"""
Collector: Cyber & Infrastructure (indicator 6)

Data sources:
  - TWCERT/CC (Taiwan CERT) RSS / news
  - Cloudflare Radar (outage detection for Taiwan)
  - IODA (Internet Outage Detection and Analysis)
  - GPSJam (GNSS interference — daily only)
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error

from collectors.base import (
    fetch_url, fetch_rss, keyword_match, assign_confidence,
    make_reading, safe_collect,
)
from config import CLOUDFLARE_API_TOKEN

log = logging.getLogger(__name__)

TWCERT_RSS = "https://www.twcert.org.tw/tw/rss-132-1.xml"

# Cloudflare Radar — Taiwan country code. dateRange is required.
CLOUDFLARE_RADAR_URL = "https://api.cloudflare.com/client/v4/radar/annotations/outages?location=TW&dateRange=1d&limit=5"

# IODA — Taiwan internet outage alerts. API requires from/until unix timestamps.
IODA_BASE_URL = "https://api.ioda.inetintel.cc.gatech.edu/v2/outages/alerts"
IODA_WINDOW_SECONDS = 24 * 3600

CYBER_KEYWORDS = [
    "ddos", "cyberattack", "cyber attack", "data breach", "ransomware",
    "malware", "critical infrastructure", "government hack", "military hack",
    "telecom disruption", "banking system", "financial system attack",
    "power grid", "water system", "transportation system",
]

DESTRUCTIVE_KEYWORDS = [
    "critical infrastructure down", "nationwide outage", "banking offline",
    "power grid attack", "telecom down", "cable cut", "submarine cable",
    "gnss jamming", "gps interference", "coordinated attack",
]


@safe_collect
def collect() -> list:
    all_hits = []
    # Two distinct categories — was previously conflated as `destructive_hits`,
    # which let any single outage source flip the indicator destructive and
    # promote it from secondary to primary. That produced a false-positive RED
    # on a lone IODA blip + generic "ransomware" keyword. Now:
    #
    #   specific_destructive — explicit Taiwan-relevant destructive keywords
    #     (cable cut, GNSS jamming, power grid, telecom down, etc.). One hit
    #     is sufficient for is_destructive.
    #
    #   outage_sources — count of independent monitors flagging an outage
    #     (TWCERT keyword, IODA, Cloudflare Radar). At least 2 independent
    #     outage sources are needed before is_destructive fires on outage
    #     alone, since a single ISP/BGP blip should not signal "destructive
    #     Taiwan-targeted attack."
    specific_destructive: list[str] = []
    outage_sources: list[str] = []
    source_count = 0
    any_healthy = False

    # --- TWCERT RSS ---
    twcert_items = fetch_rss(TWCERT_RSS, verify_ssl=False)
    if twcert_items:
        any_healthy = True
        source_count += 1
        twcert_destructive = []
        for item in twcert_items[:10]:
            text = f"{item['title']} {item['summary']}"
            all_hits.extend(keyword_match(text, CYBER_KEYWORDS))
            twcert_destructive.extend(keyword_match(text, DESTRUCTIVE_KEYWORDS))
        if twcert_destructive:
            specific_destructive.extend(twcert_destructive)
            outage_sources.append("TWCERT")

    # --- Cloudflare Radar ---
    cf_outage, cf_healthy = _check_cloudflare()
    if cf_healthy:
        any_healthy = True
        source_count += 1
    if cf_outage:
        all_hits.append("cloudflare_outage_detected")
        outage_sources.append("Cloudflare")

    # --- IODA ---
    ioda_outage, ioda_healthy = _check_ioda()
    if ioda_healthy:
        any_healthy = True
        source_count += 1
    if ioda_outage:
        all_hits.append("ioda_outage_detected")
        outage_sources.append("IODA")

    # --- Honest reporting ---
    cyber_checked = []
    cyber_failed = []
    if twcert_items:
        cyber_checked.append("TWCERT")
    else:
        cyber_failed.append("TWCERT (RSS unreachable)")
    if cf_healthy:
        cyber_checked.append("Cloudflare Radar")
    elif not CLOUDFLARE_API_TOKEN:
        cyber_failed.append("Cloudflare Radar (API token not configured)")
    else:
        cyber_failed.append("Cloudflare Radar (API error)")
    if ioda_healthy:
        cyber_checked.append("IODA")
    else:
        cyber_failed.append("IODA (API error)")

    checked_str = ", ".join(cyber_checked) if cyber_checked else "none"
    failed_str = f" Failed: {', '.join(cyber_failed)}." if cyber_failed else ""

    active = len(all_hits) >= 2
    has_specific_destructive = len(specific_destructive) >= 1
    has_corroborated_outage = len(set(outage_sources)) >= 2
    is_destructive = has_specific_destructive or has_corroborated_outage

    if source_count == 0:
        cyber_summary = f"Could not check — all sources failed.{failed_str}"
    elif active:
        details = []
        if all_hits:
            details.append(f"Signals: {', '.join(sorted(set(all_hits))[:5])}")
        if is_destructive:
            if has_specific_destructive:
                kw_preview = ", ".join(sorted(set(specific_destructive))[:3])
                details.append(
                    f"DESTRUCTIVE — escalated to Primary "
                    f"(Taiwan-targeted keywords: {kw_preview})"
                )
            else:
                details.append(
                    f"DESTRUCTIVE — escalated to Primary "
                    f"(corroborated outage across {len(set(outage_sources))} sources: "
                    f"{', '.join(sorted(set(outage_sources)))})"
                )
        elif outage_sources:
            details.append(
                f"Outage on {sorted(set(outage_sources))[0]} only — "
                f"insufficient corroboration to mark destructive"
            )
        cyber_summary = f"Checked {checked_str}. {' | '.join(details)}.{failed_str}"
    else:
        cyber_summary = f"Checked {checked_str} for cyberattacks, internet outages, cable disruptions. None detected.{failed_str}"

    return [make_reading(
        indicator_id=6,
        active=active,
        confidence=assign_confidence(len(all_hits), source_count),
        summary=cyber_summary,
        feed_healthy=any_healthy,
        is_destructive=is_destructive,
        # Concrete only when destructive (specific keywords or corroborated
        # outage). Lone outage sources or generic keyword chatter stay "keyword".
        evidence_class="concrete" if is_destructive else "keyword",
    )]


def _check_cloudflare() -> tuple[bool, bool]:
    """Check Cloudflare Radar for recent Taiwan outages."""
    if not CLOUDFLARE_API_TOKEN:
        log.info("CLOUDFLARE_API_TOKEN not set — skipping Cloudflare Radar")
        return False, False

    req = urllib.request.Request(
        CLOUDFLARE_RADAR_URL,
        headers={
            "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
            "User-Agent": "TaiwanAlertBot/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            annotations = data.get("result", {}).get("annotations", [])
            # If there are recent outage annotations for TW, flag it
            return len(annotations) > 0, True
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log.warning("Cloudflare Radar failed: %s", e)
        return False, False


def _check_ioda() -> tuple[bool, bool]:
    """Check IODA for Taiwan internet outage alerts in the last 24h."""
    now = int(time.time())
    url = (
        f"{IODA_BASE_URL}?entityType=country&entityCode=TW"
        f"&from={now - IODA_WINDOW_SECONDS}&until={now}&limit=10"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TaiwanAlertBot/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            alerts = data.get("data") or []
            return len(alerts) > 0, True
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log.warning("IODA fetch failed: %s", e)
        return False, False
