"""
Collector: Rhetoric & Political Pressure (indicator 9)

Also covers sub-indicators for political takeover scenario (12a-d),
but these are folded into indicator 9 since indicator 12 was merged
into the rhetoric/political category.

Data sources:
  - Xinhua English RSS
  - Global Times RSS
  - China MFA spokesperson transcripts (via RSS/scrape)
  - CNA (Taiwan) RSS — for domestic political crisis signals
  - Taipei Times RSS
"""

from __future__ import annotations

import logging

from collectors.base import (
    fetch_rss, keyword_match, assign_confidence,
    make_reading, safe_collect,
)

log = logging.getLogger(__name__)

# RSS feeds
FEEDS = {
    "xinhua": "http://www.news.cn/english/rss/worldrss.xml",
    "globaltimes": "https://www.globaltimes.cn/rss/outbrain.xml",
    "cna": "https://feeds.feedburner.com/rsscna/engnews/",
    "taipeitimes": "https://www.taipeitimes.com/xml/index.rss",
}

# Rhetoric shift: deterrence → action language
RHETORIC_ESCALATION_KEYWORDS = [
    "will take action", "will not hesitate", "will use force",
    "take all necessary measures", "smash any attempt",
    "never allow", "reunification by force", "non-peaceful means",
    "crush independence", "red line crossed", "severe consequences",
    "war preparation", "military operation", "combat ready",
    "separatist forces", "foreign interference will fail",
]

# Political takeover / crisis keywords
POLITICAL_CRISIS_KEYWORDS = [
    "one country two systems", "reunification achieved",
    "return to motherland", "one china restored",
    "peaceful reunification", "reunification inevitable",
    "emergency decree", "martial law", "emergency session",
    "national mobilization", "inspection regime",
    "customs blockade", "quarantine zone",
    "economic sanctions against taiwan",
]

# Celebratory / fait accompli framing (most alarming)
FAIT_ACCOMPLI_KEYWORDS = [
    "reunification achieved", "return to motherland",
    "one china restored", "historic reunification",
    "taiwan returned", "reunification completed",
]


@safe_collect
def collect() -> list:
    rhetoric_hits = []
    political_hits = []
    fait_accompli_hits = []
    source_count = 0
    any_healthy = False

    for name, url in FEEDS.items():
        items = fetch_rss(url, verify_ssl=False)
        if not items:
            continue

        any_healthy = True
        source_count += 1

        for item in items[:15]:
            text = f"{item['title']} {item['summary']}"
            rhetoric_hits.extend(keyword_match(text, RHETORIC_ESCALATION_KEYWORDS))
            political_hits.extend(keyword_match(text, POLITICAL_CRISIS_KEYWORDS))
            fait_accompli_hits.extend(keyword_match(text, FAIT_ACCOMPLI_KEYWORDS))

    total_hits = len(rhetoric_hits) + len(political_hits)
    active = total_hits >= 3 or len(fait_accompli_hits) >= 1

    details = []
    if rhetoric_hits:
        details.append(f"Rhetoric: {', '.join(set(rhetoric_hits)[:3])}")
    if political_hits:
        details.append(f"Political: {', '.join(set(political_hits)[:3])}")
    if fait_accompli_hits:
        details.append(f"FAIT ACCOMPLI framing: {', '.join(set(fait_accompli_hits)[:3])}")

    return [make_reading(
        indicator_id=9,
        active=active,
        confidence=assign_confidence(total_hits, source_count),
        summary=f"Checked Xinhua, Global Times, Focus Taiwan, Taipei Times RSS feeds. Escalation detected: {' | '.join(details)}" if active else f"Checked Xinhua, Global Times, Focus Taiwan, Taipei Times for rhetoric shifts or political crisis keywords. Normal levels ({source_count} of 4 feeds reachable).",
        feed_healthy=any_healthy,
    )]
