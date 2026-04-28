"""
Collector: Military indicators (1: Force Concentration, 2: Logistics & Mobilization)

Also feeds: 8 (Allied Response) and 11 (OSINT Chatter, folded into context)

Data sources:
  - Apify Twitter scraper for curated OSINT accounts (daily 9AM TPE)
  - Taiwan MND PLA activity page
  - Japan MOD/Joint Staff press releases

Indicator 1 (Force Concentration) keywords:
  Ship concentration, fleet deployment, naval staging, aircraft forward deploy,
  missile repositioning, TEL movement, PLARF

Indicator 2 (Logistics & Mobilization) keywords:
  Fuel staging, ammunition, hospital activation, blood drive, reserve call-up,
  civilian ferry requisition, rail military transport, mobilization order,
  RO-RO ships, logistics surge
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error

from collectors.base import (
    fetch_url, fetch_rss, keyword_match, assign_confidence,
    make_reading, safe_collect, now_iso,
)
from config import APIFY_API_TOKEN, APIFY_MAX_CHARGE_USD

log = logging.getLogger(__name__)

# Curated OSINT accounts on X
OSINT_ACCOUNTS = [
    "detresfa_",
    "COUPSURE",
    "IndoPac_Info",
    "RupsNair",
    "MT_Anderson",
    "coalitionSAS",
    "OAlexanderDK",
    "sentdefender",
    "Faytuks",
    "Nfrayer",
    "OSABORNINGCN",
    "PLaboringCN",
]

# Taiwan/PRC-specific context — OSINT accounts post about every conflict in the world,
# so generic terms like "aircraft carrier" must co-occur with one of these to count.
# Local Taiwan/Japan gov sources are already bounded by URL, so this filter only
# applies to the OSINT tweet stream.
TAIWAN_CONTEXT_PATTERNS = [
    "taiwan", "taipei", "formosa", "fujian", "guangdong", "kaohsiung",
    "cross-strait", "cross strait", "strait of taiwan", "taiwan strait",
    " pla ", " plan ", " plaaf ", "plarf", "pla navy", "pla air",
    "indo-pacific command", "indopacom", "tpe ", "rocaf",
]


def _taiwan_relevant(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in TAIWAN_CONTEXT_PATTERNS)


FORCE_KEYWORDS = [
    "pla navy", "plan fleet", "amphibious", "landing ship", "lst",
    "fujian port", "guangdong port", "naval staging", "ship concentration",
    "carrier strike", "aircraft carrier", "forward deploy", "fighter deploy",
    "missile repositioning", "tel movement", "plarf", "df-", "rocket force",
    "joint exercise fujian", "combat readiness patrol",
]

LOGISTICS_KEYWORDS = [
    "fuel staging", "ammunition", "ammo movement", "hospital activation",
    "blood drive", "blood donation military", "reserve call-up", "reservist",
    "mobilization order", "civilian ferry", "ro-ro ship", "rail military",
    "logistics surge", "transport requisition", "militia mobilization",
    "strategic reserve", "war mobilization", "military conscription",
]

ALLIED_KEYWORDS = [
    "carrier strike group taiwan", "taiwan strait transit",
    "surge deploy western pacific", "reposition to taiwan",
    "japan sdf alert", "jsdf scramble record",
    "p-8 poseidon taiwan", "guam surge deploy",
    "indopacom taiwan contingency",
]

# Taiwan MND daily PLA activity report
MND_PLA_URL = "https://www.mnd.gov.tw/PublishTable.aspx?Types=%E5%8D%B3%E6%99%82%E8%BB%8D%E4%BA%8B%E5%8B%95%E6%85%8B&title=%E5%9C%8B%E9%98%B2%E6%B6%88%E6%81%AF"

# Japan MOD Maritime SDF press releases (English)
JAPAN_MOD_URL = "https://www.mod.go.jp/msdf/en/release/"


@safe_collect
def collect() -> list:
    readings = []
    all_force_hits = []
    all_logistics_hits = []
    all_allied_hits = []
    source_count = 0

    # --- Source 1: Apify Twitter scraper ---
    osint_texts = _fetch_osint_tweets()
    if osint_texts is not None:
        source_count += 1
        for text in osint_texts:
            if not _taiwan_relevant(text):
                continue
            all_force_hits.extend(keyword_match(text, FORCE_KEYWORDS))
            all_logistics_hits.extend(keyword_match(text, LOGISTICS_KEYWORDS))
            all_allied_hits.extend(keyword_match(text, ALLIED_KEYWORDS))

    # --- Source 2: Taiwan MND PLA activity page ---
    mnd_text = fetch_url(MND_PLA_URL, verify_ssl=False)
    mnd_healthy = mnd_text is not None
    if mnd_text:
        source_count += 1
        all_force_hits.extend(keyword_match(mnd_text, FORCE_KEYWORDS))

    # --- Source 3: Japan MOD MSDF press releases ---
    japan_text = fetch_url(JAPAN_MOD_URL, verify_ssl=False)
    if japan_text:
        source_count += 1
        all_force_hits.extend(keyword_match(japan_text, FORCE_KEYWORDS))
        all_allied_hits.extend(keyword_match(japan_text, ALLIED_KEYWORDS))

    # --- Build source status for honest reporting ---
    sources_checked = []
    sources_failed = []
    if osint_texts is not None:
        sources_checked.append("X/OSINT accounts")
    elif not APIFY_API_TOKEN:
        sources_failed.append("X/OSINT (Apify token not configured)")
    else:
        sources_failed.append("X/OSINT (Apify fetch failed)")
    if mnd_text:
        sources_checked.append("Taiwan MND")
    else:
        sources_failed.append("Taiwan MND (unreachable)")
    if japan_text:
        sources_checked.append("Japan MOD")
    else:
        sources_failed.append("Japan MOD (blocked)")

    checked_str = ", ".join(sources_checked) if sources_checked else "none"
    failed_str = f" Failed: {', '.join(sources_failed)}." if sources_failed else ""

    # --- Indicator 1: Force Concentration ---
    force_active = len(all_force_hits) >= 3
    if source_count == 0:
        force_summary = f"Could not check — all sources failed.{failed_str}"
    elif force_active:
        force_summary = f"Checked {checked_str}. Force buildup signals: {', '.join(sorted(set(all_force_hits))[:5])}.{failed_str}"
    else:
        force_summary = f"Checked {checked_str} for ship/aircraft/missile repositioning. No unusual concentration.{failed_str}"

    readings.append(make_reading(
        indicator_id=1,
        active=force_active,
        confidence=assign_confidence(len(all_force_hits), source_count),
        summary=force_summary,
        feed_healthy=source_count > 0,
    ))

    # --- Indicator 2: Logistics & Mobilization ---
    logistics_active = len(all_logistics_hits) >= 2
    if osint_texts is None:
        logistics_summary = f"Could not check — X/OSINT is the primary source for logistics signals.{failed_str}"
    elif logistics_active:
        logistics_summary = f"Checked {checked_str}. Mobilization signals: {', '.join(sorted(set(all_logistics_hits))[:5])}."
    else:
        logistics_summary = f"Checked {checked_str} for fuel staging, ammo movement, reserve call-ups, transport requisitions. None detected."

    readings.append(make_reading(
        indicator_id=2,
        active=logistics_active,
        confidence=assign_confidence(len(all_logistics_hits), source_count),
        summary=logistics_summary,
        feed_healthy=osint_texts is not None,
    ))

    # --- Indicator 8: Allied Response ---
    allied_active = len(all_allied_hits) >= 2
    if source_count == 0:
        allied_summary = f"Could not check — all sources failed.{failed_str}"
    elif allied_active:
        allied_summary = f"Checked {checked_str}. Allied repositioning signals: {', '.join(sorted(set(all_allied_hits))[:5])}."
    else:
        allied_summary = f"Checked {checked_str} for US/Japan military repositioning. No unusual posture changes."

    readings.append(make_reading(
        indicator_id=8,
        active=allied_active,
        confidence=assign_confidence(len(all_allied_hits), source_count),
        summary=allied_summary,
        feed_healthy=source_count > 0,
    ))

    return readings


def _fetch_osint_tweets() -> list[str] | None:
    """Fetch recent tweets from curated OSINT accounts via Apify."""
    if not APIFY_API_TOKEN:
        log.warning("APIFY_API_TOKEN not set — skipping X/OSINT collection")
        return None

    # Use Apify Twitter scraper actor
    actor_id = "apidojo~tweet-scraper"
    api_url = f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"

    payload = json.dumps({
        "handles": OSINT_ACCOUNTS,
        "tweetsDesired": 5,  # per account
        "maxTotalChargeUsd": APIFY_MAX_CHARGE_USD,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{api_url}?token={APIFY_API_TOKEN}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            items = json.loads(resp.read().decode("utf-8"))
            return [
                f"{item.get('full_text', '')} {item.get('text', '')}"
                for item in items
                if isinstance(item, dict)
            ]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log.error("Apify tweet fetch failed: %s", e)
        return None
