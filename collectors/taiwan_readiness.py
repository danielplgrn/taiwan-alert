"""
Collector: Taiwan Domestic Readiness (indicator 5)

Data sources:
  - Taiwan MND press releases / news channel
  - Focus Taiwan (CNA English) RSS
"""

from __future__ import annotations

import logging

from collectors.base import (
    fetch_url, fetch_rss, keyword_match, assign_confidence,
    make_reading, safe_collect,
)

log = logging.getLogger(__name__)

MND_NEWS_URL = "https://www.mnd.gov.tw/PublishTable.aspx?Types=%E5%8D%B3%E6%99%82%E8%BB%8D%E4%BA%8B%E5%8B%95%E6%85%8B&title=%E5%9C%8B%E9%98%B2%E6%B6%88%E6%81%AF"
FOCUS_TAIWAN_RSS = "https://feeds.feedburner.com/rsscna/engnews/"

READINESS_KEYWORDS = [
    "combat readiness", "alert level", "raise alert", "heightened alert",
    "leave cancelled", "leave cancellation", "cancel leave",
    "reserve activation", "reserve mobilization", "reserve call-up",
    "civil defense", "air raid drill", "emergency mobilization",
    "war readiness", "defense readiness", "combat alert",
    "national emergency", "martial law", "emergency decree",
]


@safe_collect
def collect() -> list:
    all_hits = []
    source_count = 0

    # --- Taiwan MND ---
    mnd_text = fetch_url(MND_NEWS_URL, verify_ssl=False)
    mnd_healthy = mnd_text is not None
    if mnd_text:
        source_count += 1
        all_hits.extend(keyword_match(mnd_text, READINESS_KEYWORDS))

    # --- Focus Taiwan RSS ---
    ft_items = fetch_rss(FOCUS_TAIWAN_RSS)
    if ft_items:
        source_count += 1
        for item in ft_items[:20]:
            text = f"{item['title']} {item['summary']}"
            all_hits.extend(keyword_match(text, READINESS_KEYWORDS))

    # --- Honest reporting ---
    tw_checked = []
    tw_failed = []
    if mnd_text:
        tw_checked.append("Taiwan MND")
    else:
        tw_failed.append("Taiwan MND (unreachable)")
    if ft_items:
        tw_checked.append("Focus Taiwan")
    else:
        tw_failed.append("Focus Taiwan (RSS unreachable)")

    checked_str = ", ".join(tw_checked) if tw_checked else "none"
    failed_str = f" Failed: {', '.join(tw_failed)}." if tw_failed else ""

    active = len(all_hits) >= 2
    if source_count == 0:
        tw_summary = f"Could not check — all sources failed.{failed_str}"
    elif active:
        tw_summary = f"Checked {checked_str}. Readiness keywords found: {', '.join(sorted(set(all_hits))[:5])}.{failed_str}"
    else:
        tw_summary = f"Checked {checked_str} for alert-level changes, leave cancellations, reserve activations. None found.{failed_str}"

    return [make_reading(
        indicator_id=5,
        active=active,
        confidence=assign_confidence(len(all_hits), source_count),
        summary=tw_summary,
        feed_healthy=source_count > 0,
        # READINESS_KEYWORDS are tightly curated to categorical-escalation
        # admin acts (alert level, leave cancellation, reserve activation,
        # martial law). Treat hits as concrete signals.
        evidence_class="concrete" if active else "keyword",
    )]
