"""
Baseline tracking for MND-reported PLA activity counts.

Taiwan MND publishes a daily bulletin with concrete integers — number of
PLA aircraft entering the ADIZ, number of vessels detected, etc. These
numbers are the only quantitative signal we get; everything else is
qualitative keyword matching.

This module:
  1. Parses today's MND bulletin into integer counts.
  2. Persists each day's counts to data/baselines.jsonl (append-only).
  3. Computes a robust anomaly check (median + 2 × MAD) over a rolling
     60-day window, with a 14-day bootstrap before any anomaly fires.
  4. Records parser confidence + raw text snippet so a fragile parse
     never becomes a false anomaly. On parse failure → "unknown", not
     "anomalous".
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from typing import Optional

from config import BASELINE_FILE, DATA_DIR

log = logging.getLogger(__name__)

WINDOW_DAYS = 60
BOOTSTRAP_DAYS = 14
ANOMALY_MAD_MULTIPLIER = 2.0    # current >= median + N * MAD = anomaly
HIGH_ANOMALY_MAD_MULTIPLIER = 3.0  # used by scoring for RED-eligible anomaly


# ---------------------------------------------------------------------------
# Patterns — MND bulletins use phrasing like "20 PLA aircraft", "5 PLAN
# vessels", "X aircraft entering ADIZ". We extract integers paired with
# these tokens.
# ---------------------------------------------------------------------------

# We deliberately avoid the trailing dot/colon pattern lookbehind so the regex
# tolerates Chinese MND HTML scraping noise (varied whitespace, mixed punct).
_AIRCRAFT_PATTERNS = [
    re.compile(r"(\d{1,3})\s*(?:pla|chinese|prc)?\s*aircraft", re.IGNORECASE),
    re.compile(r"(\d{1,3})\s*(?:pla|chinese|prc)?\s*sorties", re.IGNORECASE),
    re.compile(r"detected\s*(\d{1,3})\s*(?:pla|chinese|prc)?\s*aircraft", re.IGNORECASE),
]

_VESSEL_PATTERNS = [
    re.compile(r"(\d{1,3})\s*(?:pla|plan|chinese|prc)\s*vessels?", re.IGNORECASE),
    re.compile(r"(\d{1,3})\s*(?:pla|plan|chinese|prc)\s*ships?", re.IGNORECASE),
    re.compile(r"(\d{1,3})\s*naval\s*vessels?", re.IGNORECASE),
]


@dataclass
class BaselineEntry:
    date: str                    # ISO date (YYYY-MM-DD), Asia/Taipei
    aircraft: Optional[int]      # parsed PLA aircraft count, or None on failure
    vessels: Optional[int]       # parsed PLAN vessel count, or None on failure
    parser_confidence: str       # "high" | "low" | "failed"
    raw_excerpt: str             # short snippet for human verification


@dataclass
class AnomalyResult:
    status: str                  # "normal" | "anomaly" | "high_anomaly" | "unknown"
    metric: str                  # which metric was anomalous (e.g. "aircraft")
    current: Optional[int]
    median: Optional[float]
    mad: Optional[float]
    sample_size: int
    explanation: str


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _max_match(text: str, patterns: list[re.Pattern]) -> Optional[int]:
    """Return the largest integer matching any pattern (caps at 500 to ignore parse junk)."""
    values: list[int] = []
    for p in patterns:
        for m in p.finditer(text):
            try:
                v = int(m.group(1))
                if 0 <= v <= 500:
                    values.append(v)
            except (ValueError, IndexError):
                continue
    return max(values) if values else None


def parse_mnd_counts(text: str, today: Optional[date] = None) -> BaselineEntry:
    """
    Parse a fetched MND bulletin into an aircraft + vessel count.

    Resilient to wording variation: tries multiple patterns; takes the max
    integer found for each metric. Records "low" or "failed" parser_confidence
    when no pattern matches, so anomaly checks know to abstain.
    """
    if today is None:
        today = date.today()

    aircraft = _max_match(text, _AIRCRAFT_PATTERNS)
    vessels = _max_match(text, _VESSEL_PATTERNS)

    if aircraft is None and vessels is None:
        confidence = "failed"
    elif aircraft is None or vessels is None:
        confidence = "low"
    else:
        confidence = "high"

    # Short excerpt to commit alongside the count for human verification
    excerpt = (text[:600] + "...") if len(text) > 600 else text
    excerpt = re.sub(r"\s+", " ", excerpt).strip()

    return BaselineEntry(
        date=today.isoformat(),
        aircraft=aircraft,
        vessels=vessels,
        parser_confidence=confidence,
        raw_excerpt=excerpt,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def append_baseline(entry: BaselineEntry) -> None:
    """Append a baseline entry to baselines.jsonl. One row per day."""
    os.makedirs(DATA_DIR, exist_ok=True)
    # If today's date is already at the tail, replace it (idempotent across runs in same day)
    existing = load_baselines()
    same_day = [i for i, e in enumerate(existing) if e["date"] == entry.date]
    if same_day:
        existing[same_day[-1]] = asdict(entry)
    else:
        existing.append(asdict(entry))
    # Cap at WINDOW_DAYS * 2 entries to bound file size; keep most recent
    cap = WINDOW_DAYS * 2
    if len(existing) > cap:
        existing = existing[-cap:]
    with open(BASELINE_FILE, "w") as f:
        for row in existing:
            f.write(json.dumps(row) + "\n")


def load_baselines() -> list[dict]:
    """Load baselines.jsonl. Returns list of dicts (oldest first)."""
    if not os.path.exists(BASELINE_FILE):
        return []
    out: list[dict] = []
    with open(BASELINE_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ---------------------------------------------------------------------------
# Anomaly check
# ---------------------------------------------------------------------------

def _mad(values: list[float], median_value: float) -> float:
    """Median absolute deviation."""
    if not values:
        return 0.0
    return statistics.median(abs(v - median_value) for v in values)


def check_anomaly(metric: str, current: Optional[int]) -> AnomalyResult:
    """
    Compare `current` to the rolling baseline for `metric` ("aircraft" | "vessels").

    Returns AnomalyResult with status:
      - "unknown"      — fewer than BOOTSTRAP_DAYS samples, OR current is None
      - "normal"       — within median + 2*MAD
      - "anomaly"      — above median + 2*MAD (used for evidence_class="anomaly")
      - "high_anomaly" — above median + 3*MAD (RED-eligible)
    """
    if current is None:
        return AnomalyResult("unknown", metric, current, None, None, 0,
                             f"Could not parse current {metric} count.")

    rows = load_baselines()
    # Only use samples with high parser_confidence and a value for this metric
    samples = [r[metric] for r in rows
               if r.get("parser_confidence") == "high"
               and isinstance(r.get(metric), int)]

    if len(samples) < BOOTSTRAP_DAYS:
        return AnomalyResult(
            "unknown", metric, current, None, None, len(samples),
            f"Bootstrap: only {len(samples)} of {BOOTSTRAP_DAYS} required {metric} samples collected.",
        )

    # Use only the most recent WINDOW_DAYS
    window = samples[-WINDOW_DAYS:]
    median_value = statistics.median(window)
    mad_value = _mad(window, median_value)

    # If the population is constant (mad == 0), require a hard 1.5x threshold
    if mad_value == 0:
        if current >= median_value * 1.5 and current > median_value:
            status = "anomaly"
        else:
            status = "normal"
    else:
        z_high = median_value + HIGH_ANOMALY_MAD_MULTIPLIER * mad_value
        z_low = median_value + ANOMALY_MAD_MULTIPLIER * mad_value
        if current >= z_high:
            status = "high_anomaly"
        elif current >= z_low:
            status = "anomaly"
        else:
            status = "normal"

    explanation = (
        f"{metric} = {current} (median {median_value:.0f}, MAD {mad_value:.1f}, "
        f"window={len(window)}d). Status: {status}."
    )

    return AnomalyResult(status, metric, current, median_value, mad_value,
                         len(window), explanation)


def now_taipei_date() -> date:
    """Today's date in Asia/Taipei (so MND publication day aligns with our row)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Taipei")).date()
    except Exception:
        return datetime.now(timezone.utc).date()
