"""
Military / OSINT collector — sole owner of indicators 1, 2, 8.

Pipeline (LLM-first; Option B.1 architecture, post-Codex round 2):

    OSINT tweets ──► dedup into clusters ──► chunks (representative only)
    Taiwan MND   ──────────────────────────► chunks
    Japan MOD    ──────────────────────────► chunks
                                                │
                                                ▼
                ┌───────────────────────────────┴─────────────────────┐
                │                                                       │
                ▼                                                       ▼
    LLM evidence extractor (Opus 4.7)              STRONG keyword detector
    -- returns evidence refs --                    -- deterministic, parallel --
    Code validates verbatim quotes,                Fires authoritatively when
    drops manipulation_flag/speculation/           a categorical act appears
    hypothetical, groups by cluster.

                                  │
                                  ▼
                    Deterministic reducer derives:
                       active, evidence_class, confidence
                    LLM-derived activation OR STRONG hit OR anomaly path
                                  │
                                  ▼
                     IndicatorReading (×3: 1, 2, 8)

    Anomaly path (indicator #1 only):
        MND aircraft/vessel counts ──► MAD anomaly check ──► concrete-anomaly fact
        (LLM never sees baseline data; this is fully deterministic)
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error

from collectors.base import fetch_url, make_reading, safe_collect, now_iso
from collectors.keywords import (
    detect_strong, StrongHit,
    INDICATOR_1_STRONG, INDICATOR_2_STRONG, INDICATOR_8_STRONG,
)
from analysis.dedup import cluster_events, TweetMember, Cluster
from analysis.baseline import (
    parse_mnd_counts, append_baseline, check_anomaly, now_taipei_date,
)
from analysis.llm_evidence_extractor import (
    extract_evidence, InputChunk, EvidenceRef, ExtractionResult,
    SUPPORTED_INDICATORS,
)
from config import APIFY_API_TOKEN, APIFY_MAX_CHARGE_USD

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OSINT account tiers — manually curated source-credibility split.
# ---------------------------------------------------------------------------

OSINT_TIER1 = ["detresfa_", "IndoPac_Info", "MT_Anderson", "sentdefender"]
OSINT_TIER2 = [
    "COUPSURE", "RupsNair", "coalitionSAS", "OAlexanderDK",
    "Faytuks", "Nfrayer", "OSABORNINGCN", "PLaboringCN",
]
OSINT_ACCOUNTS = OSINT_TIER1 + OSINT_TIER2

FAMILY_GOV = "GOV"
FAMILY_OSINT_T1 = "OSINT_TIER1"
FAMILY_OSINT_T2 = "OSINT_TIER2"


def _osint_source_id(handle: str) -> str:
    return f"osint:{handle.lstrip('@').lower()}"


def _osint_family(handle: str) -> str:
    h = handle.lstrip("@").lower()
    if h in {a.lower() for a in OSINT_TIER1}:
        return FAMILY_OSINT_T1
    return FAMILY_OSINT_T2


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
# Apify OSINT fetch (correct schema; cost-capped per run)
# ---------------------------------------------------------------------------

def _fetch_osint_tweets() -> list[dict] | None:
    """
    Returns a list of {"text": str, "author": str} or None on failure.
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
        url, data=payload,
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
# Chunk-builder: representative-only OSINT, gov text as single chunks
# ---------------------------------------------------------------------------

def _build_chunks(
    osint_clusters: list[Cluster],
    mnd_text: str | None,
    japan_text: str | None,
) -> tuple[list[InputChunk], dict[str, str]]:
    """
    Build the chunk list to send to the LLM. Representative-only for OSINT.
    Returns (chunks, chunk_id_to_cluster_id) — the second map lets the
    reducer compute corroboration math on cluster membership.
    """
    chunks: list[InputChunk] = []
    chunk_to_cluster: dict[str, str] = {}
    cid = 0

    def next_id() -> str:
        nonlocal cid
        cid += 1
        return f"c{cid:03d}"

    # OSINT — one chunk per cluster, using the representative member
    for cluster in osint_clusters:
        if cluster.representative is None:
            continue
        rep = cluster.representative
        chunk_id = next_id()
        family = _osint_family(rep.author) if rep.author else FAMILY_OSINT_T2
        source_id = _osint_source_id(rep.author) if rep.author else "osint:unknown"
        chunks.append(InputChunk(
            chunk_id=chunk_id,
            source=source_id,
            family=family,
            text=rep.text,
            cluster_id=cluster.cluster_id,
            count_in_cluster=cluster.size,
        ))
        chunk_to_cluster[chunk_id] = cluster.cluster_id

    # Gov sources — single large chunk each
    if mnd_text:
        chunk_id = next_id()
        chunks.append(InputChunk(
            chunk_id=chunk_id,
            source="MND",
            family=FAMILY_GOV,
            text=mnd_text,
            cluster_id=f"gov:{chunk_id}",
            count_in_cluster=1,
        ))
        chunk_to_cluster[chunk_id] = f"gov:{chunk_id}"

    if japan_text:
        chunk_id = next_id()
        chunks.append(InputChunk(
            chunk_id=chunk_id,
            source="Japan MOD",
            family=FAMILY_GOV,
            text=japan_text,
            cluster_id=f"gov:{chunk_id}",
            count_in_cluster=1,
        ))
        chunk_to_cluster[chunk_id] = f"gov:{chunk_id}"

    return chunks, chunk_to_cluster


