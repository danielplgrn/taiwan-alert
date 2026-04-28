"""
Base collector — shared utilities for all data collectors.

Each collector module exposes a `collect() -> list[IndicatorReading]` function.
The base provides helpers for HTTP fetching, RSS parsing, keyword matching,
confidence assignment, and error-safe wrapping.
"""

from __future__ import annotations

import logging
import ssl
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

from scoring import IndicatorReading

log = logging.getLogger(__name__)

# Shared SSL context that doesn't verify (some .gov.cn sites have cert issues)
_SSL_NOVERIFY = ssl.create_default_context()
_SSL_NOVERIFY.check_hostname = False
_SSL_NOVERIFY.verify_mode = ssl.CERT_NONE

# Standard SSL context for normal sites
_SSL_DEFAULT = ssl.create_default_context()

USER_AGENT = "TaiwanAlertBot/1.0 (personal monitoring)"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_url(url: str, timeout: int = 30, verify_ssl: bool = True) -> Optional[str]:
    """Fetch URL content as text. Returns None on failure."""
    ctx = _SSL_DEFAULT if verify_ssl else _SSL_NOVERIFY
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("Failed to fetch %s: %s", url, e)
        return None


def fetch_rss(url: str, timeout: int = 30, verify_ssl: bool = True) -> list[dict]:
    """Fetch and parse an RSS/Atom feed. Returns list of {title, link, summary, published}."""
    text = fetch_url(url, timeout=timeout, verify_ssl=verify_ssl)
    if not text:
        return []

    items = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        log.warning("Failed to parse RSS from %s", url)
        return []

    # Handle RSS 2.0
    for item in root.iter("item"):
        items.append({
            "title": _text(item, "title"),
            "link": _text(item, "link"),
            "summary": _text(item, "description"),
            "published": _text(item, "pubDate"),
        })

    # Handle Atom
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
        link_el = entry.find("atom:link", ns)
        items.append({
            "title": _text_ns(entry, "title", ns),
            "link": link_el.get("href", "") if link_el is not None else "",
            "summary": _text_ns(entry, "summary", ns) or _text_ns(entry, "content", ns),
            "published": _text_ns(entry, "published", ns) or _text_ns(entry, "updated", ns),
        })

    return items


def _text(el: ET.Element, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _text_ns(el: ET.Element, tag: str, ns: dict) -> str:
    child = el.find(f"atom:{tag}", ns)
    return (child.text or "").strip() if child is not None else ""


def keyword_match(text: str, keywords: list[str]) -> list[str]:
    """Return list of keywords found in text (case-insensitive)."""
    text_lower = text.lower()
    return [kw for kw in keywords if kw.lower() in text_lower]


def assign_confidence(match_count: int, source_count: int = 1) -> str:
    """Simple heuristic: more matches and sources = higher confidence."""
    if match_count == 0:
        return "none"
    if source_count >= 2 or match_count >= 3:
        return "high"
    if match_count >= 1:
        return "medium"
    return "low"


def make_reading(
    indicator_id: int,
    active: bool,
    confidence: str = "none",
    summary: str = "",
    feed_healthy: bool = True,
    is_destructive: bool = False,
    evidence_class: str = "keyword",
) -> IndicatorReading:
    return IndicatorReading(
        id=indicator_id,
        active=active,
        confidence=confidence,
        summary=summary,
        last_checked=now_iso(),
        feed_healthy=feed_healthy,
        is_destructive=is_destructive,
        evidence_class=evidence_class,
    )


def safe_collect(func):
    """Decorator: catch exceptions in collectors, return unhealthy reading."""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            log.exception("Collector %s failed: %s", func.__name__, e)
            # Return unhealthy readings for all indicators this collector covers
            # The caller should handle this by checking feed_healthy
            return []
    return wrapper
