"""
STRONG-only keyword vocabulary for the deterministic parallel detector.

Architecture context (Option B.1):
  - The LLM evidence extractor is the primary path for OSINT and gov-text
    interpretation on indicators 1, 2, 8.
  - This module is the **deterministic parallel detector**. It runs alongside
    the LLM and fires authoritatively when a tightly-curated, externally-
    observable categorical act appears in the text.
  - WEAK keyword lists (FORCE_WEAK / LOGISTICS_WEAK / ALLIED_WEAK) and the
    sentence-scoped negative-context filter were removed in this refactor.
    Only categorical-action vocabulary remains.
  - Observed-action gate stays: speculation/hypothetical sentences are
    rejected. Geography gate stays: "port closure" requires a theater port.

The full keyword list is intentionally short (8-12 phrases). Adding less-
specific terms here would re-introduce the false-positive surface we just
spent a refactor removing. Expansion needs eval evidence.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# STRONG — concrete, costly, externally observable acts only.
# Curated short list. Each phrase requires a real bureaucratic/operational
# decision that is hard to fake or reverse, and is published or observable.
# ---------------------------------------------------------------------------

STRONG_KEYWORDS = [
    # Civilian transport requisition (administrative act, observable from outside)
    "civilian ferry requisition",
    "civilian ferries requisitioned",
    "ro-ro ship requisition",

    # Manpower mobilization (administrative orders)
    "reserve call-up order",
    "reservist mobilization order",
    "civilian conscription order",

    # Mass-casualty preparation (logistics signal)
    "blood donation drive military",
    "mass casualty preparation",

    # Hard infrastructure closures (geo-gated below)
    "port closure",
    "civilian airspace closure",

    # Diplomatic personnel withdrawal — a lagging but very concrete escalation
    "embassy evacuation",
]


# ---------------------------------------------------------------------------
# Per-indicator routing for STRONG hits
# ---------------------------------------------------------------------------

INDICATOR_1_STRONG = {
    "port closure",  # geo-gated
    "civilian airspace closure",  # geo-gated
}

INDICATOR_2_STRONG = {
    "civilian ferry requisition",
    "civilian ferries requisitioned",
    "ro-ro ship requisition",
    "reserve call-up order",
    "reservist mobilization order",
    "civilian conscription order",
    "blood donation drive military",
    "mass casualty preparation",
}

INDICATOR_8_STRONG: set[str] = set()  # No allied-side STRONG terms — LLM-only
INDICATOR_7_STRONG = {"embassy evacuation"}  # diplomatic indicator (handled elsewhere)


# ---------------------------------------------------------------------------
# Observed-action gate — reject speculative/conditional/second-hand forms.
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


def is_hypothetical(sentence: str) -> bool:
    """True if the sentence is conditional / speculative / second-hand."""
    return bool(_HYPOTHETICAL_RE.search(sentence))


# ---------------------------------------------------------------------------
# Theater-relevant geography (for closure-type STRONG keywords)
# ---------------------------------------------------------------------------

THEATER_PORTS = [
    # PRC ports near Taiwan (Fujian / Guangdong / Zhejiang)
    "xiamen", "fuzhou", "quanzhou", "putian", "ningde", "zhangzhou",
    "shantou", "shanwei", "wenzhou",
    # Taiwan ports
    "kaohsiung", "keelung", "taichung port", "hualien port", "anping",
    # Strategic islands
    "matsu", "kinmen", "penghu", "dongyin",
]

THEATER_AIRSPACE_TOKENS = [
    "taiwan", "taipei fir", "taipei flight information region",
    "strait", "cross-strait", "fujian airspace",
]


def has_theater_geography(sentence: str, kind: str) -> bool:
    s = sentence.lower()
    if kind == "port":
        return any(p in s for p in THEATER_PORTS)
    if kind == "airspace":
        return any(p in s for p in THEATER_AIRSPACE_TOKENS)
    return True


# ---------------------------------------------------------------------------
# Sentence splitter — used to scope the observed-action / geography gates
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？；;\n])\s+")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


# ---------------------------------------------------------------------------
# STRONG keyword detector
# ---------------------------------------------------------------------------

class StrongHit:
    __slots__ = ("keyword", "sentence", "source", "chunk_id")

    def __init__(self, keyword: str, sentence: str, source: str, chunk_id: str = ""):
        self.keyword = keyword
        self.sentence = sentence
        self.source = source
        self.chunk_id = chunk_id

    def __repr__(self):
        return f"StrongHit({self.keyword!r}, source={self.source}, chunk_id={self.chunk_id})"


_PORT_GATED = {"port closure"}
_AIRSPACE_GATED = {"civilian airspace closure"}


def _passes_gates(keyword: str, sentence: str) -> bool:
    if is_hypothetical(sentence):
        return False
    if keyword in _PORT_GATED:
        return has_theater_geography(sentence, "port")
    if keyword in _AIRSPACE_GATED:
        return has_theater_geography(sentence, "airspace")
    return True


def detect_strong(text: str, source: str, chunk_id: str = "") -> list[StrongHit]:
    """
    Find STRONG keyword matches in `text`, sentence-by-sentence, applying
    the observed-action and geography gates. Returns one hit per
    (keyword, sentence) pair.
    """
    hits: list[StrongHit] = []
    for sentence in split_sentences(text):
        s_lower = sentence.lower()
        for kw in STRONG_KEYWORDS:
            if kw in s_lower and _passes_gates(kw, sentence):
                hits.append(StrongHit(kw, sentence, source, chunk_id))
    return hits