# ---------------------------------------------------------------------------
# Deterministic reducer — turns LLM evidence + STRONG hits + anomaly into
# IndicatorReading(s). The LLM does NOT determine indicator state here.
# ---------------------------------------------------------------------------

# Per-indicator activation rules (in code, not LLM):
#   observed_act + GOV present                         → active, concrete, high
#   observed_act + ≥1 OSINT_TIER1 cluster (no GOV)     → active, concrete, medium
#   observed_act + only OSINT_TIER2 clusters            → active, keyword, low
#   vocabulary_only + ≥3 distinct clusters across ≥2 families → active, keyword, low
#   else                                                → inactive
# STRONG hit on the same chunks → forces active=True regardless (parallel detector).
# Anomaly path → active=True with evidence_class=anomaly, confidence=high.

def _reduce_indicator(
    indicator_id: int,
    chunks: list[InputChunk],
    evidence: list[EvidenceRef],
    strong_hits: list[StrongHit],
    chunk_lookup: dict[str, InputChunk],
    chunk_to_cluster: dict[str, str],
    indicator_strong_set: set[str],
    sources_checked_str: str,
    failed_str: str,
    extractor_available: bool,
    manipulation_flagged: int,
    anomaly_status: str | None = None,
    anomaly_explanation: str | None = None,
    indicator_name: str = "",
):
    """Apply Option-B.1 rules to produce an IndicatorReading for one indicator."""

    # ---------- Anomaly path (deterministic, separate from LLM) ----------
    if anomaly_status in ("anomaly", "high_anomaly"):
        return make_reading(
            indicator_id=indicator_id,
            active=True,
            confidence="high" if anomaly_status == "high_anomaly" else "medium",
            evidence_class="anomaly",
            summary=(
                f"Quantitative anomaly detected — {anomaly_explanation} "
                f"Checked {sources_checked_str}.{failed_str}"
            ),
            rationale=anomaly_explanation or "",
            evidence_quotes=[],
            manipulation_flagged_count=manipulation_flagged,
            feed_healthy=True,
        )

    # ---------- STRONG keyword detector path (deterministic, parallel) ----------
    relevant_strong = [h for h in strong_hits if h.keyword in indicator_strong_set]
    if relevant_strong:
        unique_terms = sorted({h.keyword for h in relevant_strong})
        sources = sorted({h.source for h in relevant_strong})
        evidence_quotes = [
            {
                "chunk_id": h.chunk_id,
                "source": h.source,
                "family": chunk_lookup.get(h.chunk_id, InputChunk("", h.source, "", "")).family if chunk_lookup else "",
                "key_phrase": h.keyword,
                "claim_type": "observed_act",
                "directness": "reported_event",
                "why": "STRONG keyword detector match",
            }
            for h in relevant_strong[:5]
        ]
        return make_reading(
            indicator_id=indicator_id,
            active=True,
            confidence="high",
            evidence_class="concrete",
            summary=(
                f"Concrete signal (STRONG detector) — {', '.join(unique_terms[:5])} "
                f"in {', '.join(sources[:3])}. Checked {sources_checked_str}.{failed_str}"
            ),
            rationale=f"Deterministic STRONG keyword match: {', '.join(unique_terms[:3])}",
            evidence_quotes=evidence_quotes,
            manipulation_flagged_count=manipulation_flagged,
            feed_healthy=True,
        )

    # ---------- LLM-derived path ----------
    # Filter validated evidence for this indicator. `taiwan_relevance == "direct"`
    # is required: tangential/unrelated evidence (e.g. INDOPACOM equipment failures,
    # PLA Beibu Gulf drills, US carrier movements outside the Pacific) is dropped
    # before activation logic and dashboard rendering. The LLM still extracts
    # them — they're useful audit signal for prompt tuning — but they don't
    # drive alerting and don't appear on indicator cards.
    indicator_evidence = [
        ev for ev in evidence
        if ev.validated
        and ev.indicator_id == indicator_id
        and not ev.manipulation_flag
        and ev.claim_type not in ("speculation", "unrelated")
        and ev.directness != "hypothetical"
        and ev.taiwan_relevance == "direct"
    ]

    if not indicator_evidence:
        # Inactive
        if not extractor_available:
            summary = (
                f"LLM extractor unavailable — only deterministic paths checked. "
                f"No STRONG keyword hits, no anomaly. {sources_checked_str}.{failed_str}"
            )
        else:
            summary = (
                f"Checked {sources_checked_str}. No qualifying evidence "
                f"(after dropping speculation/hypothetical/manipulation chunks).{failed_str}"
            )
        return make_reading(
            indicator_id=indicator_id,
            active=False,
            confidence="none",
            evidence_class="keyword",
            summary=summary,
            rationale="",
            evidence_quotes=[],
            manipulation_flagged_count=manipulation_flagged,
            feed_healthy=extractor_available or any(c.family == FAMILY_GOV for c in chunks),
        )

    # Group surviving evidence by cluster_id (collapses retweets)
    clusters_per_evidence: dict[str, list[EvidenceRef]] = {}
    for ev in indicator_evidence:
        ck = chunk_to_cluster.get(ev.chunk_id, ev.chunk_id)
        clusters_per_evidence.setdefault(ck, []).append(ev)

    # Compute family coverage
    families_with_evidence: set[str] = set()
    for ev in indicator_evidence:
        chunk = chunk_lookup.get(ev.chunk_id)
        if chunk:
            families_with_evidence.add(chunk.family)

    has_gov = FAMILY_GOV in families_with_evidence
    has_t1 = FAMILY_OSINT_T1 in families_with_evidence
    only_t2 = (
        FAMILY_OSINT_T2 in families_with_evidence
        and not has_gov and not has_t1
    )

    has_observed_act = any(ev.claim_type == "observed_act" for ev in indicator_evidence)
    vocabulary_only_clusters = {
        ck for ck, evs in clusters_per_evidence.items()
        if all(ev.claim_type == "vocabulary_only" for ev in evs)
    }

    # Apply rules
    active = False
    evidence_class = "keyword"
    confidence = "none"
    summary = f"Checked {sources_checked_str}.{failed_str}"

    if has_observed_act and has_gov:
        active = True
        evidence_class = "concrete"
        confidence = "high"
        summary = (
            f"Observed act corroborated by GOV source. "
            f"Checked {sources_checked_str}.{failed_str}"
        )
    elif has_observed_act and has_t1:
        active = True
        evidence_class = "concrete"
        confidence = "medium"
        summary = (
            f"Observed act corroborated by tier-1 OSINT. "
            f"Checked {sources_checked_str}.{failed_str}"
        )
    elif has_observed_act and only_t2:
        active = True
        evidence_class = "keyword"
        confidence = "low"
        summary = (
            f"Observed act from tier-2 OSINT only — low confidence. "
            f"Checked {sources_checked_str}.{failed_str}"
        )
    elif (
        len(vocabulary_only_clusters) >= 3
        and len(families_with_evidence) >= 2
    ):
        active = True
        evidence_class = "keyword"
        confidence = "low"
        summary = (
            f"Vocabulary convergence across {len(vocabulary_only_clusters)} clusters "
            f"in {len(families_with_evidence)} families. "
            f"Checked {sources_checked_str}.{failed_str}"
        )
    else:
        # Below thresholds
        active = False
        evidence_class = "keyword"
        confidence = "none"
        summary = (
            f"Evidence below thresholds — "
            f"{len(indicator_evidence)} qualifying refs in {len(families_with_evidence)} "
            f"families ({len(clusters_per_evidence)} clusters). "
            f"Checked {sources_checked_str}.{failed_str}"
        )

    # Build evidence quote payload for the dashboard
    evidence_quotes = []
    for ev in indicator_evidence[:6]:
        chunk = chunk_lookup.get(ev.chunk_id)
        evidence_quotes.append({
            "chunk_id": ev.chunk_id,
            "source": chunk.source if chunk else "",
            "family": chunk.family if chunk else "",
            "key_phrase": ev.key_phrase,
            "claim_type": ev.claim_type,
            "directness": ev.directness,
            "why": ev.why,
        })

    rationale = " ".join(
        f"[{ev.directness}/{ev.claim_type}] {ev.why}"
        for ev in indicator_evidence[:3]
    )[:400]

    return make_reading(
        indicator_id=indicator_id,
        active=active,
        confidence=confidence,
        evidence_class=evidence_class,
        summary=summary,
        rationale=rationale,
        evidence_quotes=evidence_quotes,
        manipulation_flagged_count=manipulation_flagged,
        feed_healthy=True,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

@safe_collect
def collect() -> list:
    sources_checked: list[str] = []
    sources_failed: list[str] = []

    # ---------- Fetch ----------
    osint_items = _fetch_osint_tweets()
    if osint_items is not None:
        sources_checked.append("X/OSINT accounts")
    elif not APIFY_API_TOKEN:
        sources_failed.append("X/OSINT (Apify token not configured)")
    else:
        sources_failed.append("X/OSINT (Apify fetch failed)")

    mnd_text = fetch_url(MND_PLA_URL, verify_ssl=False)
    if mnd_text:
        sources_checked.append("Taiwan MND")
    else:
        sources_failed.append("Taiwan MND (unreachable)")

    japan_text = fetch_url(JAPAN_MOD_URL, verify_ssl=False)
    if japan_text:
        sources_checked.append("Japan MOD")
    else:
        sources_failed.append("Japan MOD (blocked)")

    sources_checked_str = ", ".join(sources_checked) if sources_checked else "none"
    failed_str = f" Failed: {', '.join(sources_failed)}." if sources_failed else ""

    # ---------- Cluster OSINT ----------
    osint_clusters: list[Cluster] = []
    if osint_items:
        members = [TweetMember(text=item["text"], author=item.get("author", "")) for item in osint_items]
        osint_clusters = cluster_events(members)

    # ---------- Build chunks ----------
    chunks, chunk_to_cluster = _build_chunks(osint_clusters, mnd_text, japan_text)
    chunk_lookup = {c.chunk_id: c for c in chunks}

    # ---------- LLM evidence extraction ----------
    extraction = extract_evidence(chunks)
    if not extraction.available and extraction.error:
        log.warning("LLM extractor unavailable: %s", extraction.error)
    if extraction.dropped_for_injection:
        log.warning(
            "Pre-filter dropped %d chunks with obvious injection markers",
            extraction.dropped_for_injection,
        )

    manipulation_flagged = sum(1 for ev in extraction.evidence if ev.manipulation_flag)

    # ---------- STRONG keyword parallel detector ----------
    strong_hits: list[StrongHit] = []
    for c in chunks:
        strong_hits.extend(detect_strong(c.text, c.source, chunk_id=c.chunk_id))

    # ---------- Baseline anomaly (indicator #1 only, fully deterministic) ----------
    aircraft_anomaly_status = None
    aircraft_anomaly_explanation = None
    if mnd_text:
        baseline_entry = parse_mnd_counts(mnd_text, today=now_taipei_date())
        try:
            append_baseline(baseline_entry)
        except OSError as e:
            log.warning("Could not persist baseline: %s", e)
        aircraft_anomaly = check_anomaly("aircraft", baseline_entry.aircraft)
        vessel_anomaly = check_anomaly("vessels", baseline_entry.vessels)
        # Take whichever metric is most anomalous
        if aircraft_anomaly.status in ("anomaly", "high_anomaly"):
            aircraft_anomaly_status = aircraft_anomaly.status
            aircraft_anomaly_explanation = aircraft_anomaly.explanation
        elif vessel_anomaly.status in ("anomaly", "high_anomaly"):
            aircraft_anomaly_status = vessel_anomaly.status
            aircraft_anomaly_explanation = vessel_anomaly.explanation

    # ---------- Reduce per indicator ----------
    readings = []
    for indicator_id, indicator_strong_set, indicator_name in [
        (1, INDICATOR_1_STRONG, "Force Concentration"),
        (2, INDICATOR_2_STRONG, "Logistics & Mobilization"),
        (8, INDICATOR_8_STRONG, "Allied Response"),
    ]:
        anomaly_for_this = (
            (aircraft_anomaly_status, aircraft_anomaly_explanation)
            if indicator_id == 1 else (None, None)
        )
        readings.append(_reduce_indicator(
            indicator_id=indicator_id,
            chunks=chunks,
            evidence=extraction.evidence,
            strong_hits=strong_hits,
            chunk_lookup=chunk_lookup,
            chunk_to_cluster=chunk_to_cluster,
            indicator_strong_set=indicator_strong_set,
            sources_checked_str=sources_checked_str,
            failed_str=failed_str,
            extractor_available=extraction.available,
            manipulation_flagged=manipulation_flagged,
            anomaly_status=anomaly_for_this[0],
            anomaly_explanation=anomaly_for_this[1],
            indicator_name=indicator_name,
        ))

    return readings
