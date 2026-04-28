"""
Collector: Diplomatic Signals (indicator 7)

Data sources:
  - US State Department Taiwan travel advisory
  - UK FCDO Taiwan travel advice
  - Japan MFA Taiwan safety info
  - Australia DFAT Smartraveller Taiwan

Detection approach: check for ACTIVE high-level advisories, not just
the presence of keywords (many pages describe all levels as reference).
"""

from __future__ import annotations

import logging
import re

from collectors.base import (
    fetch_url, keyword_match, assign_confidence,
    make_reading, safe_collect,
)

log = logging.getLogger(__name__)


@safe_collect
def collect() -> list:
    escalations = []
    source_count = 0
    any_healthy = False

    # --- US State Department ---
    us_text = fetch_url(
        "https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories/taiwan-travel-advisory.html",
        timeout=15,
    )
    if us_text:
        any_healthy = True
        source_count += 1
        # US advisories put the level in a prominent heading like "Level 4: Do Not Travel"
        # Current normal for Taiwan: Level 1 or Level 2
        if re.search(r"Level\s*4", us_text, re.IGNORECASE):
            escalations.append("US: Level 4 Do Not Travel")
        elif re.search(r"Level\s*3", us_text, re.IGNORECASE):
            escalations.append("US: Level 3 Reconsider Travel")

    # --- UK FCDO ---
    uk_text = fetch_url("https://www.gov.uk/foreign-travel-advice/taiwan", timeout=15)
    if uk_text:
        any_healthy = True
        source_count += 1
        # UK uses "advise against all travel" or "advise against all but essential travel"
        uk_lower = uk_text.lower()
        if "advise against all travel" in uk_lower and "but essential" not in uk_lower:
            escalations.append("UK: Advise against ALL travel")
        elif "advise against all but essential travel" in uk_lower:
            escalations.append("UK: Advise against all but essential travel")

    # --- Japan MFA ---
    # Japan's page for Taiwan describes all levels as reference text.
    # We need to check the ACTUAL current level assigned to Taiwan.
    # The current level is shown in a specific section. Look for the active designation.
    jp_text = fetch_url(
        "https://www.anzen.mofa.go.jp/info/pcinfectionspothazardinfo_004.html",
        timeout=15,
    )
    if jp_text:
        any_healthy = True
        source_count += 1
        # Japan MFA puts active level in a colored badge/class near top of page
        # Level 1 = 十分注意 (exercise caution) — normal for Taiwan
        # Level 3 = 渡航中止勧告 (avoid travel) — escalation
        # Level 4 = 退避勧告 (evacuate) — critical
        # Check for active level indicators (colored level badges, not description text)
        if re.search(r'class="[^"]*level4[^"]*"', jp_text, re.IGNORECASE):
            escalations.append("Japan: Level 4 (Evacuate)")
        elif re.search(r'class="[^"]*level3[^"]*"', jp_text, re.IGNORECASE):
            escalations.append("Japan: Level 3 (Avoid travel)")
        # Also check for sudden content change mentioning Taiwan evacuation
        elif keyword_match(jp_text, ["台湾　退避", "台湾　渡航中止"]):
            escalations.append("Japan: Taiwan evacuation advisory detected")

    # --- Australia DFAT ---
    au_text = fetch_url("https://www.smartraveller.gov.au/destinations/asia/taiwan", timeout=15)
    if au_text:
        any_healthy = True
        source_count += 1
        au_lower = au_text.lower()
        if "do not travel" in au_lower:
            escalations.append("Australia: Do Not Travel")
        elif "reconsider your need to travel" in au_lower:
            escalations.append("Australia: Reconsider Travel")

    active = len(escalations) >= 1
    return [make_reading(
        indicator_id=7,
        active=active,
        confidence=assign_confidence(len(escalations), source_count),
        summary=f"Checked US, UK, Japan, Australia travel advisories. Escalation detected: {' | '.join(escalations)}" if active else f"Checked US, UK, Japan, Australia travel advisories for Taiwan. All at normal levels ({source_count} of 4 sources reachable).",
        feed_healthy=any_healthy,
        # Travel-advisory level changes are administrative acts published by
        # the issuing government; treat as concrete evidence when active.
        evidence_class="concrete" if active else "keyword",
    )]
