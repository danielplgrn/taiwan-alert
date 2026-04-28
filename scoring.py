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
    confidence: str = "none"       # "high", "medium", "low", "none"
    summary: str = ""
    last_checked: str = ""         # ISO timestamp
    feed_healthy: bool = True
    is_destructive: bool = False   # only relevant for indicator 6 (cyber)


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
    if overt_hostilities:
        raw_state = AlertState.RED
        detail = "Overt hostilities flagged"
    elif len(active_primaries) >= t:
        raw_state = AlertState.RED
        names = [INDICATORS[i].name for i in active_primaries]
        detail = f"{len(active_primaries)} primaries active (threshold {t}): {', '.join(names)}"
    elif len(active_primaries) >= 1 and total_active >= t:
        raw_state = AlertState.AMBER
        p_names = [INDICATORS[i].name for i in active_primaries]
        s_names = [INDICATORS[i].name for i in active_secondaries]
        detail = f"Primary: {', '.join(p_names)} + Secondary: {', '.join(s_names) or '(none)'} (threshold {t})"
    elif len(active_primaries) >= 1:
        raw_state = AlertState.YELLOW
        names = [INDICATORS[i].name for i in active_primaries]
        detail = f"1 primary active: {', '.join(names)}"
    elif len(active_secondaries) >= t:
        raw_state = AlertState.YELLOW
        names = [INDICATORS[i].name for i in active_secondaries]
        detail = f"{len(active_secondaries)} secondaries active (threshold {t}): {', '.join(names)}"
    else:
        raw_state = AlertState.GREEN
        detail = f"{total_active} indicator(s) active"

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
            indicators=indicators,
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
