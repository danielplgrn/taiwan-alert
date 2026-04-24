#!/usr/bin/env python3
"""
Slot-based poll-group scheduling for CI.

Prints space-separated group names that are due for this run based on
data/group_runs.json and UTC wall-clock time. Called by the GitHub Actions
workflow to determine which collect.py --group invocations to run.

Usage:
  python ci_groups.py            # prints due groups, e.g. "30min 2hours"
  python ci_groups.py --mark daily_9am  # mark a group as done (used internally)
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # Python < 3.9

GROUP_RUNS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "group_runs.json")
TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# Slot cadences — group fires when floor(now/cadence) > recorded_slot
SLOT_CADENCES: dict[str, timedelta] = {
    "30min": timedelta(minutes=30),
    "2hours": timedelta(hours=2),
    "6hours": timedelta(hours=6),
}


def _get_slot(now: datetime, cadence: timedelta) -> int:
    return int(now.timestamp() // cadence.total_seconds())


def _load() -> dict:
    if os.path.exists(GROUP_RUNS_FILE):
        with open(GROUP_RUNS_FILE) as f:
            return json.load(f)
    return {}


def _save(runs: dict) -> None:
    os.makedirs(os.path.dirname(GROUP_RUNS_FILE), exist_ok=True)
    with open(GROUP_RUNS_FILE, "w") as f:
        json.dump(runs, f, indent=2)


def due_groups(now: datetime | None = None) -> list[str]:
    """Return list of groups due to run at the given time (defaults to now UTC)."""
    if now is None:
        now = datetime.now(timezone.utc)
    runs = _load()
    due = []

    for group, cadence in SLOT_CADENCES.items():
        current_slot = _get_slot(now, cadence)
        last_slot = runs.get(group)
        if last_slot is None or last_slot < current_slot:
            due.append(group)

    # daily_9am: run once per Asia/Taipei calendar date
    taipei_today = now.astimezone(TAIPEI_TZ).date().isoformat()
    if runs.get("daily_9am") != taipei_today:
        due.append("daily_9am")

    return due


def mark_group_done(group: str, now: datetime | None = None) -> None:
    """Record that a group completed successfully at the given time."""
    if now is None:
        now = datetime.now(timezone.utc)
    runs = _load()

    if group in SLOT_CADENCES:
        runs[group] = _get_slot(now, SLOT_CADENCES[group])
    elif group == "daily_9am":
        runs["daily_9am"] = now.astimezone(TAIPEI_TZ).date().isoformat()
    else:
        return

    _save(runs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mark", metavar="GROUP", help="Mark a group as done and exit")
    args = parser.parse_args()

    if args.mark:
        mark_group_done(args.mark)
    else:
        print(" ".join(due_groups()))
