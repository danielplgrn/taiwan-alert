"""Tests for the new keyword-matching pipeline (Codex-debate refactor)."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collectors.keywords import (
    match_strong, match_weak, unique_keywords, hits_by_source_family,
    is_negative_context, is_hypothetical, has_theater_geography,
    FORCE_WEAK, LOGISTICS_WEAK,
)


# ----- Negative-context detection -----

def test_negative_context_detected():
    assert is_negative_context("This is a routine annual exercise.")
    assert is_negative_context("Joint Sword 2024-A drill scheduled for next week.")


def test_negative_context_not_in_concrete_sentence():
    assert not is_negative_context("PLA observed staging amphibious force in Fujian.")


# ----- Hypothetical detection -----

def test_hypothetical_detected():
    assert is_hypothetical("Analysts fear PLA may stage carrier strike force.")
    assert is_hypothetical("China could decide to enforce blockade.")
    assert is_hypothetical("Reportedly, reservists have been called up.")
    assert is_hypothetical("If the PLA decides to escalate.")


def test_factual_not_hypothetical():
    assert not is_hypothetical("Reservists ordered to report by 06:00 Friday.")
    assert not is_hypothetical("Civilian ferries requisitioned in Fujian.")


# ----- STRONG matching with observed-action gate -----

def test_strong_match_observed_action():
    text = "Civilian ferries requisitioned in Fujian today by central command."
    hits = match_strong(text, "MND")
    assert len(hits) >= 1
    assert any("ferries requisitioned" in h.keyword.lower() or
               "requisition" in h.keyword.lower() for h in hits)
    assert all(h.strength == "strong" for h in hits)


def test_strong_rejected_when_hypothetical():
    """STRONG keyword inside a 'analysts fear' sentence should not count."""
    text = "Analysts fear China may issue a reserve call-up order soon."
    hits = match_strong(text, "osint:test")
    assert len(hits) == 0


def test_strong_not_dropped_by_negative_filter():
    """The Codex catch: 'Joint Sword exercise expands; civilian ferries requisitioned in Fujian'
    must NOT lose the requisition signal. Negative filter is WEAK-only, sentence-scoped."""
    text = "Joint Sword exercise expands; civilian ferries requisitioned in Fujian."
    strong_hits = match_strong(text, "osint:test")
    # The "Joint Sword exercise" sentence has the trigger word, but the
    # second sentence (civilian ferries requisitioned) is concrete and observed.
    # Sentence splitter may keep them as one sentence since the joiner is ';'.
    # Either way, the STRONG matcher only checks the observed-action gate, not
    # negative-context — so this should hit.
    assert any("requisition" in h.keyword.lower() for h in strong_hits)


# ----- WEAK matching with sentence-scoped negative filter -----

def test_weak_match_baseline():
    text = "PLA carrier strike group transiting through Taiwan Strait. Amphibious ships observed."
    hits = match_weak(text, FORCE_WEAK, "osint:test")
    keywords = [h.keyword for h in hits]
    assert "carrier strike" in keywords
    assert "amphibious" in keywords


def test_weak_filtered_by_negative_context():
    text = "Joint Sword exercise: PLA aircraft carrier and amphibious deployment."
    hits = match_weak(text, FORCE_WEAK, "osint:test")
    assert len(hits) == 0  # whole sentence has "exercise" → all weak hits dropped


def test_weak_negative_filter_is_sentence_scoped():
    """Two sentences in one text — only the negative one drops out."""
    text = (
        "Joint Sword exercise scheduled for next week. "
        "PLA carrier strike group also transiting through strait."
    )
    hits = match_weak(text, FORCE_WEAK, "osint:test")
    # Sentence 1 dropped (has "exercise" + "scheduled"), sentence 2 kept
    keywords = [h.keyword for h in hits]
    assert "carrier strike" in keywords


# ----- Geography gates -----

def test_port_closure_gated_to_theater():
    # Theater-relevant port → STRONG match passes
    text = "Port closure ordered at Xiamen following central directive."
    hits = match_strong(text, "MND")
    assert any(h.keyword == "port closure" for h in hits)


def test_port_closure_outside_theater_rejected():
    text = "Port closure announced at Hamburg today."
    hits = match_strong(text, "osint:test")
    # 'port closure' is geo-gated; Hamburg is not on the allowlist
    assert all(h.keyword != "port closure" for h in hits)


# ----- Aggregation helpers -----

def test_unique_keywords_count():
    text = "amphibious ships near amphibious staging area; amphibious deployment."
    hits = match_weak(text, FORCE_WEAK, "osint:test")
    # Multiple raw hits of "amphibious" → one unique keyword
    assert len({h.keyword for h in hits}) == 1


def test_hits_by_source_family():
    family_map = {"MND": "GOV", "osint:a": "OSINT_TIER1", "osint:b": "OSINT_TIER2"}
    text_a = "Carrier strike group operations underway."
    text_b = "PLA amphibious vessels concentrated."
    text_c = "Aircraft carrier transit observed."
    hits = []
    hits.extend(match_weak(text_a, FORCE_WEAK, "MND"))
    hits.extend(match_weak(text_b, FORCE_WEAK, "osint:a"))
    hits.extend(match_weak(text_c, FORCE_WEAK, "osint:b"))
    by_fam = hits_by_source_family(hits, family_map)
    # All three families represented
    assert "GOV" in by_fam and "OSINT_TIER1" in by_fam and "OSINT_TIER2" in by_fam
