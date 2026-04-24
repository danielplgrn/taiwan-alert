"""
Collector: Allied Response (indicator 8)

Note: most allied response data comes from the military.py collector
(OSINT accounts). This module adds structured sources:
  - USNI Fleet Tracker (weekly editorial — context only)
  - Japan MOD press releases

If military.py already produced a reading for indicator 8, this collector's
reading will be merged in collect.py (latest wins, or OR-merge on active).
"""

from __future__ import annotations

import logging

from collectors.base import (
    fetch_url, fetch_rss, keyword_match, assign_confidence,
    make_reading, safe_collect,
)

log = logging.getLogger(__name__)

USNI_FLEET_TRACKER_URL = "https://news.usni.org/category/fleet-tracker"
JAPAN_MOD_NEWS_URL = "https://www.mod.go.jp/msdf/en/release/"

# More specific keywords — generic terms like "deploy", "china", "exercise"
# always appear in routine USNI/Japan MOD reporting and cause false positives
ALLIED_POSTURE_KEYWORDS = [
    "taiwan strait transit", "taiwan contingency", "taiwan readiness",
    "forward deploy to western pacific", "surge deploy",
    "carrier strike group taiwan", "reposition western pacific",
    "japan defense alert", "japan sdf scramble china",
    "defense readiness condition", "defcon",
    "unusual military posture", "heightened alert",
]


@safe_collect
def collect() -> list:
    all_hits = []
    source_count = 0
    any_healthy = False

    # --- USNI Fleet Tracker ---
    usni_text = fetch_url(USNI_FLEET_TRACKER_URL)
    if usni_text:
        any_healthy = True
        source_count += 1
        all_hits.extend(keyword_match(usni_text, ALLIED_POSTURE_KEYWORDS))

    # --- Japan MOD ---
    japan_text = fetch_url(JAPAN_MOD_NEWS_URL, verify_ssl=False)
    if japan_text:
        any_healthy = True
        source_count += 1
        all_hits.extend(keyword_match(japan_text, ALLIED_POSTURE_KEYWORDS))

    # --- Honest reporting ---
    al_checked = []
    al_failed = []
    if usni_text:
        al_checked.append("USNI Fleet Tracker")
    else:
        al_failed.append("USNI Fleet Tracker (unreachable)")
    if japan_text:
        al_checked.append("Japan MOD")
    else:
        al_failed.append("Japan MOD (blocked)")

    checked_str = ", ".join(al_checked) if al_checked else "none"
    failed_str = f" Failed: {', '.join(al_failed)}." if al_failed else ""

    active = len(all_hits) >= 2
    if source_count == 0:
        al_summary = f"Could not check — all sources failed.{failed_str}"
    elif active:
        al_summary = f"Checked {checked_str}. Posture change signals: {', '.join(sorted(set(all_hits))[:5])}.{failed_str}"
    else:
        al_summary = f"Checked {checked_str} for US/Japan military repositioning. Normal posture.{failed_str}"

    return [make_reading(
        indicator_id=8,
        active=active,
        confidence=assign_confidence(len(all_hits), source_count),
        summary=al_summary,
        feed_healthy=source_count > 0,
    )]
