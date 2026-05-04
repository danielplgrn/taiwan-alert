#!/usr/bin/env python3
"""
Taiwan Strait Early Warning System — Main Collection Runner

This script is invoked by cron at various cadences. It accepts a --group
argument to run only the collectors for that polling group.

Usage:
  python collect.py                   # run ALL collectors
  python collect.py --group 30min     # run only 30-min cadence collectors
  python collect.py --group 2hours    # run only 2-hour cadence collectors
  python collect.py --group 6hours    # run only 6-hour cadence collectors
  python collect.py --group daily_9am # run only daily 9AM TPE collectors
  python collect.py --dispatch-alerts # send Slack if state changed since last alert

After collection, it evaluates the scoring engine, updates state.json, and
appends to history.jsonl. Slack alerts are dispatched separately via
--dispatch-alerts, called by the workflow after a successful Pages deploy.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import INDICATORS, STATE_FILE, DATA_DIR, AlertState, ACTION_LABELS
from scoring import IndicatorReading, SystemState, evaluate, save_state, load_previous_state
from alerting import send_alert
from analysis.advisor import generate_advisories

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("collect")

# Map poll_group -> collector modules and the indicator IDs they produce
COLLECTOR_REGISTRY: dict[str, list[dict]] = {
    "30min": [
        {"module": "collectors.airspace_maritime", "indicators": [3, 4]},
        {"module": "collectors.taiwan_readiness", "indicators": [5]},
        {"module": "collectors.cyber_infra", "indicators": [6]},
    ],
    "2hours": [
        {"module": "collectors.diplomatic", "indicators": [7]},
        {"module": "collectors.financial", "indicators": [10]},
    ],
    "6hours": [
        {"module": "collectors.rhetoric_political", "indicators": [9]},
    ],
    "daily_9am": [
        # military.py is the sole owner of indicators 1, 2, and 8.
        # The previous lossy OR-merge with collectors.allied was removed when
        # the LLM-first pipeline replaced the keyword approach.
        {"module": "collectors.military", "indicators": [1, 2, 8]},
    ],
}


def run_collectors(group: str | None = None) -> dict[int, IndicatorReading]:
    """Run collectors for the given group (or all groups). Returns readings by indicator ID."""
    readings: dict[int, IndicatorReading] = {}

    groups = [group] if group else list(COLLECTOR_REGISTRY.keys())

    for g in groups:
        if g not in COLLECTOR_REGISTRY:
            log.warning("Unknown collector group: %s", g)
            continue

        for entry in COLLECTOR_REGISTRY[g]:
            mod_name = entry["module"]
            log.info("Running collector: %s", mod_name)

            try:
                mod = __import__(mod_name, fromlist=["collect"])
                results = mod.collect()
                for reading in results:
                    if isinstance(reading, IndicatorReading):
                        # If we already have a reading for this indicator (e.g. allied
                        # from both military.py and allied.py), merge: OR on active
                        if reading.id in readings:
                            existing = readings[reading.id]
                            if reading.active and not existing.active:
                                readings[reading.id] = reading
                            # keep existing if it was already active
                        else:
                            readings[reading.id] = reading
            except Exception as e:
                log.exception("Collector %s failed: %s", mod_name, e)
                # Mark indicators from this collector as unhealthy
                for ind_id in entry["indicators"]:
                    if ind_id not in readings:
                        readings[ind_id] = IndicatorReading(
                            id=ind_id,
                            active=False,
                            confidence="none",
                            summary=f"Collector failed: {e}",
                            last_checked="",
                            feed_healthy=False,
                        )

    return readings


def merge_with_previous(
    new_readings: dict[int, IndicatorReading],
    previous_state: SystemState | None,
) -> dict[int, IndicatorReading]:
    """
    When running a partial group, we don't have fresh readings for all indicators.
    Carry forward previous readings for indicators not collected in this run.
    """
    if not previous_state or not previous_state.indicators:
        # Fill missing indicators with inactive defaults
        for ind_id in INDICATORS:
            if ind_id not in new_readings:
                new_readings[ind_id] = IndicatorReading(
                    id=ind_id, active=False, summary="Not yet checked",
                )
        return new_readings

    # Carry forward previous readings for indicators not in this run
    for ind_id_str, prev_data in previous_state.indicators.items():
        ind_id = int(ind_id_str) if isinstance(ind_id_str, str) else ind_id_str
        if ind_id not in new_readings:
            # Reconstruct reading from previous state dict
            if isinstance(prev_data, dict):
                new_readings[ind_id] = IndicatorReading(
                    id=ind_id,
                    active=prev_data.get("active", False),
                    confidence=prev_data.get("confidence", "none"),
                    summary=prev_data.get("summary", ""),
                    last_checked=prev_data.get("last_checked", ""),
                    feed_healthy=prev_data.get("feed_healthy", True),
                    is_destructive=prev_data.get("is_destructive", False),
                    evidence_class=prev_data.get("evidence_class", "keyword"),
                    evidence_quotes=prev_data.get("evidence_quotes") or [],
                    rationale=prev_data.get("rationale", ""),
                    manipulation_flagged_count=prev_data.get("manipulation_flagged_count", 0),
                    consecutive_active_runs=prev_data.get("consecutive_active_runs", 0),
                )
            elif isinstance(prev_data, IndicatorReading):
                new_readings[ind_id] = prev_data

    # Still fill any missing
    for ind_id in INDICATORS:
        if ind_id not in new_readings:
            new_readings[ind_id] = IndicatorReading(
                id=ind_id, active=False, summary="Not yet checked",
            )

    return new_readings


def dispatch_alerts() -> None:
    """Send Slack alert if current state has changed since last notification."""
    current = load_previous_state()
    if current is None:
        log.warning("No state file found — skipping alert dispatch")
        return

    last = AlertState(current.last_alerted_state) if current.last_alerted_state else AlertState.GREEN
    high_states = (AlertState.AMBER, AlertState.RED)

    needs_alert = (
        current.alert_state != last
        and (current.alert_state in high_states or last in high_states)
    )

    if not needs_alert:
        log.info("No alert needed (current=%s, last_alerted=%s)", current.alert_state.value, last.value)
        return

    # Build a synthetic previous-state object for the alert message
    fake_previous = SystemState(
        alert_state=last,
        alert_label=ACTION_LABELS[last],
    )

    log.info("Dispatching alert: %s -> %s", last.value, current.alert_state.value)
    if send_alert(fake_previous, current):
        current.last_alerted_state = current.alert_state.value
        save_state(current)
        log.info("Alert watermark updated to %s", current.alert_state.value)


def main():
    parser = argparse.ArgumentParser(description="Taiwan Alert System — collector runner")
    parser.add_argument("--group", type=str, default=None,
                        help="Poll group to run (30min, 2hours, 6hours, daily_9am)")
    parser.add_argument("--dispatch-alerts", action="store_true",
                        help="Send Slack alert if state changed since last notification")
    args = parser.parse_args()

    if args.dispatch_alerts:
        dispatch_alerts()
        return

    log.info("=== Taiwan Alert collection run: group=%s ===", args.group or "ALL")

    # Load previous state for carry-forward and hysteresis
    previous_state = load_previous_state()

    # Run collectors
    new_readings = run_collectors(args.group)

    # Merge with previous readings for indicators not collected in this run
    all_readings = merge_with_previous(new_readings, previous_state)

    # Evaluate scoring engine
    current_state = evaluate(
        readings=all_readings,
        previous_state=previous_state,
    )

    # Carry alert watermark forward (only --dispatch-alerts advances it)
    current_state.last_alerted_state = (
        previous_state.last_alerted_state if previous_state else ""
    )

    # Advisory LLM layer (read-only commentary). Never mutates alert_state
    # or any indicator field; output stored as `advisories` for the dashboard
    # and slack alert. Empty list when ADVISOR_ENABLED is unset, the API is
    # unreachable, or the model has nothing notable to flag.
    try:
        current_state.advisories = generate_advisories(current_state)
    except Exception as e:
        log.warning("Advisor failed (non-fatal): %s", e)
        current_state.advisories = []

    # Save state
    save_state(current_state)
    log.info("State: %s (%s) — %s",
             current_state.alert_state.value,
             current_state.alert_label,
             current_state.score_detail)

    if current_state.degraded:
        log.warning("DEGRADED feeds: %s", ", ".join(current_state.degraded_feeds))

    # Mark this group as done in group_runs.json
    if args.group:
        try:
            from ci_groups import mark_group_done
            mark_group_done(args.group)
        except Exception as e:
            log.warning("Could not update group_runs.json: %s", e)

    log.info("=== Collection run complete ===")


if __name__ == "__main__":
    main()
