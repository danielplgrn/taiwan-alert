"""
Tweet event-deduplication.

OSINT accounts repost and paraphrase each other. A single underlying event
("PLA carrier transit observed") can show up as 5 near-identical tweets,
inflating the keyword-hit count. This module collapses those into a single
canonical event using rapidfuzz string similarity on normalized text.
"""

from __future__ import annotations

import re
from typing import Iterable

try:
    from rapidfuzz import fuzz
    _HAVE_RAPIDFUZZ = True
except ImportError:
    _HAVE_RAPIDFUZZ = False


_URL_RE = re.compile(r"https?://\S+")
_HANDLE_RE = re.compile(r"@\w+")
_HASHTAG_RE = re.compile(r"#\w+")
_WHITESPACE_RE = re.compile(r"\s+")
_RT_PREFIX_RE = re.compile(r"^\s*(?:rt|RT)[ :]+", re.IGNORECASE)


def normalize_tweet(text: str) -> str:
    """
    Strip URLs, handles, hashtags, RT-prefixes, and collapse whitespace —
    so that retweets and paraphrases collapse to the same canonical form.
    """
    if not text:
        return ""
    text = _RT_PREFIX_RE.sub("", text)
    text = _URL_RE.sub("", text)
    text = _HANDLE_RE.sub("", text)
    text = _HASHTAG_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip().lower()


def dedup_events(texts: Iterable[str], threshold: int = 85) -> list[str]:
    """
    Cluster `texts` by approximate-string similarity. Within each cluster,
    return one representative (the longest, since longer often = original).

    `threshold` is rapidfuzz's token_set_ratio (0..100). 85 is a reasonable
    default — strict enough to dedupe retweets and copy-paste reposts, loose
    enough to merge minor paraphrasing.

    If rapidfuzz is unavailable, falls back to exact-match dedup on the
    normalized form.
    """
    items = [(t, normalize_tweet(t)) for t in texts if t and t.strip()]
    if not items:
        return []

    if not _HAVE_RAPIDFUZZ:
        # Fallback: exact-match dedup on normalized form
        seen: dict[str, str] = {}
        for original, norm in items:
            if norm not in seen or len(original) > len(seen[norm]):
                seen[norm] = original
        return list(seen.values())

    clusters: list[list[tuple[str, str]]] = []
    for original, norm in items:
        placed = False
        for cluster in clusters:
            # Compare against the cluster's first member
            _, cluster_norm = cluster[0]
            if fuzz.token_set_ratio(norm, cluster_norm) >= threshold:
                cluster.append((original, norm))
                placed = True
                break
        if not placed:
            clusters.append([(original, norm)])

    # Pick the longest original per cluster (likely the source, not a truncated retweet)
    return [max(cluster, key=lambda pair: len(pair[0]))[0] for cluster in clusters]
