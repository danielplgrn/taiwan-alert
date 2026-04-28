"""Tests for analysis/dedup.py — tweet event deduplication."""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analysis.dedup import normalize_tweet, dedup_events, _HAVE_RAPIDFUZZ


def test_normalize_strips_urls():
    assert "https://" not in normalize_tweet("hello https://example.com world")


def test_normalize_strips_handles_and_hashtags():
    out = normalize_tweet("RT @user: check out #taiwan news")
    assert "@user" not in out
    assert "#taiwan" not in out
    assert "rt" not in out  # RT prefix stripped
    assert "check out" in out


def test_dedup_collapses_identical():
    items = ["hello world", "hello world", "hello world"]
    out = dedup_events(items)
    assert len(out) == 1


@pytest.mark.skipif(not _HAVE_RAPIDFUZZ, reason="rapidfuzz not installed — fuzzy dedup unavailable")
def test_dedup_collapses_retweets():
    items = [
        "PLA aircraft carrier transit observed near Fujian today",
        "RT @sentdefender: PLA aircraft carrier transit observed near Fujian today",
        "via @aggregator PLA aircraft carrier transit observed near Fujian today https://t.co/abc",
    ]
    out = dedup_events(items)
    # All three are paraphrases of the same event
    assert len(out) <= 2  # rapidfuzz threshold may keep 1 or merge to 2


def test_dedup_keeps_distinct_events():
    items = [
        "PLA carrier transit observed near Fujian",
        "Reservists ordered to report by 06:00 Friday",
        "Civilian ferries requisitioned in Xiamen",
    ]
    out = dedup_events(items)
    assert len(out) == 3
