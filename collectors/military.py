"""
Collector: Military indicators
  #1 Force Concentration
  #2 Logistics & Mobilization
  #8 Allied Response

Pipeline (post-Codex-debate refactor):

    OSINT tweets ──► dedup events ──► Taiwan-context filter ──┐
    Taiwan MND ────────────────────────────────────────────────┤
    Japan MOD ─────────────────────────────────────────────────┤
                                                               │
                       ┌───────────────────────────────────────┘
                       ▼
        ┌──────────────────────────────────────┐
        │ match_strong  → concrete-action hits │  ─► evidence_class="concrete"
        │                  (observed-action +  │      activates indicator immediately
        │                   geography gates)   │
        └──────────────────────────────────────┘
        ┌──────────────────────────────────────┐
        │ match_weak    → vocabulary hits      │
        │                  (sentence-scoped    │
        │                   negative filter)   │
        └──────────────────────────────────────┘
                       ▼
        Source-family corroboration check (≥2 distinct families)
                       ▼
        LLM adjudicator (Claude Haiku) on WEAK-only path
                       ▼
                IndicatorReading

    MND quantitative counts ──► baseline (median + 2*MAD)
                                  ▼
                          evidence_class="anomaly"
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error

from collectors.base import (
    fetch_url, make_reading, safe_collect, now_iso,
)
from collectors.keywords import (
    STRONG_KEYWORDS,
    FORCE_WEAK, LOGISTICS_WEAK, ALLIED_WEAK,
    KeywordHit,
    match_strong, match_weak,
    unique_keywords, hits_by_source_family,
    is_negative_context,
)
from analysis.dedup import dedup_events
from analysis.baseline import (
    parse_mnd_counts, append_baseline, check_anomaly, now_taipei_date,
)
from analysis.llm_adjudicator import (
    adjudicate_weak_signal, WeakMatchSnippet,
)
from config import APIFY_API_TOKEN, APIFY_MAX_CHARGE_USD

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OSINT account tiers
#
# TIER1 = direct observers / known credibility for PLA + cross-strait.
# TIER2 = commentary / aggregator accounts that often retell others' work.
#
# Tiering is gut-feel and curated by hand — there is no public credibility
# ranking we'd trust enough to wire into scoring. Revisit periodically.
# ---------------------------------------------------------------------------

OSINT_TIER1 = ["detresfa_", "IndoPac_Info", "MT_Anderson", "sentdefender"]
OSINT_TIER2 = [
    "COUPSURE", "RupsNair", "coalitionSAS", "OAlexanderDK",
    "Faytuks", "Nfrayer", "OSABORNINGCN", "PLaboringCN",
]
OSINT_ACCOUNTS = OSINT_TIER1 + OSINT_TIER2


# Source families for cross-source corroboration
SOURCE_FAMILY_GOV = "GOV"
SOURCE_FAMILY_OSINT_T1 = "OSINT_TIER1"
SOURCE_FAMILY_OSINT_T2 = "OSINT_TIER2"


def _osint_source_id(handle: str) -> str:
    return f"osint:{handle.lstrip('@').lower()}"


def _build_family_map() -> dict[str, str]:
    """Map source identifier -> family name."""
    family: dict[str, str] = {
        "MND": SOURCE_FAMILY_GOV,
        "Japan MOD": SOURCE_FAMILY_GOV,
        "INDOPACOM": SOURCE_FAMILY_GOV,
    }
    for h in OSINT_TIER1:
        family[_osint_source_id(h)] = SOURCE_FAMILY_OSINT_T1
    for h in OSINT_TIER2:
        family[_osint_source_id(h)] = SOURCE_FAMILY_OSINT_T2
    return family


# ---------------------------------------------------------------------------
# Per-indicator STRONG keyword routing.
# STRONG_KEYWORDS in keywords.py covers many indicators (diplomatic, airspace,
# Taiwan readiness, etc.). We route only the ones relevant to indicators
# 1, 2, 8 here. Others are handled by their respective collectors.
# ---------------------------------------------------------------------------

INDICATOR_1_STRONG = {
    "port closure", "harbor closure",  # geography-gated to PRC ports in keywords.py
}

INDICATOR_2_STRONG = {
    "civilian ferry requisition", "civilian ferries requisitioned",
    "ro-ro ship requisition", "ro-ro requisitioned",
    "civilian vessel commandeered", "merchant fleet mobilized",
    "reserve call-up order", "reservist mobilization order",
    "reserve activation order", "civilian conscription order",
    "militia mobilization order", "general mobilization",
    "blood donation drive military", "blood drive military",
    "mass casualty preparation",
}

# Indicator 8 has no STRONG term mapping — Allied Response signals are
# detected via cross-source WEAK corroboration with adjudication.
INDICATOR_8_STRONG: set[str] = set()


# WEAK keyword thresholds (unique terms required) — escalation rule:
#   ≥1 STRONG keyword from any source, OR
#   ≥3 unique WEAK keywords from ≥2 distinct source families,
#   with negative-context sentences excluded and LLM adjudicator pass.
WEAK_UNIQUE_THRESHOLD = 3
WEAK_FAMILY_THRESHOLD = 2


# ---------------------------------------------------------------------------
# Source URLs
# ---------------------------------------------------------------------------

MND_PLA_URL = (
    "https://www.mnd.gov.tw/PublishTable.aspx"
    "?Types=%E5%8D%B3%E6%99%82%E8%BB%8D%E4%BA%8B%E5%8B%95%E6%85%8B"
    "&title=%E5%9C%8B%E9%98%B2%E6%B6%88%E6%81%AF"
)
JAPAN_MOD_URL = "https://www.mod.go.jp/msdf/en/release/"


# ---------------------------------------------------------------------------
# OSINT fetch — Apify tweet-scraper
# ---------------------------------------------------------------------------

def _fetch_osint_tweets() -> list[dict] | None:
    """
    Fetch recent tweets from curated OSINT accounts via Apify.

    Returns a list of {"text": str, "author": str} dicts, or None on
    failure. Uses the schema-correct Apify input fields (twitterHandles,
    maxItems) and sends maxTotalChargeUsd as a URL query param so it
    actually caps the run cost.
    """
    if not APIFY_API_TOKEN:
        log.warning("APIFY_API_TOKEN not set — skipping X/OSINT collection")
        return None

    actor_id = "apidojo~tweet-scraper"
    api_url = f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"

    max_items = 5 * len(OSINT_ACCOUNTS)
    payload = json.dumps({
        "twitterHandles": OSINT_ACCOUNTS,
        "maxItems": max_items,
        "sort": "Latest",
        "tweetLanguage": "en",
    }).encode("utf-8")

    url = (
        f"{api_url}?token={APIFY_API_TOKEN}"
        f"&maxTotalChargeUsd={APIFY_MAX_CHARGE_USD}"
        f"&maxItems={max_items}"
    )
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            items = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log.error("Apify tweet fetch failed: %s", e)
        return None

    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = (item.get("full_text") or item.get("text") or "").strip()
        if not text:
            continue
        # Author handle can show up under several keys depending on actor build
        author = (
            (item.get("author") or {}).get("userName")
            or (item.get("user") or {}).get("userName")
            or item.get("authorUsername")
            or item.get("username")
            or ""
        )
        out.append({"text": text, "author": str(author).lstrip("@").lower()})
    return out


# ---------------------------------------------------------------------------
# Collection logic
# ---------------------------------------------------------------------------

@safe_collect
def collect() -> list:
    family_map = _build_family_map()
    sources_checked: list[str] = []
    sources_failed: list[str] = []
    source_count = 0

    all_strong: list[KeywordHit] = []
    all_weak_force: list[KeywordHit] = []
    all_weak_logistics: list[KeywordHit] = []
    all_weak_allied: list[KeywordHit] = []

    # --- Source 1: Apify Twitter scraper ---
    osint_items = _fetch_osint_tweets()
    if osint_items is not None:
        sources_checked.append("X/OSINT accounts")
        source_count += 1

        # Dedup retweets / paraphrases before any keyword matching
        deduped_texts = dedup_events([item["text"] for item in osint_items])
        # Re-attach authors by matching on text
        text_to_author = {item["text"]: item["author"] for item in osint_items}
        deduped = [(t, text_to_author.get(t, "")) for t in deduped_texts]

        for text, author in deduped:
            if not _osint_taiwan_relevant(text):
                continue
            source_id = _osint_source_id(author) if author else "osint:unknown"
            all_strong.extend(match_strong(text, source_id))
            all_weak_force.extend(match_weak(text, FORCE_WEAK, source_id))
            all_weak_logistics.extend(match_weak(text, LOGISTICS_WEAK, source_id))
            all_weak_allied.extend(match_weak(text, ALLIED_WEAK, source_id))
    elif not APIFY_API_TOKEN:
        sources_failed.append("X/OSINT (Apify token not configured)")
    else:
        sources_failed.append("X/OSINT (Apify fetch failed)")

    # --- Source 2: Taiwan MND PLA activity page ---
    mnd_text = fetch_url(MND_PLA_URL, verify_ssl=False)
    if mnd_text:
        sources_checked.append("Taiwan MND")
        source_count += 1
        all_strong.extend(match_strong(mnd_text, "MND"))
        all_weak_force.extend(match_weak(mnd_text, FORCE_WEAK, "MND"))
        all_weak_logistics.extend(match_weak(mnd_text, LOGISTICS_WEAK, "MND"))
        all_weak_allied.extend(match_weak(mnd_text, ALLIED_WEAK, "MND"))

        # Quantitative baseline: count anomalies feed indicator #1 directly
        baseline_entry = parse_mnd_counts(mnd_text, today=now_taipei_date())
        try:
            append_baseline(baseline_entry)
        except OSError as e:
            log.warning("Could not persist baseline: %s", e)
        aircraft_anomaly = check_anomaly("aircraft", baseline_entry.aircraft)
        vessel_anomaly = check_anomaly("vessels", baseline_entry.vessels)
    else:
        sources_failed.append("Taiwan MND (unreachable)")
        aircraft_anomaly = None
        vessel_anomaly = None

    # --- Source 3: Japan MOD MSDF press releases ---
    japan_text = fetch_url(JAPAN_MOD_URL, verify_ssl=False)
    if japan_text:
        sources_checked.append("Japan MOD")
        source_count += 1
        all_strong.extend(match_strong(japan_text, "Japan MOD"))
        all_weak_force.extend(match_weak(japan_text, FORCE_WEAK, "Japan MOD"))
        all_weak_allied.extend(match_weak(japan_text, ALLIED_WEAK, "Japan MOD"))
    else:
        sources_failed.append("Japan MOD (blocked)")

    checked_str = ", ".join(sources_checked) if sources_checked else "none"
    failed_str = f" Failed: {', '.join(sources_failed)}." if sources_failed else ""

    # --- Indicator #1: Force Concentration ---
    readings = [
        _evaluate_indicator(
            indicator_id=1,
            indicator_name="Force Concentration",
            strong_keyword_filter=INDICATOR_1_STRONG,
            all_strong=all_strong,
            weak_hits=all_weak_force,
            family_map=family_map,
            source_count=source_count,
            checked_str=checked_str,
            failed_str=failed_str,
            anomaly=aircraft_anomaly or vessel_anomaly,
            anomaly_label="MND aircraft/vessel count",
        ),
        # --- Indicator #2: Logistics & Mobilization ---
        _evaluate_indicator(
            indicator_id=2,
            indicator_name="Logistics & Mobilization",
            strong_keyword_filter=INDICATOR_2_STRONG,
            all_strong=all_strong,
            weak_hits=all_weak_logistics,
            family_map=family_map,
            source_count=source_count,
            checked_str=checked_str,
            failed_str=failed_str,
            # Logistics has no quantitative MND-count baseline
            anomaly=None,
            anomaly_label="",
            # X/OSINT is the primary feed for logistics signals
            require_osint=True,
            osint_available=osint_items is not None,
        ),
        # --- Indicator #8: Allied Response ---
        _evaluate_indicator(
            indicator_id=8,
            indicator_name="Allied Response",
            strong_keyword_filter=INDICATOR_8_STRONG,
            all_strong=all_strong,
            weak_hits=all_weak_allied,
            family_map=family_map,
            source_count=source_count,
            checked_str=checked_str,
            failed_str=failed_str,
            anomaly=None,
            anomaly_label="",
        ),
    ]

    return readings


# ---------------------------------------------------------------------------
# Per-indicator evaluator — encodes the converged activation rule
# ---------------------------------------------------------------------------

def _evaluate_indicator(
    *,
    indicator_id: int,
    indicator_name: str,
    strong_keyword_filter: set[str],
    all_strong: list[KeywordHit],
    weak_hits: list[KeywordHit],
    family_map: dict[str, str],
    source_count: int,
    checked_str: str,
    failed_str: str,
    anomaly,
    anomaly_label: str,
    require_osint: bool = False,
    osint_available: bool = True,
):
    """
    Activation rule:
        active iff ANY of:
            (a) ≥1 STRONG keyword (filtered to this indicator's vocabulary)
                from any source, observed-action gate already applied
            (b) baseline anomaly (current >= median + 2*MAD), if applicable
            (c) ≥3 unique WEAK keywords across ≥2 source families,
                negative-context sentences excluded, AND LLM adjudicator
                returns "yes"

    Confidence and evidence_class derive from which branch fired.
    """
    # Branch A: STRONG match
    relevant_strong = [h for h in all_strong if h.keyword in strong_keyword_filter]
    if relevant_strong:
        unique_terms = sorted({h.keyword for h in relevant_strong})
        sources = sorted({h.source for h in relevant_strong})
        return make_reading(
            indicator_id=indicator_id,
            active=True,
            confidence="high",
            evidence_class="concrete",
            summary=(
                f"Concrete signal — {', '.join(unique_terms[:5])} "
                f"(sources: {', '.join(sources[:3])}). "
                f"Checked {checked_str}.{failed_str}"
            ),
            feed_healthy=source_count > 0,
        )

    # Branch B: Baseline anomaly (only indicator #1)
    if anomaly is not None and anomaly.status in ("anomaly", "high_anomaly"):
        return make_reading(
            indicator_id=indicator_id,
            active=True,
            confidence="high" if anomaly.status == "high_anomaly" else "medium",
            evidence_class="anomaly",
            summary=(
                f"Anomaly detected — {anomaly.explanation} "
                f"Checked {checked_str}.{failed_str}"
            ),
            feed_healthy=source_count > 0,
        )

    # Branch C: WEAK keyword convergence + LLM adjudication
    unique_weak = unique_keywords(weak_hits)
    by_family = hits_by_source_family(weak_hits, family_map)
    families_with_hits = [f for f, hits in by_family.items() if hits]

    weak_threshold_met = (
        len(unique_weak) >= WEAK_UNIQUE_THRESHOLD
        and len(families_with_hits) >= WEAK_FAMILY_THRESHOLD
    )

    if not weak_threshold_met:
        # Inactive — no concrete signal, no anomaly, weak signal below threshold
        if require_osint and not osint_available:
            summary = (
                f"Could not check — X/OSINT is the primary source for this indicator.{failed_str}"
            )
            healthy = False
        elif source_count == 0:
            summary = f"Could not check — all sources failed.{failed_str}"
            healthy = False
        else:
            summary = (
                f"Checked {checked_str}. No concrete signals; "
                f"{len(unique_weak)} unique vocabulary matches across "
                f"{len(families_with_hits)} source families "
                f"(needs ≥{WEAK_UNIQUE_THRESHOLD} terms across "
                f"≥{WEAK_FAMILY_THRESHOLD} families)."
            )
            healthy = source_count > 0
        return make_reading(
            indicator_id=indicator_id,
            active=False,
            confidence="none",
            evidence_class="keyword",
            summary=summary,
            feed_healthy=healthy,
        )

    # WEAK threshold met → ask the LLM adjudicator
    snippets = [
        WeakMatchSnippet(source=h.source, matched_terms=[h.keyword], sentence=h.sentence)
        for h in weak_hits
        if not is_negative_context(h.sentence)  # extra defense; should already be filtered
    ]
    verdict = adjudicate_weak_signal(indicator_name, snippets)

    if verdict.verdict == "yes":
        return make_reading(
            indicator_id=indicator_id,
            active=True,
            confidence="medium",
            evidence_class="keyword",
            summary=(
                f"Vocabulary convergence ({len(unique_weak)} unique terms across "
                f"{len(families_with_hits)} families) confirmed by LLM adjudicator: "
                f"{verdict.rationale} Checked {checked_str}.{failed_str}"
            ),
            feed_healthy=source_count > 0,
        )

    # Adjudicator said "no" or "undetermined" → inactive, but record the signal
    suffix = (
        " (adjudicator unavailable — defaulting to inactive)"
        if not verdict.available
        else ""
    )
    return make_reading(
        indicator_id=indicator_id,
        active=False,
        confidence="low",
        evidence_class="keyword",
        summary=(
            f"Vocabulary convergence at threshold but adjudicator returned "
            f"'{verdict.verdict}': {verdict.rationale}{suffix} "
            f"Checked {checked_str}.{failed_str}"
        ),
        feed_healthy=source_count > 0,
    )


# ---------------------------------------------------------------------------
# OSINT relevance pre-filter
# ---------------------------------------------------------------------------

TAIWAN_CONTEXT_PATTERNS = [
    "taiwan", "taipei", "formosa", "fujian", "guangdong", "kaohsiung",
    "cross-strait", "cross strait", "strait of taiwan", "taiwan strait",
    " pla ", " plan ", " plaaf ", "plarf", "pla navy", "pla air",
    "indo-pacific command", "indopacom", "rocaf",
]


def _osint_taiwan_relevant(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in TAIWAN_CONTEXT_PATTERNS)
