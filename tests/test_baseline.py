"""Tests for analysis/baseline.py — MND count parsing + MAD anomaly check."""

import sys
import os
import json
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_parse_mnd_aircraft_count(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "config.BASELINE_FILE", str(tmp_path / "baselines.jsonl")
    )
    from analysis import baseline as baseline_module
    monkeypatch.setattr(baseline_module, "BASELINE_FILE", str(tmp_path / "baselines.jsonl"))

    text = "Detected 23 PLA aircraft and 5 PLAN vessels in the past 24 hours."
    entry = baseline_module.parse_mnd_counts(text, today=date(2026, 4, 28))
    assert entry.aircraft == 23
    assert entry.vessels == 5
    assert entry.parser_confidence == "high"
    assert entry.date == "2026-04-28"


def test_parse_mnd_failure_marks_low_confidence(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "config.BASELINE_FILE", str(tmp_path / "baselines.jsonl")
    )
    from analysis import baseline as baseline_module
    monkeypatch.setattr(baseline_module, "BASELINE_FILE", str(tmp_path / "baselines.jsonl"))

    entry = baseline_module.parse_mnd_counts("No relevant content here.", today=date(2026, 4, 28))
    assert entry.aircraft is None
    assert entry.vessels is None
    assert entry.parser_confidence == "failed"


def test_anomaly_unknown_during_bootstrap(tmp_path, monkeypatch):
    """Should return 'unknown' until BOOTSTRAP_DAYS samples accumulate."""
    monkeypatch.setattr(
        "config.BASELINE_FILE", str(tmp_path / "baselines.jsonl")
    )
    from analysis import baseline as baseline_module
    monkeypatch.setattr(baseline_module, "BASELINE_FILE", str(tmp_path / "baselines.jsonl"))

    result = baseline_module.check_anomaly("aircraft", 50)
    assert result.status == "unknown"
    assert "Bootstrap" in result.explanation


def test_anomaly_normal_within_baseline(tmp_path, monkeypatch):
    """With enough samples and current within baseline, status is 'normal'."""
    monkeypatch.setattr(
        "config.BASELINE_FILE", str(tmp_path / "baselines.jsonl")
    )
    from analysis import baseline as baseline_module
    monkeypatch.setattr(baseline_module, "BASELINE_FILE", str(tmp_path / "baselines.jsonl"))

    # Seed 20 days of "normal" 20-aircraft counts
    rows = [
        {
            "date": f"2026-04-{i:02d}",
            "aircraft": 20,
            "vessels": 5,
            "parser_confidence": "high",
            "raw_excerpt": "..."
        }
        for i in range(1, 21)
    ]
    with open(tmp_path / "baselines.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    result = baseline_module.check_anomaly("aircraft", 22)
    assert result.status == "normal"


def test_anomaly_high_anomaly_triggers(tmp_path, monkeypatch):
    """Current >> baseline → high_anomaly."""
    monkeypatch.setattr(
        "config.BASELINE_FILE", str(tmp_path / "baselines.jsonl")
    )
    from analysis import baseline as baseline_module
    monkeypatch.setattr(baseline_module, "BASELINE_FILE", str(tmp_path / "baselines.jsonl"))

    # Seed varied "normal" counts so MAD is non-zero
    rows = [
        {
            "date": f"2026-04-{i:02d}",
            "aircraft": 18 + (i % 5),  # 18..22 range
            "vessels": 5,
            "parser_confidence": "high",
            "raw_excerpt": "..."
        }
        for i in range(1, 21)
    ]
    with open(tmp_path / "baselines.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    result = baseline_module.check_anomaly("aircraft", 200)
    assert result.status == "high_anomaly"
