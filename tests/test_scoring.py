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


def _reading(ind_id, active=False, confidence="medium", is_destructive=False,
             feed_healthy=True, evidence_class="concrete"):
    """
    Default `evidence_class="concrete"` so existing tests can verify
    state-machine behavior independently of the keyword-only cap.
    Tests that specifically exercise the cap pass evidence_class="keyword".
    """
    return IndicatorReading(
        id=ind_id, active=active, confidence=confidence,
        summary="test", last_checked="2026-01-01T00:00:00Z",
        feed_healthy=feed_healthy, is_destructive=is_destructive,
        evidence_class=evidence_class,
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


# ----- KEYWORD-ONLY MAX-PROMOTION RULE -----
# RED requires at least one active indicator with concrete/anomaly/hostilities
# evidence. Keyword-only signals cap at YELLOW regardless of how many fire.

def test_two_keyword_only_primaries_caps_at_yellow():
    """Two keyword-class primaries should NOT promote to RED."""
    readings = _all_inactive()
    readings[1] = _reading(1, active=True, evidence_class="keyword")
    readings[3] = _reading(3, active=True, evidence_class="keyword")
    state = evaluate(readings)
    assert state.alert_state == AlertState.YELLOW
    assert "keyword-only" in state.score_detail.lower()


def test_keyword_primary_plus_keyword_secondary_caps_at_yellow():
    """Mixed keyword-only signals shouldn't promote past YELLOW (no concrete evidence)."""
    readings = _all_inactive()
    readings[5] = _reading(5, active=True, evidence_class="keyword")  # primary
    readings[9] = _reading(9, active=True, evidence_class="keyword")  # secondary
    state = evaluate(readings)
    assert state.alert_state == AlertState.YELLOW


def test_concrete_primary_plus_keyword_secondary_is_amber():
    """1 concrete primary + 1 keyword secondary should promote to AMBER."""
    readings = _all_inactive()
    readings[5] = _reading(5, active=True, evidence_class="concrete")
    readings[9] = _reading(9, active=True, evidence_class="keyword")
    state = evaluate(readings)
    assert state.alert_state == AlertState.AMBER


def test_anomaly_primary_alone_drives_yellow_not_red():
    """One anomaly primary alone hits the threshold-1 RED branch only at threshold=1."""
    readings = _all_inactive()
    readings[1] = _reading(1, active=True, evidence_class="anomaly")
    state = evaluate(readings)
    # 1 primary → YELLOW (need 2 to hit RED at default threshold)
    assert state.alert_state == AlertState.YELLOW


def test_two_anomaly_primaries_require_persistence_for_red():
    """Two anomaly-class primaries should NOT immediately promote to RED — anomaly
    requires PERSISTENCE_REQUIRED_RUNS consecutive runs before it can drive the
    alert state past YELLOW. Single-tick anomalies cap at YELLOW."""
    readings = _all_inactive()
    readings[1] = _reading(1, active=True, evidence_class="anomaly")
    readings[3] = _reading(3, active=True, evidence_class="anomaly")
    state = evaluate(readings)
    # Fresh anomalies (consecutive_active_runs=1 after evaluate) cap at YELLOW.
    assert state.alert_state == AlertState.YELLOW
    assert "awaiting persistence" in state.score_detail.lower()


def test_two_anomaly_primaries_promote_to_red_after_persistence():
    """After PERSISTENCE_REQUIRED_RUNS=2 consecutive runs of anomaly activation,
    two anomaly-class primaries DO promote to RED."""
    from scoring import SystemState
    # First tick: anomaly fires, cap at YELLOW
    readings = _all_inactive()
    readings[1] = _reading(1, active=True, evidence_class="anomaly")
    readings[3] = _reading(3, active=True, evidence_class="anomaly")
    state1 = evaluate(readings)
    assert state1.alert_state == AlertState.YELLOW

    # Second tick: same indicators still active, persistence threshold met
    readings2 = _all_inactive()
    readings2[1] = _reading(1, active=True, evidence_class="anomaly")
    readings2[3] = _reading(3, active=True, evidence_class="anomaly")
    state2 = evaluate(readings2, previous_state=state1)
    assert state2.alert_state == AlertState.RED


def test_overt_hostilities_overrides_keyword_cap():
    """overt_hostilities still drives RED regardless of evidence_class."""
    readings = _all_inactive()
    readings[1] = _reading(1, active=True, evidence_class="keyword")
    state = evaluate(readings, overt_hostilities=True)
    assert state.alert_state == AlertState.RED


def test_evidence_class_persisted_through_save_load(tmp_path, monkeypatch):
    """evidence_class field round-trips through save_state/load_previous_state."""
    import config as cfg_module
    state_file = tmp_path / "state.json"
    web_file = tmp_path / "web_state.json"
    history_file = tmp_path / "history.jsonl"
    monkeypatch.setattr(cfg_module, "STATE_FILE", str(state_file))
    monkeypatch.setattr(cfg_module, "WEB_STATE_FILE", str(web_file))
    monkeypatch.setattr(cfg_module, "HISTORY_FILE", str(history_file))
    monkeypatch.setattr(cfg_module, "DATA_DIR", str(tmp_path))
    # scoring module uses module-level imports; patch there too
    import scoring as scoring_module
    monkeypatch.setattr(scoring_module, "STATE_FILE", str(state_file))
    monkeypatch.setattr(scoring_module, "WEB_STATE_FILE", str(web_file))
    monkeypatch.setattr(scoring_module, "HISTORY_FILE", str(history_file))
    monkeypatch.setattr(scoring_module, "DATA_DIR", str(tmp_path))

    readings = _all_inactive()
    readings[3] = _reading(3, active=True, evidence_class="anomaly")
    state = evaluate(readings)
    scoring_module.save_state(state)

    loaded = scoring_module.load_previous_state()
    assert loaded is not None
    assert loaded.indicators[3].evidence_class == "anomaly"
    assert loaded.indicators[3].active is True


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
