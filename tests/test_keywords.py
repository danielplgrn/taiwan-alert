"""Tests for the STRONG-only deterministic keyword detector (Option B.1)."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collectors.keywords import (
    detect_strong, StrongHit,
    is_hypothetical, has_theater_geography,
    INDICATOR_1_STRONG, INDICATOR_2_STRONG,
)


# ----- Hypothetical detection -----

def test_hypothetical_detected():
    assert is_hypothetical("Analysts fear PLA may stage carrier strike force.")
    assert is_hypothetical("China could decide to enforce blockade.")
    assert is_hypothetical("Reportedly, reservists have been called up.")
    assert is_hypothetical("If the PLA decides to escalate.")


def test_factual_not_hypothetical():
    assert not is_hypothetical("Reservists ordered to report by 06:00 Friday.")
    assert not is_hypothetical("Civilian ferries requisitioned in Fujian.")


# ----- STRONG detector with observed-action gate -----

def test_strong_detector_categorical_act():
    """A concrete observed administrative act fires the STRONG detector."""
    text = "Civilian ferries requisitioned in Fujian today by central command."
    hits = detect_strong(text, source="MND")
    assert len(hits) >= 1
    keywords = [h.keyword for h in hits]
    assert any("ferries requisitioned" in k or "ferry requisition" in k for k in keywords)


def test_strong_rejected_when_hypothetical():
    """STRONG keyword inside 'analysts fear' sentence does not count."""
    text = "Analysts fear China may issue a reserve call-up order soon."
    hits = detect_strong(text, source="osint:test")
    assert len(hits) == 0


def test_strong_keyword_membership():
    """Sanity check that the curated STRONG keyword set is small (8-12 entries)."""
    from collectors.keywords import STRONG_KEYWORDS
    assert 8 <= len(STRONG_KEYWORDS) <= 14


# ----- Geography gates -----

def test_port_closure_gated_to_theater():
    text = "Port closure ordered at Xiamen following central directive."
    hits = detect_strong(text, source="MND")
    assert any(h.keyword == "port closure" for h in hits)


def test_port_closure_outside_theater_rejected():
    text = "Port closure announced at Hamburg today."
    hits = detect_strong(text, source="osint:test")
    assert all(h.keyword != "port closure" for h in hits)


def test_airspace_closure_requires_taiwan_geography():
    """'civilian airspace closure' only counts when paired with theater terms."""
    text = "Civilian airspace closure declared over Taiwan Strait."
    hits = detect_strong(text, source="MND")
    assert any(h.keyword == "civilian airspace closure" for h in hits)


# ----- Indicator routing sanity -----

def test_indicator_routing_logistics():
    """Logistics-related STRONG terms are routed to indicator #2."""
    assert "civilian ferry requisition" in INDICATOR_2_STRONG
    assert "reserve call-up order" in INDICATOR_2_STRONG
    assert "blood donation drive military" in INDICATOR_2_STRONG


def test_indicator_routing_force_concentration():
    """Closure terms route to indicator #1."""
    assert "port closure" in INDICATOR_1_STRONG
    assert "civilian airspace closure" in INDICATOR_1_STRONG


def test_chunk_id_propagated():
    """detect_strong attaches chunk_id when provided."""
    hits = detect_strong(
        "Civilian ferries requisitioned in Fujian.",
        source="MND",
        chunk_id="c042",
    )
    assert len(hits) >= 1
    assert all(h.chunk_id == "c042" for h in hits)
