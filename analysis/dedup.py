"""
Tweet event-deduplication, returning cluster objects.

OSINT accounts repost and paraphrase each other. A single underlying event
("PLA carrier transit observed") can show up as N near-identical tweets.
Code-side corroboration in the LLM-first pipeline needs to know the cluster
membership (for cross-source-family checks) without sending all duplicates
to the LLM.

This module:
  - Normalizes tweets (strips URLs/handles/hashtags/RT prefixes)
  - Clusters via rapidfuzz token_set_ratio (≥85)
  - Returns Cluster objects with cluster_id, members, and a representative
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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


@dataclass
class TweetMember:
    text: str
    author: str = ""


@dataclass
class Cluster:
    cluster_id: str
    members: list[TweetMember] = field(default_factory=list)
    representative: TweetMember | None = None

    @property
    def size(self) -> int:
        return len(self.members)


def normalize_tweet(text: str) -> str:
    """Lowercase, strip URLs/handles/hashtags/RT-prefixes, collapse whitespace."""
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
    Backward-compat: return list of representative texts only.
    Prefer cluster_events() for new code.
    """
    return [c.representative.text for c in cluster_events(
        [TweetMember(text=t) for t in texts], threshold
    ) if c.representative is not None]


def cluster_events(
    members: Iterable[TweetMember],
    threshold: int = 85,
) -> list[Cluster]:
    """
    Cluster tweets by approximate-string similarity. Returns Cluster objects
    with stable cluster_id, all member tweets (for code-side corroboration
    math), and the representative (longest text in the cluster — typically
    the original, not a truncated retweet).

    Without rapidfuzz, falls back to exact-match clustering on the
    normalized form.
    """
    items = [(m, normalize_tweet(m.text)) for m in members if m.text and m.text.strip()]
    if not items:
        return []

    if not _HAVE_RAPIDFUZZ:
        # Fallback: exact-match clustering on normalized form
        bucket: dict[str, list[TweetMember]] = {}
        for member, norm in items:
            bucket.setdefault(norm, []).append(member)
        return [
            _build_cluster(i, members_in_bucket)
            for i, members_in_bucket in enumerate(bucket.values())
        ]

    clusters: list[list[tuple[TweetMember, str]]] = []
    for member, norm in items:
        placed = False
        for cluster_items in clusters:
            _, cluster_norm = cluster_items[0]
            if fuzz.token_set_ratio(norm, cluster_norm) >= threshold:
                cluster_items.append((member, norm))
                placed = True
                break
        if not placed:
            clusters.append([(member, norm)])

    return [
        _build_cluster(i, [m for (m, _) in cluster_items])
        for i, cluster_items in enumerate(clusters)
    ]


def _build_cluster(idx: int, members: list[TweetMember]) -> Cluster:
    """Pick the longest member as the representative."""
    representative = max(members, key=lambda m: len(m.text)) if members else None
    return Cluster(
        cluster_id=f"k{idx:03d}",
        members=members,
        representative=representative,
    )
