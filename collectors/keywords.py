"""
Keyword vocabulary + matching pipeline for the military / OSINT collectors.

Designed around the converged Codex critique:

- STRONG keywords describe **concrete, externally observable, costly
  administrative or operational acts**. They are not just "scary words" —
  they are things that require a real bureaucratic decision and would
  cost the regime to fake or reverse. Examples: "civilian ferry
  requisition", "reserve call-up order", "blood donation drive military".

- WEAK keywords are doctrine vocabulary, capability terms, or routine
  PLA-pressure language ("aircraft carrier", "amphibious", "combat
  readiness patrol"). They appear in normal daily reporting as much as
  in genuine escalation. They count only when MULTIPLE unique terms
  cross-corroborate from independent sources, with negative-context
  sentences excluded.

- The observed-action gate rejects conditional / hypothetical / reporting-
  about-reporting forms. "Analysts fear a reserve call-up" must not count
  the same as "Reservists ordered to report by 06:00 Friday".

- The negative filter is SENTENCE-scoped, not document-scoped. A tweet
  saying "Joint Sword exercise expands; civilian ferries requisitioned in
  Fujian" must not lose the requisition signal because the same tweet
  also mentions "exercise".
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# STRONG — concrete, costly, externally observable acts.
#
# A STRONG match should be a meaningful signal on its own (subject to the
# observed-action gate below). Each phrase here describes a specific act,
# not a capability or doctrine concept.
# ---------------------------------------------------------------------------

STRONG_KEYWORDS = [
    # Civilian transport requisition (impossible to fake; visible from outside)
    "civilian ferry requisition",
    "civilian ferries requisitioned",
    "ro-ro ship requisition",
    "ro-ro requisitioned",
    "civilian vessel commandeered",
    "merchant fleet mobilized",

    # Manpower mobilization (administrative acts, not capability)
    "reserve call-up order",
    "reservist mobilization order",
    "reserve activation order",
    "civilian conscription order",
    "militia mobilization order",
    "leave cancellation order",  # Taiwan side too — used in ind #5 collector
    "general mobilization",

    # Mass-casualty preparation (concrete logistics signal)
    "blood donation drive military",
    "blood drive military",
    "mass casualty preparation",

    # Hard infrastructure closures with a named locale (geo-gated below)
    "port closure",
    "civilian airspace closure",
    "harbor closure",

    # Diplomatic personnel withdrawal — a lagging but concrete escalation
    "embassy evacuation",
    "diplomats recalled taiwan",
]


# ---------------------------------------------------------------------------
# WEAK — appears in routine reporting AND in genuine escalation.
#
# Counts only via the cross-source / unique-keyword / negative-filter gate.
# ---------------------------------------------------------------------------

FORCE_WEAK = [
    "pla navy", "plan fleet", "amphibious", "landing ship", "lst",
    "fujian port", "guangdong port", "naval staging", "ship concentration",
    "carrier strike", "aircraft carrier", "forward deploy", "fighter deploy",
    "missile repositioning", "tel movement", "plarf", "df-", "rocket force",
    "joint exercise fujian", "combat readiness patrol", "war mobilization",
    "military conscription",
]

LOGISTICS_WEAK = [
    "fuel staging", "ammunition", "ammo movement", "hospital activation",
    "mobilization order", "civilian ferry", "ro-ro ship", "rail military",
    "logistics surge", "transport requisition", "militia mobilization",
    "strategic reserve",
]

ALLIED_WEAK = [
    "carrier strike group taiwan", "taiwan strait transit",
    "surge deploy western pacific", "reposition to taiwan",
    "japan sdf alert", "jsdf scramble record",
    "p-8 poseidon taiwan", "guam surge deploy",
    "indopacom taiwan contingency",
]


# ---------------------------------------------------------------------------
# Negative-context (sentence-scoped, WEAK-only)
# ---------------------------------------------------------------------------

NEGATIVE_CONTEXT_WORDS = [
    "exercise", "drill", "training", "scheduled", "routine",
    "annual", "regular patrol", "rehearsal", "wargame",
]


# ---------------------------------------------------------------------------
# Observed-action gate — kill conditional / hypothetical / second-hand forms.
#
# These patterns mark a sentence as speculative. STRONG matches inside such
# sentences are downgraded. We do this with a tight allow-list of conditional
# verbs and reporting verbs; this is not perfect English-language NLP, but
# it catches the obvious cases ("analysts fear", "may", "could", "if") that
# would otherwise let rumors auto-fire.
# ---------------------------------------------------------------------------

HYPOTHETICAL_PATTERNS = [
    r"\bmay\b", r"\bmight\b", r"\bcould\b", r"\bwould\b", r"\bshould\b",
    r"\bif\b", r"\bin case\b", r"\bin the event\b",
    r"\banalysts? (fear|warn|believe|think|suggest|speculate)\b",
    r"\b(experts?|observers?|sources?) (fear|warn|believe|think|suggest|speculate)\b",
    r"\b(rumored|allegedly|reportedly|unconfirmed|speculated)\b",
    r"\bif\s+(?:the|china|prc|pla)\b",
    r"\bcontingency\b", r"\bhypothetical\b", r"\bscenario\b",
    r"\bplanning\s+for\b", r"\bpreparation(?:s)?\s+for\s+possible\b",
]
_HYPOTHETICAL_RE = re.compile("|".join(HYPOTHETICAL_PATTERNS), re.IGNORECASE)


# ---------------------------------------------------------------------------
# Theater-relevant geography (used to confirm "port closure" / "airspace closure"
# refer to a Taiwan-relevant location). A bare "port closure" without a
# named locale on this allowlist is not actionable.
# ---------------------------------------------------------------------------

THEATER_PORTS = [
    # PRC ports near Taiwan (Fujian / Guangdong / Zhejiang)
    "xiamen", "fuzhou", "quanzhou", "putian", "ningde", "zhangzhou",
    "shantou", "shanwei", "wenzhou",
    # Taiwan ports
    "kaohsiung", "keelung", "taichung port", "hualien port", "anping",
    # PLA-relevant island chains
    "matsu", "kinmen", "penghu", "dongyin", "xisha", "spratly",
]

THEATER_AIRSPACE_TOKENS = [
    "taiwan", "taipei fir", "taipei flight information region",
    "strait", "cross-strait", "fujian airspace",
]


# ---------------------------------------------------------------------------
# Sentence splitter — naive but good enough for tweets and MND HTML.
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？；;\n])\s+")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _contains(text_lower: str, terms: list[str]) -> bool:
    return any(t in text_lower for t in terms)


def is_negative_context(sentence: str) -> bool:
    """True if the sentence is framed as routine/scheduled activity."""
    return _contains(sentence.lower(), NEGATIVE_CONTEXT_WORDS)


def is_hypothetical(sentence: str) -> bool:
    """True if the sentence is conditional / speculative / second-hand."""
    return bool(_HYPOTHETICAL_RE.search(sentence))


def has_theater_geography(sentence: str, kind: str) -> bool:
    """
    For hard-infrastructure STRONG keywords (port/airspace closure), require
    that the named locale falls within the Taiwan theater. Without this gate
    a "port closure in Hamburg" would auto-fire.
    """
    s = sentence.lower()
    if kind == "port":
        return _contains(s, THEATER_PORTS)
    if kind == "airspace":
        return _contains(s, THEATER_AIRSPACE_TOKENS)
    return True


# ---------------------------------------------------------------------------
# Match dataclasses
# ---------------------------------------------------------------------------

class KeywordHit:
    __slots__ = ("keyword", "sentence", "strength", "source")

    def __init__(self, keyword: str, sentence: str, strength: str, source: str):
        self.keyword = keyword
        self.sentence = sentence
        self.strength = strength      # "strong" | "weak"
        self.source = source           # source identifier (e.g. "MND", "osint:sentdefender")

    def __repr__(self):
        return f"KeywordHit({self.strength}, {self.keyword!r}, {self.source})"


# ---------------------------------------------------------------------------
# Geography-gated STRONG keywords (require theater-relevant locale)
# ---------------------------------------------------------------------------

_PORT_GATED = {"port closure", "harbor closure"}
_AIRSPACE_GATED = {"civilian airspace closure"}


def _strong_passes_gates(keyword: str, sentence: str) -> bool:
    """Apply observed-action + geography gates to a STRONG keyword."""
    if is_hypothetical(sentence):
        return False
    if keyword in _PORT_GATED:
        return has_theater_geography(sentence, "port")
    if keyword in _AIRSPACE_GATED:
        return has_theater_geography(sentence, "airspace")
    return True


# ---------------------------------------------------------------------------
# Public matching API
# ---------------------------------------------------------------------------

def match_strong(text: str, source: str) -> list[KeywordHit]:
    """
    Find STRONG keyword hits in `text`, sentence-by-sentence, applying
    the observed-action and geography gates. Returns one KeywordHit per
    matching (keyword, sentence) pair.
    """
    hits: list[KeywordHit] = []
    for sentence in split_sentences(text):
        s_lower = sentence.lower()
        for kw in STRONG_KEYWORDS:
            if kw in s_lower and _strong_passes_gates(kw, sentence):
                hits.append(KeywordHit(kw, sentence, "strong", source))
    return hits


def match_weak(
    text: str,
    weak_keywords: list[str],
    source: str,
    apply_negative_filter: bool = True,
) -> list[KeywordHit]:
    """
    Find WEAK keyword hits in `text`. Sentences containing negative-context
    words ("exercise", "drill", "training", ...) are dropped at the SENTENCE
    level — but only WEAK matches are filtered. (STRONG matches in the same
    document are unaffected; that pipeline runs separately.)
    """
    hits: list[KeywordHit] = []
    for sentence in split_sentences(text):
        if apply_negative_filter and is_negative_context(sentence):
            continue
        s_lower = sentence.lower()
        for kw in weak_keywords:
            if kw in s_lower:
                hits.append(KeywordHit(kw, sentence, "weak", source))
    return hits


def unique_keywords(hits: list[KeywordHit]) -> set[str]:
    """Set of distinct keywords — counts unique terms, not raw hits."""
    return {h.keyword for h in hits}


def hits_by_source_family(hits: list[KeywordHit], family_of: dict[str, str]) -> dict[str, list[KeywordHit]]:
    """
    Group hits by source family. `family_of` maps source -> family name
    (e.g. "GOV", "OSINT_TIER1", "OSINT_TIER2"). Hits whose source isn't
    in the map fall under "OTHER".
    """
    by_family: dict[str, list[KeywordHit]] = {}
    for h in hits:
        family = family_of.get(h.source, "OTHER")
        by_family.setdefault(family, []).append(h)
    return by_family
