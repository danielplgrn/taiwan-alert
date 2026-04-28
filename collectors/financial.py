"""
Collector: Financial Stress (indicator 10)

Data sources:
  - yfinance: TWD/USD exchange rate, TAIEX index

Signals:
  - TWD depreciation > 2% in 24h (normal daily: ~0.3%)
  - TAIEX drop > 5% in 24h (normal daily: ~1%)
"""

from __future__ import annotations

import logging

from collectors.base import make_reading, safe_collect

log = logging.getLogger(__name__)


# Thresholds for anomaly detection
TWD_DEPRECIATION_THRESHOLD = 0.02   # 2% depreciation in a day
TAIEX_DROP_THRESHOLD = 0.05          # 5% drop in a day


@safe_collect
def collect() -> list:
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance not installed — cannot check financial indicators")
        return [make_reading(
            indicator_id=10, active=False,
            summary="yfinance not available", feed_healthy=False,
        )]

    signals = []
    any_healthy = False

    # --- TWD/USD ---
    try:
        twd = yf.Ticker("TWD=X")
        hist = twd.history(period="2d")
        if len(hist) >= 2:
            any_healthy = True
            prev_close = hist["Close"].iloc[-2]
            curr = hist["Close"].iloc[-1]
            change = (curr - prev_close) / prev_close
            # TWD=X is USD/TWD, so a rise means TWD weakening
            if change > TWD_DEPRECIATION_THRESHOLD:
                signals.append(f"TWD depreciation {change:.1%}")
    except Exception as e:
        log.warning("TWD fetch failed: %s", e)

    # --- TAIEX ---
    try:
        taiex = yf.Ticker("^TWII")
        hist = taiex.history(period="2d")
        if len(hist) >= 2:
            any_healthy = True
            prev_close = hist["Close"].iloc[-2]
            curr = hist["Close"].iloc[-1]
            change = (curr - prev_close) / prev_close
            if change < -TAIEX_DROP_THRESHOLD:
                signals.append(f"TAIEX drop {change:.1%}")
    except Exception as e:
        log.warning("TAIEX fetch failed: %s", e)

    active = len(signals) >= 1
    return [make_reading(
        indicator_id=10,
        active=active,
        confidence="high" if len(signals) >= 2 else ("medium" if signals else "none"),
        summary=f"Checked TWD/USD and TAIEX. Anomaly detected: {' | '.join(signals)}" if active else "Checked TWD/USD exchange rate and TAIEX index via Yahoo Finance. Both within normal volatility range.",
        feed_healthy=any_healthy,
        # Financial signals are quantitative deviations from baseline volatility.
        evidence_class="anomaly" if active else "keyword",
    )]
