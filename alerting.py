"""
Taiwan Strait Early Warning System — Slack Alerting

Sends alerts on state transitions to Amber or Red.
Sends recovery notices when dropping back from Amber/Red.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error
from typing import Optional

from config import AlertState, ACTION_LABELS, SLACK_WEBHOOK_URL
from scoring import SystemState

TEST_MODE = os.getenv("TAIWAN_ALERT_TEST_MODE", "").lower() in ("1", "true", "yes")
TEST_PREFIX = "🧪 TEST — " if TEST_MODE else ""

log = logging.getLogger(__name__)

# State colors for Slack attachment
STATE_COLORS = {
    AlertState.GREEN: "#2ecc71",
    AlertState.YELLOW: "#f1c40f",
    AlertState.AMBER: "#e67e22",
    AlertState.RED: "#e74c3c",
}

STATE_EMOJI = {
    AlertState.GREEN: ":large_green_circle:",
    AlertState.YELLOW: ":large_yellow_circle:",
    AlertState.AMBER: ":large_orange_circle:",
    AlertState.RED: ":red_circle:",
}


def should_alert(previous: Optional[SystemState], current: SystemState) -> bool:
    """Alert on transitions to/from Amber or Red, or on new Degraded status."""
    if not previous:
        return current.alert_state in (AlertState.AMBER, AlertState.RED)

    # Alert on any state change involving Amber or Red
    if previous.alert_state != current.alert_state:
        high_states = (AlertState.AMBER, AlertState.RED)
        if current.alert_state in high_states or previous.alert_state in high_states:
            return True

    # Alert if newly degraded
    if current.degraded and not previous.degraded:
        return True

    return False


def build_message(previous: Optional[SystemState], current: SystemState) -> dict:
    """Build Slack message payload."""
    emoji = STATE_EMOJI.get(current.alert_state, ":white_circle:")
    color = STATE_COLORS.get(current.alert_state, "#95a5a6")

    if previous and previous.alert_state != current.alert_state:
        prev_label = ACTION_LABELS[previous.alert_state]
        curr_label = ACTION_LABELS[current.alert_state]
        title = f"{TEST_PREFIX}{emoji} Taiwan Alert: {previous.alert_state.value.upper()} -> {current.alert_state.value.upper()}"
        subtitle = f"Action changed: {prev_label} -> *{curr_label}*"
    else:
        curr_label = ACTION_LABELS[current.alert_state]
        title = f"{TEST_PREFIX}{emoji} Taiwan Alert: {current.alert_state.value.upper()}"
        subtitle = f"Action: *{curr_label}*"

    # Active indicators summary
    active_lines = []
    for ind_id, ind in sorted(current.indicators.items()):
        if ind.active:
            conf = f" ({ind.confidence})" if ind.confidence != "none" else ""
            active_lines.append(f"  *{ind.id}. {ind.summary or INDICATORS_NAMES.get(ind.id, '')}*{conf}")

    active_text = "\n".join(active_lines) if active_lines else "  None"

    # Degraded warning
    degraded_text = ""
    if current.degraded:
        degraded_text = f"\n:warning: *Degraded feeds*: {', '.join(current.degraded_feeds)}"

    fields = [
        {"title": "Detail", "value": current.score_detail, "short": False},
    ]

    return {
        "text": title,
        "attachments": [
            {
                "color": color,
                "text": f"{subtitle}\n\n*Active indicators:*\n{active_text}{degraded_text}",
                "fields": fields,
                "footer": f"Taiwan Strait EWS | {current.evaluated_at}",
            }
        ],
    }


# Lookup for indicator names in alert messages
from config import INDICATORS
INDICATORS_NAMES = {ind_id: ind.name for ind_id, ind in INDICATORS.items()}


def send_alert(previous: Optional[SystemState], current: SystemState) -> bool:
    """Send Slack alert. Returns True on success."""
    if not SLACK_WEBHOOK_URL:
        log.warning("SLACK_WEBHOOK_URL not configured — skipping alert")
        return False

    payload = build_message(previous, current)
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                log.info("Slack alert sent: %s -> %s",
                         previous.alert_state.value if previous else "init",
                         current.alert_state.value)
                return True
            else:
                log.error("Slack returned status %d", resp.status)
                return False
    except urllib.error.URLError as e:
        log.error("Slack alert failed: %s", e)
        return False
