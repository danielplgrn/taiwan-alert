"""
Collector: Airspace Control (3) and Maritime Control (4)

Data sources:
  - Optional user-configured NOTAM API (NOTAM_API_URL + NOTAM_API_TOKEN)
  - China MSA maritime safety notices
  - OpenSky Network (flight density over Taiwan Strait)

Note on NOTAMs: The FAA DINS public pages block non-browser clients (403).
All free no-auth NOTAM aggregators either require signup (ICAO dataservices,
Notamify, FAA external-api) or don't cover Fujian/Xiamen FIRs. Set
NOTAM_API_URL and NOTAM_API_TOKEN in .env to enable NOTAM checks; otherwise
airspace indicator relies on OpenSky flight density anomalies alone.
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error

from collectors.base import (
    fetch_url, keyword_match, assign_confidence,
    make_reading, safe_collect, now_iso,
)
from config import NOTAM_API_URL, NOTAM_API_TOKEN
from analysis.opensky_baseline import (
    record_sample, check_low_anomaly, _taipei_hour_now,
)

log = logging.getLogger(__name__)

# --- NOTAM keywords indicating military restriction ---
AIRSPACE_KEYWORDS = [
    "danger area", "restricted area", "prohibited area",
    "military exercise", "missile firing", "live firing",
    "temporary flight restriction", "no-fly", "closed to civil",
    "fujian", "xiamen", "fuzhou", "pingtan", "taiwan strait",
]

# China MSA — English safety information page
CHINA_MSA_URL = "https://en.msa.gov.cn/"

MARITIME_KEYWORDS = [
    "military exercise", "live firing", "missile", "exclusion zone",
    "prohibited area", "navigation warning", "shipping restriction",
    "taiwan strait", "fujian", "east china sea",
    "fishing ban", "fishing moratorium", "vessels prohibited",
]

# ICAO locations covering Taiwan Strait + Fujian. Passed to the user-configured
# NOTAM_API_URL via a {locations} placeholder. If the user's chosen service uses
# different query-string keys, edit NOTAM_API_URL directly to match.
NOTAM_ICAO_LOCATIONS = "ZSFZ,ZSAM,RCAA,RCTP"

# OpenSky bounding box for Taiwan Strait region
# lat: 22-27, lon: 117-122
OPENSKY_STRAIT_URL = "https://opensky-network.org/api/states/all?lamin=22&lomin=117&lamax=27&lomax=122"


@safe_collect
def collect() -> list:
    readings = []

    # --- Airspace: NOTAMs (optional) ---
    notam_hits, notam_healthy, notam_skipped = _check_notams()

    # --- Airspace: OpenSky flight density ---
    # Diurnal-aware MAD anomaly check (analysis/opensky_baseline). Replaces the
    # old hard-threshold (`< 20`) which falsely fired during the early-morning
    # Taipei civil-aviation trough.
    flight_count, opensky_healthy = _check_opensky_flights()
    flight_anomaly_status = "unknown"
    flight_anomaly_explanation = ""
    flight_anomaly_active = False
    flight_anomaly_high = False
    if flight_count is not None:
        # Always record the sample so the baseline grows even on quiet days
        try:
            record_sample(flight_count)
        except Exception as e:
            log.warning("Failed to record OpenSky baseline sample: %s", e)
        anomaly = check_low_anomaly(flight_count)
        flight_anomaly_status = anomaly.status
        flight_anomaly_explanation = anomaly.explanation
        flight_anomaly_active = anomaly.status in ("low_anomaly", "high_low_anomaly")
        flight_anomaly_high = anomaly.status == "high_low_anomaly"

    airspace_active = len(notam_hits) >= 2 or flight_anomaly_active
    airspace_details = []
    if notam_hits:
        airspace_details.append(f"NOTAMs: {', '.join(sorted(set(notam_hits))[:3])}")
    if flight_anomaly_active:
        airspace_details.append(flight_anomaly_explanation)

    # --- Honest reporting ---
    air_sources_checked = []
    air_sources_failed = []
    if notam_healthy:
        air_sources_checked.append("NOTAMs")
    elif notam_skipped:
        air_sources_failed.append("NOTAMs (NOTAM_API_URL not configured)")
    else:
        air_sources_failed.append("NOTAMs (API error)")
    if opensky_healthy:
        if flight_anomaly_active:
            air_sources_checked.append(
                f"OpenSky ({flight_count} flights, hour {_taipei_hour_now()} Taipei)"
            )
        else:
            # Surface bootstrap status / normal explanation when not firing
            air_sources_checked.append(
                f"OpenSky ({flight_count} flights, {flight_anomaly_status})"
            )
    else:
        air_sources_failed.append("OpenSky (unreachable)")

    air_checked = ", ".join(air_sources_checked) if air_sources_checked else "none"
    air_failed = f" Failed: {', '.join(air_sources_failed)}." if air_sources_failed else ""

    if not notam_healthy and not opensky_healthy:
        air_summary = f"Could not check — all sources failed.{air_failed}"
    elif airspace_active:
        air_summary = f"Checked {air_checked}. Anomaly detected: {' | '.join(airspace_details)}.{air_failed}"
    else:
        air_summary = f"Checked {air_checked}. No military closures or flight rerouting anomalies.{air_failed}"

    # Evidence classification:
    #   - NOTAM hits = "concrete" (a published airspace closure is an admin act)
    #   - flight density anomaly only = "anomaly" (quantitative deviation)
    #   - both = "concrete" (the stronger signal wins)
    if notam_hits:
        airspace_evidence = "concrete"
    elif flight_anomaly_active:
        airspace_evidence = "anomaly"
    else:
        airspace_evidence = "keyword"

    # NOTAM is optional; feed is considered healthy as long as OpenSky is up.
    readings.append(make_reading(
        indicator_id=3,
        active=airspace_active,
        confidence=assign_confidence(len(notam_hits) + (1 if flight_anomaly_active else 0)),
        summary=air_summary,
        feed_healthy=opensky_healthy or notam_healthy,
        evidence_class=airspace_evidence,
    ))

    # --- Maritime: China MSA ---
    msa_hits, msa_healthy = _check_china_msa()

    maritime_active = len(msa_hits) >= 2
    if not msa_healthy:
        msa_summary = "Could not check — China MSA site unreachable."
    elif maritime_active:
        msa_summary = f"Checked China MSA. Restriction signals: {', '.join(sorted(set(msa_hits))[:3])}."
    else:
        msa_summary = "Checked China Maritime Safety Administration for exclusion zones, shipping restrictions, fishing fleet recalls. None found."

    readings.append(make_reading(
        indicator_id=4,
        active=maritime_active,
        confidence=assign_confidence(len(msa_hits)),
        summary=msa_summary,
        feed_healthy=msa_healthy,
        # MSA exclusion zones / shipping restrictions are admin acts
        evidence_class="concrete" if maritime_active else "keyword",
    ))

    return readings


def _check_notams() -> tuple[list[str], bool, bool]:
    """
    Check NOTAMs via user-configured API.

    Returns (hits, healthy, skipped). skipped=True means no NOTAM_API_URL
    configured — not a failure, just not enabled.
    """
    if not NOTAM_API_URL:
        return [], False, True

    url = NOTAM_API_URL.replace("{locations}", NOTAM_ICAO_LOCATIONS)
    headers = {"User-Agent": "TaiwanAlertBot/1.0", "Accept": "application/json"}
    if NOTAM_API_TOKEN:
        headers["Authorization"] = f"Bearer {NOTAM_API_TOKEN}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        return keyword_match(text, AIRSPACE_KEYWORDS), True, False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("NOTAM fetch failed: %s", e)
        return [], False, False


def _check_china_msa() -> tuple[list[str], bool]:
    """Check China Maritime Safety Administration notices."""
    text = fetch_url(CHINA_MSA_URL, verify_ssl=False)
    if text is None:
        return [], False
    hits = keyword_match(text, MARITIME_KEYWORDS)
    return hits, True


def _check_opensky_flights() -> tuple[int | None, bool]:
    """Count flights currently visible in Taiwan Strait area via OpenSky."""
    try:
        req = urllib.request.Request(
            OPENSKY_STRAIT_URL,
            headers={"User-Agent": "TaiwanAlertBot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            states = data.get("states") or []
            return len(states), True
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        log.warning("OpenSky fetch failed: %s", e)
        return None, False
