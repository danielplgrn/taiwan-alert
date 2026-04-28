"""
Taiwan Strait Early Warning System — Configuration

All indicator definitions, categories, thresholds, and polling cadences.
"""

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Alert states
# ---------------------------------------------------------------------------

class AlertState(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    AMBER = "amber"
    RED = "red"

ACTION_LABELS = {
    AlertState.GREEN: "Monitor",
    AlertState.YELLOW: "Prepare",
    AlertState.AMBER: "Leave within 48h",
    AlertState.RED: "Shelter",
}


# ---------------------------------------------------------------------------
# Indicator category
# ---------------------------------------------------------------------------

class Category(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


# ---------------------------------------------------------------------------
# Indicator definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IndicatorDef:
    id: int
    name: str
    category: Category
    description: str
    can_escalate_to_primary: bool = False          # e.g. cyber: secondary by default, primary if destructive
    poll_group: str = "30min"                      # which cron group runs this


INDICATORS: dict[int, IndicatorDef] = {d.id: d for d in [
    IndicatorDef(
        id=1,
        name="Force Concentration",
        category=Category.PRIMARY,
        description="PLA ship/aircraft/missile repositioning beyond exercise norms",
        poll_group="daily_9am",
    ),
    IndicatorDef(
        id=2,
        name="Logistics & Mobilization",
        category=Category.PRIMARY,
        description="Fuel staging, ammo movement, reserve call-ups, transport requisition",
        poll_group="daily_9am",
    ),
    IndicatorDef(
        id=3,
        name="Airspace Control",
        category=Category.PRIMARY,
        description="NOTAM closures near Fujian/Strait, civilian flight rerouting",
        poll_group="30min",
    ),
    IndicatorDef(
        id=4,
        name="Maritime Control",
        category=Category.PRIMARY,
        description="MSA exclusion zones, shipping avoidance, fishing fleet recall",
        poll_group="30min",
    ),
    IndicatorDef(
        id=5,
        name="Taiwan Domestic Readiness",
        category=Category.PRIMARY,
        description="Taiwan MND raises alert level, cancels leave, activates reserves",
        poll_group="30min",
    ),
    IndicatorDef(
        id=6,
        name="Cyber & Infrastructure",
        category=Category.SECONDARY,
        description="Cyberattacks on Taiwan infra, cable cuts, GNSS jamming",
        can_escalate_to_primary=True,               # becomes primary if destructive/systemic
        poll_group="30min",
    ),
    IndicatorDef(
        id=7,
        name="Diplomatic Signals",
        category=Category.SECONDARY,
        description="Travel advisories upgraded, China restricts travel to Taiwan",
        poll_group="2hours",
    ),
    IndicatorDef(
        id=8,
        name="Allied Response",
        category=Category.SECONDARY,
        description="US/Japan military repositioning, posture changes",
        poll_group="daily_9am",
    ),
    IndicatorDef(
        id=9,
        name="Rhetoric & Political Pressure",
        category=Category.SECONDARY,
        description="State media narrative shift, political crisis, coercion escalation",
        poll_group="6hours",
    ),
    IndicatorDef(
        id=10,
        name="Financial Stress",
        category=Category.SECONDARY,
        description="TWD/USD spike, TAIEX drop beyond normal volatility",
        poll_group="2hours",
    ),
]}


# ---------------------------------------------------------------------------
# Hysteresis — minimum time (seconds) a state must persist before demotion
# ---------------------------------------------------------------------------

HYSTERESIS_SECONDS = {
    AlertState.YELLOW: 4 * 3600,      # Yellow → Green: 4 hours
    AlertState.AMBER:  6 * 3600,      # Amber  → Yellow: 6 hours
    AlertState.RED:   12 * 3600,      # Red    → Amber: 12 hours
}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.jsonl")
WEB_STATE_FILE = os.path.join(BASE_DIR, "web", "state.json")
BASELINE_FILE = os.path.join(DATA_DIR, "baselines.jsonl")


# ---------------------------------------------------------------------------
# External service config (from environment variables)
# ---------------------------------------------------------------------------

SLACK_WEBHOOK_URL = os.environ.get("TAIWAN_ALERT_SLACK_WEBHOOK", "")
APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN", "")
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")

# Anthropic API key for the LLM adjudicator (Claude Haiku). Optional —
# absence degrades gracefully: WEAK-only matches return "undetermined".
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Apify cost cap per run (USD)
APIFY_MAX_CHARGE_USD = float(os.environ.get("APIFY_MAX_CHARGE_USD", "0.50"))

# Optional NOTAM API — user plugs in their own service (ICAO dataservices,
# Notamify, FAA external-api, etc.). Use {locations} placeholder for ICAO IDs.
NOTAM_API_URL = os.environ.get("NOTAM_API_URL", "")
NOTAM_API_TOKEN = os.environ.get("NOTAM_API_TOKEN", "")

# Alert promotion threshold — minimum active indicator count required at each
# promotion step. Default 2 preserves the original gated logic:
#   YELLOW: 1 primary OR >= threshold secondaries
#   AMBER:  1 primary + total active >= threshold
#   RED:    >= threshold primaries
# Higher = dampened. Lower = hair-trigger.
ALERT_THRESHOLD = int(os.environ.get("TAIWAN_ALERT_THRESHOLD", "2"))
