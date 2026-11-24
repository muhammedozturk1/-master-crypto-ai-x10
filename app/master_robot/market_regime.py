"""Piyasa rejimi motoru (şartname 3.2).

ADX 25 üstü trend, 20 altı yatay için başlangıç filtresidir; bu eşikler piyasa gerçeği
değildir ve backtest ile doğrulanacaktır (Aşama 8).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.bots.base import MarketContext
from app.indicators import core as I


class Regime(str, Enum):
    GUCLU_YUKSELIS = "GUCLU_YUKSELIS"
    ZAYIF_YUKSELIS = "ZAYIF_YUKSELIS"
    GUCLU_DUSUS = "GUCLU_DUSUS"
    ZAYIF_DUSUS = "ZAYIF_DUSUS"
    YATAY = "YATAY"
    VOLATILITE_SIKISMASI = "VOLATILITE_SIKISMASI"
    YUKSEK_VOLATILITE = "YUKSEK_VOLATILITE"
    DUSUK_LIKIDITE = "DUSUK_LIKIDITE"
    HABER_BELIRSIZLIGI = "HABER_BELIRSIZLIGI"
    ANORMAL_HAREKET = "ANORMAL_HAREKET"


NO_NEW_TRADES = {Regime.DUSUK_LIKIDITE, Regime.HABER_BELIRSIZLIGI, Regime.ANORMAL_HAREKET}


@dataclass
class RegimeResult:
    regime: Regime
    direction: int          # +1 yükseliş, -1 düşüş, 0 yön yok
    metrics: dict

    def to_dict(self) -> dict:
        return {"regime": self.regime.value, "direction": self.direction, **self.metrics}


@dataclass
class RegimeThresholds:
    adx_trend: float = 25.0
    adx_range: float = 20.0
    high_vol_atr_pct: float = 90.0
    squeeze_bw_pct: float = 15.0
    squeeze_atr_pct: float = 25.0
    max_spread_pct: float = 0.10
    abnormal_range_atr: float = 4.0
    abnormal_move_pct: float = 5.0


def detect_regime(ctx: MarketContext, news_veto: bool = False,
                  th: RegimeThresholds | None = None) -> RegimeResult:
    th = th or RegimeThresholds()
    H, L, C = ctx.highs, ctx.lows, ctx.closes
    i = len(C) - 1
    e20, e50, e200 = I.ema(C, 20), I.ema(C, 50), I.ema(C, 200)
    atr = I.atr(H, L, C, 14)
    adx = I.adx(H, L, C, 14)[0]
    _, _, _, bw = I.bollinger(C, 20, 2.0)
    m = {
        "adx": adx[i], "atr": atr[i],
        "atr_pct_rank": I.percentile_rank(atr, i, 120),
        "bb_width_pct_rank": I.percentile_rank(bw, i, 120),
        "spread_pct": ctx.spread_pct,
        "last_move_pct": abs(C[i] / C[i - 1] - 1) * 100 if i > 0 else 0.0,
    }
    direction = 0
    if e200[i] is not None and e50[i] is not None:
        if C[i] > e200[i] and e20[i] > e50[i]:
            direction = 1
        elif C[i] < e200[i] and e20[i] < e50[i]:
            direction = -1
    m["ema_alignment"] = direction

    def res(r: Regime) -> RegimeResult:
        return RegimeResult(r, direction, m)

    # Öncelik: işlem açmayı engelleyen durumlar önce
    if news_veto:
        return res(Regime.HABER_BELIRSIZLIGI)
    if atr[i] and (H[i] - L[i] > th.abnormal_range_atr * atr[i] or m["last_move_pct"] > th.abnormal_move_pct):
        return res(Regime.ANORMAL_HAREKET)
    if ctx.spread_pct is not None and ctx.spread_pct > th.max_spread_pct:
        return res(Regime.DUSUK_LIKIDITE)
    if adx[i] is None or atr[i] is None:
        return res(Regime.YATAY)
    if m["atr_pct_rank"] is not None and m["atr_pct_rank"] >= th.high_vol_atr_pct:
        return res(Regime.YUKSEK_VOLATILITE)
    if (m["bb_width_pct_rank"] is not None and m["atr_pct_rank"] is not None
            and m["bb_width_pct_rank"] <= th.squeeze_bw_pct and m["atr_pct_rank"] <= th.squeeze_atr_pct):
        return res(Regime.VOLATILITE_SIKISMASI)
    if adx[i] >= th.adx_trend and direction != 0:
        return res(Regime.GUCLU_YUKSELIS if direction > 0 else Regime.GUCLU_DUSUS)
    if adx[i] >= th.adx_range and direction != 0:
        return res(Regime.ZAYIF_YUKSELIS if direction > 0 else Regime.ZAYIF_DUSUS)
    return res(Regime.YATAY)
