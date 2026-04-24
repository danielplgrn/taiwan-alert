"""
Tests for the scoring engine.

Covers: state transitions, hysteresis, cyber escalation, edge cases.
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scoring import IndicatorReading, SystemState, evaluate, effective_category
from config import AlertState, Category


def _reading(ind_id, active=False, confidence="medium", is_destructive=False, feed_healthy=True):
    return IndicatorReading(
        id=ind_id, active=active, confidence=confidence,
        summary="test", last_checked="2026-01-01T00:00:00Z",
        feed_healthy=feed_healthy, is_destructive=is_destructive,
    )


def _all_inactive():
    return {i: _reading(i) for i in range(1, 11)}


def _with_active(base, *ids, **kwargs):
    readings = dict(base)
    for ind_id in ids:
        readings[ind_id] = _reading(ind_id, active=True, **kwargs)
    return readings


# ----- GREEN -----

def test_all_inactive_is_green():
    state = evaluate(_all_inactive())
    assert state.alert_state == AlertState.GREEN


def test_one_secondary_is_green():
    readings = _with_active(_all_inactive(), 10)  # financial
    state = evaluate(readings)
    assert state.alert_state == AlertState.GREEN


# ----- YELLOW -----

def test_one_primary_is_yellow():
    readings = _with_active(_all_inactive(), 3)  # airspace
    state = evaluate(readings)
    assert state.alert_state == AlertState.YELLOW


def test_two_secondaries_is_yellow():
    readings = _with_active(_all_inactive(), 7, 10)  # diplomatic + financial
    state = evaluate(readings)
    assert state.alert_state == AlertState.YELLOW


# ----- AMBER -----

def test_one_primary_plus_one_secondary_is_amber():
    readings = _with_active(_all_inactive(), 5, 9)  # taiwan readiness + rhetoric
    state = evaluate(readings)
    assert state.alert_state == AlertState.AMBER


def test_one_primary_plus_one_primary_is_red_not_amber():
    readings = _with_active(_all_inactive(), 2, 5)  # logistics + taiwan readiness
    state = evaluate(readings)
    assert state.alert_state == AlertState.RED


# ----- RED -----

def test_two_primaries_is_red():
    readings = _with_active(_all_inactive(), 1, 3)  # force + airspace
    state = evaluate(readings)
    assert state.alert_state == AlertState.RED


def test_overt_hostilities_is_red():
    state = evaluate(_all_inactive(), overt_hostilities=True)
    assert state.alert_state == AlertState.RED


# ----- CYBER ESCALATION -----

def test_cyber_secondary_by_default():
    reading = _reading(6, active=True, is_destructive=False)
    assert effective_category(reading) == Category.SECONDARY


def test_cyber_escalates_to_primary_if_destructive():
    reading = _reading(6, active=True, is_destructive=True)
    assert effective_category(reading) == Category.PRIMARY


def test_destructive_cyber_plus_secondary_is_amber():
    readings = _all_inactive()
    readings[6] = _reading(6, active=True, is_destructive=True)
    readings[10] = _reading(10, active=True)  # financial
    state = evaluate(readings)
    assert state.alert_state == AlertState.AMBER


# ----- HYSTERESIS -----

def test_hysteresis_holds_yellow():
    """Yellow should not drop to Green until 4h have passed."""
    readings_yellow = _with_active(_all_inactive(), 3)  # 1 primary → yellow
    prev = evaluate(readings_yellow)
    prev.state_since = time.time() - 3600  # 1 hour ago (< 4h threshold)

    state = evaluate(_all_inactive(), previous_state=prev)
    assert state.alert_state == AlertState.YELLOW


def test_hysteresis_releases_after_threshold():
    """Yellow should drop to Green after 4h."""
    readings_yellow = _with_active(_all_inactive(), 3)
    prev = evaluate(readings_yellow)
    prev.state_since = time.time() - 5 * 3600  # 5 hours ago (> 4h)

    state = evaluate(_all_inactive(), previous_state=prev)
    assert state.alert_state == AlertState.GREEN


# ----- DEGRADED -----

def test_degraded_flag_set():
    readings = _all_inactive()
    readings[5] = _reading(5, feed_healthy=False)
    state = evaluate(readings)
    assert state.degraded is True
    assert "Taiwan Domestic Readiness" in state.degraded_feeds


def test_no_degraded_when_healthy():
    state = evaluate(_all_inactive())
    assert state.degraded is False


# ----- EDGE CASES -----

def test_all_primaries_active():
    readings = _with_active(_all_inactive(), 1, 2, 3, 4, 5)
    state = evaluate(readings)
    assert state.alert_state == AlertState.RED


def test_all_indicators_active():
    readings = {i: _reading(i, active=True) for i in range(1, 11)}
    state = evaluate(readings)
    assert state.alert_state == AlertState.RED


# ----- THRESHOLD -----

def test_threshold_hair_trigger_one_primary_is_red():
    """threshold=1 → a single active primary escalates straight to RED."""
    readings = _with_active(_all_inactive(), 3)  # airspace
    state = evaluate(readings, threshold=1)
    assert state.alert_state == AlertState.RED


def test_threshold_hair_trigger_one_secondary_is_yellow():
    """threshold=1 → a single active secondary reaches YELLOW (no primary so no AMBER)."""
    readings = _with_active(_all_inactive(), 10)  # financial
    state = evaluate(readings, threshold=1)
    assert state.alert_state == AlertState.YELLOW


def test_threshold_default_matches_two():
    """threshold=2 → same as default: 1 primary + 1 secondary = AMBER."""
    readings = _with_active(_all_inactive(), 5, 9)
    state_default = evaluate(readings)
    state_explicit = evaluate(readings, threshold=2)
    assert state_default.alert_state == state_explicit.alert_state == AlertState.AMBER


def test_threshold_dampened_blocks_amber():
    """threshold=3 → 1 primary + 1 secondary is only YELLOW, not AMBER."""
    readings = _with_active(_all_inactive(), 5, 9)  # 1 primary + 1 secondary
    state = evaluate(readings, threshold=3)
    assert state.alert_state == AlertState.YELLOW


def test_threshold_dampened_amber_needs_more():
    """threshold=3 → 1 primary + 2 secondaries = AMBER."""
    readings = _with_active(_all_inactive(), 5, 9, 10)  # 1 primary + 2 secondaries
    state = evaluate(readings, threshold=3)
    assert state.alert_state == AlertState.AMBER


def test_threshold_dampened_red_needs_three_primaries():
    """threshold=3 → 3 primaries needed for RED."""
    readings3 = _with_active(_all_inactive(), 1, 3, 5)  # 3 primaries
    state3 = evaluate(readings3, threshold=3)
    assert state3.alert_state == AlertState.RED


def test_threshold_dampened_two_primaries_is_yellow():
    """threshold=3 → 2 primaries alone: total_active=2 < 3 so stays YELLOW."""
    readings = _with_active(_all_inactive(), 1, 3)  # 2 primaries, no secondaries
    state = evaluate(readings, threshold=3)
    assert state.alert_state == AlertState.YELLOW


def test_threshold_zero_is_clamped_to_one():
    """threshold must be >= 1."""
    readings = _with_active(_all_inactive(), 3)
    state = evaluate(readings, threshold=0)
    assert state.alert_state == AlertState.RED  # clamped to 1 → behaves like hair-trigger


def test_threshold_persisted_in_state():
    readings = _with_active(_all_inactive(), 3)
    state = evaluate(readings, threshold=3)
    assert state.threshold == 3


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
