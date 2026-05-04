"""
Diurnal baseline for OpenSky flight density across the Taiwan Strait.

The previous logic at collectors/airspace_maritime.py used a hard threshold
(`flight_count < 20`) with no time-of-day awareness. Civil aviation through
the strait has a strong diurnal cycle — early-morning Taipei time naturally
sees lower counts. A static threshold produces false positives at dawn.

This module:
  1. Persists each tick's flight count + Taipei hour-of-day to
     data/opensky_baselines.jsonl (append-only, capped).
  2. Computes a robust LOW-side anomaly check (median - N*MAD) over a
     rolling window, bucketed by Taipei hour-of-day.
  3. Bootstrap-aware: until we have BOOTSTRAP_PER_HOUR samples for the
     current hour bucket, falls back to a conservative hard floor (5)
     so we don't suppress catastrophic shortfalls during the warm-up.

Design parallel to analysis/baseline.py (MAD-based, append-only JSONL,
abstain on insufficient samples) but with two differences: (a) hour
bucketing, (b) detects deviation BELOW baseline (flight density crash)
rather than above (PLA force buildup).
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from config import DATA_DIR

log = logging.getLogger(__name__)

OPENSKY_BASELINE_FILE = os.path.join(DATA_DIR, "opensky_baselines.jsonl")

WINDOW_SAMPLES = 240             # ~30-60 days at 4-8h cron cadence
BOOTSTRAP_PER_HOUR = 5           # min samples in this hour-bucket before MAD fires
LOW_ANOMALY_MAD_MULTIPLIER = 2.0
HIGH_ANOMALY_MAD_MULTIPLIER = 3.0
BOOTSTRAP_HARD_FLOOR = 5         # during bootstrap, only counts < 5 are anomalous


@dataclass
class OpenSkySample:
    timestamp: str               # ISO UTC
    taipei_hour: int             # 0-23
    flight_count: int


@dataclass
class FlightAnomalyResult:
    status: str                  # "unknown" | "normal" | "low_anomaly" | "high_low_anomaly"
    current: int
    taipei_hour: int
    median: Optional[float]
    mad: Optional[float]
    sample_size: int             # samples used (this hour bucket only)
    explanation: str


def _taipei_hour_now() -> int:
    """Current hour-of-day in Asia/Taipei."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Taipei")).hour
    except Exception:
        # Fallback: UTC + 8
        return (datetime.now(timezone.utc).hour + 8) % 24


def record_sample(flight_count: int, taipei_hour: Optional[int] = None) -> None:
    """Append one observation to the baseline. Always called when OpenSky
    fetch succeeds, even if we don't fire anomaly — so the baseline grows."""
    if taipei_hour is None:
        taipei_hour = _taipei_hour_now()
    os.makedirs(DATA_DIR, exist_ok=True)
    sample = OpenSkySample(
        timestamp=datetime.now(timezone.utc).isoformat(),
        taipei_hour=taipei_hour,
        flight_count=flight_count,
    )
    with open(OPENSKY_BASELINE_FILE, "a") as f:
        f.write(json.dumps(asdict(sample)) + "\n")
    _trim()


def _trim() -> None:
    """Cap the file at WINDOW_SAMPLES rows; keep the most recent."""
    rows = _load_all()
    if len(rows) <= WINDOW_SAMPLES:
        return
    keep = rows[-WINDOW_SAMPLES:]
    with open(OPENSKY_BASELINE_FILE, "w") as f:
        for r in keep:
            f.write(json.dumps(r) + "\n")


def _load_all() -> list[dict]:
    if not os.path.exists(OPENSKY_BASELINE_FILE):
        return []
    out: list[dict] = []
    with open(OPENSKY_BASELINE_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _mad(values: list[float], median_value: float) -> float:
    if not values:
        return 0.0
    return statistics.median(abs(v - median_value) for v in values)


def check_low_anomaly(current: int, taipei_hour: Optional[int] = None) -> FlightAnomalyResult:
    """
    Detect flight-density CRASH for the current Taipei hour bucket.

    Status:
      - "unknown"          — fewer than BOOTSTRAP_PER_HOUR samples in this
                             hour bucket; falls back to hard floor (only
                             counts < BOOTSTRAP_HARD_FLOOR are anomalous)
      - "normal"           — within median - 2*MAD
      - "low_anomaly"      — below median - 2*MAD (drives evidence_class="anomaly")
      - "high_low_anomaly" — below median - 3*MAD (RED-eligible after persistence)
    """
    if taipei_hour is None:
        taipei_hour = _taipei_hour_now()

    rows = _load_all()
    bucket = [
        r["flight_count"] for r in rows
        if isinstance(r.get("flight_count"), int)
        and r.get("taipei_hour") == taipei_hour
    ]

    if len(bucket) < BOOTSTRAP_PER_HOUR:
        # Bootstrap: only catastrophic shortfall fires
        if current < BOOTSTRAP_HARD_FLOOR:
            return FlightAnomalyResult(
                status="low_anomaly",
                current=current, taipei_hour=taipei_hour,
                median=None, mad=None, sample_size=len(bucket),
                explanation=(
                    f"Bootstrap: {len(bucket)}/{BOOTSTRAP_PER_HOUR} samples "
                    f"for hour {taipei_hour} Taipei. Hard-floor anomaly: "
                    f"{current} < {BOOTSTRAP_HARD_FLOOR} flights."
                ),
            )
        return FlightAnomalyResult(
            status="unknown",
            current=current, taipei_hour=taipei_hour,
            median=None, mad=None, sample_size=len(bucket),
            explanation=(
                f"Bootstrap: only {len(bucket)} of {BOOTSTRAP_PER_HOUR} "
                f"required samples for Taipei hour {taipei_hour}. "
                f"Current count {current} accepted (no baseline)."
            ),
        )

    median_value = statistics.median(bucket)
    mad_value = _mad(bucket, median_value)

    if mad_value == 0:
        # Degenerate population — require 50% drop below median
        if current < median_value * 0.5 and current < median_value:
            status = "low_anomaly"
        else:
            status = "normal"
        explanation = (
            f"flights = {current} (Taipei hour {taipei_hour}, "
            f"median {median_value:.0f}, MAD 0, samples={len(bucket)}). "
            f"Status: {status}."
        )
        return FlightAnomalyResult(status, current, taipei_hour,
                                   median_value, mad_value, len(bucket), explanation)

    z_high_low = median_value - HIGH_ANOMALY_MAD_MULTIPLIER * mad_value
    z_low = median_value - LOW_ANOMALY_MAD_MULTIPLIER * mad_value
    if current < z_high_low:
        status = "high_low_anomaly"
    elif current < z_low:
        status = "low_anomaly"
    else:
        status = "normal"

    explanation = (
        f"flights = {current} (Taipei hour {taipei_hour}, "
        f"median {median_value:.0f}, MAD {mad_value:.1f}, samples={len(bucket)}). "
        f"Status: {status}."
    )
    return FlightAnomalyResult(status, current, taipei_hour,
                               median_value, mad_value, len(bucket), explanation)
