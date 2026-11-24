"""Piyasa rejimine göre strateji ağırlıkları (şartname 3.3 ve 15.3).

Ağırlık 0 = bu rejimde bu yönde işlem açılmaz. 1,0 = nötr. >1 = rejimle uyumlu.
Kısa (SAT) işlemler vadeli piyasa içindir; spot'ta zaten sinyal üretilmez.
"""

from __future__ import annotations

from app.master_robot.market_regime import NO_NEW_TRADES, Regime

TREND = {"trend_pullback", "donchian_breakout", "supertrend_momentum", "ema_rsi_trend"}
BREAKOUT = {"nr7_breakout", "nr7_retest", "sr_breakout", "micro_breakout", "donchian_breakout"}
REVERSION = {"footprint_absorption", "liquidity_sweep", "accumulation", "distribution"}
SCALP = {"ema9_21_scalp", "vwap_reclaim", "micro_breakout"}


def base_strategy(strategy_id: str) -> str:
    return strategy_id.split("+")[0]


def strategy_weight(regime: Regime, strategy_id: str, long: bool) -> float:
    s = base_strategy(strategy_id)
    if regime in NO_NEW_TRADES:
        return 0.0
    if regime in (Regime.GUCLU_YUKSELIS, Regime.GUCLU_DUSUS):
        with_trend = long == (regime == Regime.GUCLU_YUKSELIS)
        if not with_trend:
            return 0.3 if s in REVERSION else 0.0      # güçlü trende karşı işlem yok (yalnız dönüş kurulumları zayıf)
        if s in TREND or s in BREAKOUT:
            return 1.3
        return 0.8
    if regime in (Regime.ZAYIF_YUKSELIS, Regime.ZAYIF_DUSUS):
        with_trend = long == (regime == Regime.ZAYIF_YUKSELIS)
        if not with_trend:
            return 0.6 if s in REVERSION else 0.4
        return 1.1 if (s in TREND or s in BREAKOUT) else 0.9
    if regime == Regime.YATAY:
        if s in REVERSION:
            return 1.3
        if s in SCALP:
            return 1.0
        if s in TREND:
            return 0.4
        return 0.7
    if regime == Regime.VOLATILITE_SIKISMASI:
        if s.startswith("nr7") or s in BREAKOUT:
            return 1.3
        return 0.6
    if regime == Regime.YUKSEK_VOLATILITE:
        return 0.6 if s not in SCALP else 0.3          # pozisyon küçültülür (Aşama 5), seçici olunur
    return 1.0
