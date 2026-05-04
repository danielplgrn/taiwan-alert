"""
Taiwan Strait Early Warning System — Scoring Engine

Count-based alert logic with primary/secondary gate and time-based hysteresis.

Alert states:
  GREEN  — < 2 indicators active
  YELLOW — 2+ secondaries active OR 1 primary active
  AMBER  — 1 primary + 1 other from a different indicator
  RED    — 2+ primaries active OR overt hostilities
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from config import (
    INDICATORS,
    AlertState,
    ACTION_LABELS,
    Category,
    HYSTERESIS_SECONDS,
    STATE_FILE,
    HISTORY_FILE,
    WEB_STATE_FILE,
    DATA_DIR,
    ALERT_THRESHOLD,
)


@dataclass
class IndicatorReading:
    id: int
    active: bool
    confidence: str = "none"       # "high", "medium", "low", "none" — UI/audit only; NOT consumed by evaluate()
    summary: str = ""
    last_checked: str = ""         # ISO timestamp
    feed_healthy: bool = True
    is_destructive: bool = False   # only relevant for indicator 6 (cyber)
    # Evidence class — drives the scoring engine's max-promotion rule.
    # "keyword"     — text/keyword match (weakest; cannot promote past YELLOW alone)
    # "concrete"    — observed administrative/operational act (e.g. NOTAM closure,
    #                 reserve call-up, MND announcement)
    # "anomaly"     — quantitative deviation from baseline (e.g. flight density crash,
    #                 PLA aircraft count > median + 3*MAD)
    # "hostilities" — explicit overt hostile action
    evidence_class: str = "keyword"
    # Audit fields populated by the LLM-first pipeline (military.py). Other
    # collectors leave these empty.
    # Each evidence quote: {chunk_id, source, family, key_phrase, claim_type,
    #                       directness, why}
    evidence_quotes: list[dict] = field(default_factory=list)
    rationale: str = ""                          # one-paragraph "why" for the dashboard
    manipulation_flagged_count: int = 0          # how many input chunks the LLM flagged as injection attempts
    # How many consecutive evaluations (including this one) the indicator has
    # been active. Computed by evaluate() from previous_state. Used by the
    # persistence rule: weak signals (anomaly / keyword) require ≥2
    # consecutive runs before they can drive AMBER/RED. Concrete and
    # hostilities are immediately promotable.
    consecutive_active_runs: int = 0


@dataclass
class SystemState:
    alert_state: AlertState = AlertState.GREEN
    alert_label: str = ACTION_LABELS[AlertState.GREEN]
    score_detail: str = ""
    indicators: dict[int, IndicatorReading] = field(default_factory=dict)
    degraded: bool = False
    degraded_feeds: list[str] = field(default_factory=list)
    state_since: float = 0.0       # unix timestamp when current state began
    evaluated_at: str = ""         # ISO timestamp of last evaluation
    overt_hostilities: bool = False
    threshold: int = 2             # active-indicator count required per promotion step
    last_alerted_state: str = ""   # alert_state.value of last successful Slack notification
    # Read-only LLM advisory layer (analysis/advisor.py). Pure commentary;
    # never feeds back into scoring. List of dicts:
    #   {type, indicator_ids: [int], severity: "info"|"concern", message: str}
    advisories: list[dict] = field(default_factory=list)


PERSISTENCE_REQUIRED_RUNS = 2


def is_promotable(reading: IndicatorReading) -> bool:
    """
    Whether an active indicator can drive AMBER/RED on this evaluation.

    Concrete and hostilities are always promotable — they represent observed
    administrative acts or active military events. Anomaly-class evidence
    (e.g. flight-density crash, MND count spike) is quantitative and noisy,
    so requires at least two consecutive runs of activation before it can
    contribute to AMBER/RED. Keyword evidence is never promotable past
    YELLOW regardless of persistence.

    This is the deterministic noise filter that prevents single-tick weak
    signals from flipping the alert state to "leave/shelter" before the
    operator can sanity-check.
    """
    if not reading.active:
        return False
    if reading.evidence_class in ("concrete", "hostilities"):
        return True
    if reading.evidence_class == "anomaly":
        return reading.consecutive_active_runs >= PERSISTENCE_REQUIRED_RUNS
    return False  # keyword


def effective_category(reading: IndicatorReading) -> Category:
    """Determine effective category, handling cyber escalation."""
    defn = INDICATORS[reading.id]
    if defn.can_escalate_to_primary and reading.is_destructive:
        return Category.PRIMARY
    return defn.category


def evaluate(
    readings: dict[int, IndicatorReading],
    overt_hostilities: bool = False,
    previous_state: Optional[SystemState] = None,
    threshold: Optional[int] = None,
) -> SystemState:
    """
    Evaluate all indicator readings and produce the current system state.

    `threshold` is the minimum active-indicator count required at each
    promotion step. Defaults to ALERT_THRESHOLD from config (2).
    """
    now = time.time()
    t = max(1, threshold if threshold is not None else ALERT_THRESHOLD)

    # --- Compute consecutive_active_runs by comparing to previous state ---
    prev_indicators = previous_state.indicators if previous_state else {}
    for ind_id, reading in readings.items():
        prev = prev_indicators.get(ind_id)
        if reading.active:
            prev_runs = prev.consecutive_active_runs if (prev and prev.active) else 0
            reading.consecutive_active_runs = prev_runs + 1
        else:
            reading.consecutive_active_runs = 0

    # --- Count active primaries and secondaries ---
    active_primaries: list[int] = []
    active_secondaries: list[int] = []

    for ind_id, reading in readings.items():
        if not reading.active:
            continue
        cat = effective_category(reading)
        if cat == Category.PRIMARY:
            active_primaries.append(ind_id)
        else:
            active_secondaries.append(ind_id)

    total_active = len(active_primaries) + len(active_secondaries)

    # --- Determine raw alert state ---
    # Promotability gates AMBER/RED. Concrete and hostilities are immediately
    # promotable. Anomaly requires PERSISTENCE_REQUIRED_RUNS consecutive runs
    # of activation before it can promote past YELLOW. Keyword stays at
    # YELLOW indefinitely. This is the deterministic noise filter against
    # single-tick false positives.
    #
    # AMBER requires at least one PROMOTABLE primary plus any second active
    # indicator (the corroborating second leg can be keyword-class). RED
    # requires the threshold count of PROMOTABLE primaries.
    promotable_primaries = [i for i in active_primaries if is_promotable(readings[i])]
    has_promotable_primary = len(promotable_primaries) >= 1

    # Audit: which active indicators are awaiting persistence
    awaiting_persistence = [
        INDICATORS[i].name for i in active_primaries + active_secondaries
        if readings[i].evidence_class == "anomaly"
        and readings[i].consecutive_active_runs < PERSISTENCE_REQUIRED_RUNS
    ]
    persistence_note = (
        f" (awaiting persistence: {', '.join(awaiting_persistence)})"
        if awaiting_persistence else ""
    )

    if overt_hostilities:
        raw_state = AlertState.RED
        detail = "Overt hostilities flagged"
    elif len(promotable_primaries) >= t and has_promotable_primary:
        raw_state = AlertState.RED
        names = [INDICATORS[i].name for i in promotable_primaries]
        detail = f"{len(promotable_primaries)} promotable primaries active (threshold {t}): {', '.join(names)}"
    elif has_promotable_primary and total_active >= t:
        raw_state = AlertState.AMBER
        p_names = [INDICATORS[i].name for i in promotable_primaries]
        s_names = [INDICATORS[i].name for i in active_secondaries]
        detail = f"Primary: {', '.join(p_names)} + Secondary: {', '.join(s_names) or '(none)'} (threshold {t})"
    elif len(active_primaries) >= 1:
        raw_state = AlertState.YELLOW
        names = [INDICATORS[i].name for i in active_primaries]
        cap_note = ""
        if not has_promotable_primary and len(active_primaries) >= 1:
            # Distinguish keyword-only from anomaly-awaiting-persistence so audit logs
            # / tests can read the cause without parsing internal state.
            classes = {readings[i].evidence_class for i in active_primaries}
            if classes == {"keyword"}:
                cap_note = " (keyword-only evidence — capped at Yellow)"
            elif "anomaly" in classes and "keyword" in classes:
                cap_note = " (keyword/anomaly awaiting persistence — capped at Yellow)"
            else:
                cap_note = " (anomaly awaiting persistence — capped at Yellow)"
        detail = f"{len(active_primaries)} primary active: {', '.join(names)}{cap_note}{persistence_note}"
    elif len(active_secondaries) >= t:
        raw_state = AlertState.YELLOW
        names = [INDICATORS[i].name for i in active_secondaries]
        detail = f"{len(active_secondaries)} secondaries active (threshold {t}): {', '.join(names)}{persistence_note}"
    else:
        raw_state = AlertState.GREEN
        detail = f"{total_active} indicator(s) active{persistence_note}"

    # --- Apply hysteresis for demotions ---
    STATE_ORDER = [AlertState.GREEN, AlertState.YELLOW, AlertState.AMBER, AlertState.RED]

    final_state = raw_state
    if previous_state and previous_state.alert_state != AlertState.GREEN:
        prev_rank = STATE_ORDER.index(previous_state.alert_state)
        raw_rank = STATE_ORDER.index(raw_state)

        if raw_rank < prev_rank:
            elapsed = now - previous_state.state_since
            required = HYSTERESIS_SECONDS.get(previous_state.alert_state, 0)
            if elapsed < required:
                final_state = previous_state.alert_state
                remaining_h = (required - elapsed) / 3600
                detail += f" (cooldown: holding {previous_state.alert_state.value} for {remaining_h:.1f}h more before downgrading)"

    # --- Degraded state ---
    unhealthy = [
        INDICATORS[ind_id].name
        for ind_id, reading in readings.items()
        if not reading.feed_healthy
    ]

    # --- Track state_since ---
    if previous_state and final_state == previous_state.alert_state:
        state_since = previous_state.state_since
    else:
        state_since = now

    from datetime import datetime, timezone
    evaluated_at = datetime.now(timezone.utc).isoformat()

    return SystemState(
        alert_state=final_state,
        alert_label=ACTION_LABELS[final_state],
        score_detail=detail,
        indicators=readings,
        degraded=len(unhealthy) > 0,
        degraded_feeds=unhealthy,
        state_since=state_since,
        evaluated_at=evaluated_at,
        overt_hostilities=overt_hostilities,
        threshold=t,
    )


# ---------------------------------------------------------------------------
# Persistence — read/write state.json and history.jsonl
# ---------------------------------------------------------------------------

def _state_to_dict(state: SystemState) -> dict:
    return {
        "alert_state": state.alert_state.value,
        "alert_label": state.alert_label,
        "score_detail": state.score_detail,
        "degraded": state.degraded,
        "degraded_feeds": state.degraded_feeds,
        "state_since": state.state_since,
        "evaluated_at": state.evaluated_at,
        "overt_hostilities": state.overt_hostilities,
        "threshold": state.threshold,
        "last_alerted_state": state.last_alerted_state,
        "advisories": state.advisories,
        "indicators": {
            str(ind_id): {
                "id": r.id,
                "name": INDICATORS[r.id].name,
                "description": INDICATORS[r.id].description,
                "category": effective_category(r).value,
                "active": r.active,
                "confidence": r.confidence,
                "summary": r.summary,
                "last_checked": r.last_checked,
                "feed_healthy": r.feed_healthy,
                "is_destructive": r.is_destructive,
                "evidence_class": r.evidence_class,
                "evidence_quotes": r.evidence_quotes,
                "rationale": r.rationale,
                "manipulation_flagged_count": r.manipulation_flagged_count,
                "consecutive_active_runs": r.consecutive_active_runs,
                "promotable": is_promotable(r),
            }
            for ind_id, r in state.indicators.items()
        },
    }


def save_state(state: SystemState) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    d = _state_to_dict(state)
    payload = json.dumps(d, indent=2)

    with open(STATE_FILE, "w") as f:
        f.write(payload)

    web_dir = os.path.dirname(WEB_STATE_FILE)
    os.makedirs(web_dir, exist_ok=True)
    with open(WEB_STATE_FILE, "w") as f:
        f.write(payload)

    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(d) + "\n")


def load_previous_state() -> Optional[SystemState]:
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        raw_indicators = d.get("indicators", {})
        indicators: dict[int, IndicatorReading] = {}
        for k, v in raw_indicators.items():
            try:
                ind_id = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, IndicatorReading):
                indicators[ind_id] = v
            elif isinstance(v, dict):
                indicators[ind_id] = IndicatorReading(
                    id=v.get("id", ind_id),
                    active=v.get("active", False),
                    confidence=v.get("confidence", "none"),
                    summary=v.get("summary", ""),
                    last_checked=v.get("last_checked", ""),
                    feed_healthy=v.get("feed_healthy", True),
                    is_destructive=v.get("is_destructive", False),
                    evidence_class=v.get("evidence_class", "keyword"),
                    evidence_quotes=v.get("evidence_quotes") or [],
                    rationale=v.get("rationale", ""),
                    manipulation_flagged_count=v.get("manipulation_flagged_count", 0),
                    consecutive_active_runs=v.get("consecutive_active_runs", 0),
                )
        return SystemState(
            alert_state=AlertState(d["alert_state"]),
            alert_label=d.get("alert_label", ""),
            score_detail=d.get("score_detail", ""),
            degraded=d.get("degraded", False),
            degraded_feeds=d.get("degraded_feeds", []),
            state_since=d.get("state_since", 0.0),
            evaluated_at=d.get("evaluated_at", ""),
            overt_hostilities=d.get("overt_hostilities", False),
            threshold=d.get("threshold", ALERT_THRESHOLD),
            last_alerted_state=d.get("last_alerted_state", ""),
            advisories=d.get("advisories") or [],
            indicators=indicators,
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
