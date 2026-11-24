"""Botların ortak kullandığı yardımcılar: işlem sinyali üretimi, hacim oranı,
swing noktaları, destek/direnç seviyeleri ve mum formasyonları."""

from __future__ import annotations

from dataclasses import dataclass

from app.bots.base import MarketContext, px
from app.domain.signals import Action, DataQuality, Signal

TARGET_NET_RR = 2.0   # şartname 10.1: asgari net ödül/risk 1:2
MAX_STOP_PCT = 4.0


def volume_ratio(volumes: list[float], i: int, lookback: int = 20) -> float:
    prior = volumes[max(0, i - lookback):i]
    avg = sum(prior) / len(prior) if prior else 0.0
    return volumes[i] / avg if avg > 0 else 0.0


def target_for(entry: float, stop: float, cost: float, long: bool, net_rr: float = TARGET_NET_RR) -> float:
    """Maliyetler sonrası istenen net ödül/riski veren hedef fiyat.
    net = (ödül - maliyet) / (risk + maliyet)  →  ödül = net·(risk+maliyet) + maliyet"""
    risk = abs(entry - stop)
    reward = net_rr * (risk + cost) + cost
    return entry + reward if long else entry - reward


def net_rr(entry: float, stop: float, target: float, cost: float) -> float:
    risk, reward = abs(entry - stop), abs(target - entry)
    return (reward - cost) / (risk + cost) if risk + cost > 0 else 0.0


@dataclass
class Plan:
    """Bir stratejinin önerdiği işlem planı; make_signal bunu doğrulayıp Signal'e çevirir."""
    long: bool
    entry_low: float
    entry_high: float
    stop: float
    target: float | None = None      # None → maliyet sonrası 1:2 hedef hesaplanır
    min_net_rr: float = TARGET_NET_RR


def make_signal(ctx: MarketContext, bot_id: str, strategy: str, plan: Plan, confidence: int,
                reason: str, details: dict | None = None, warnings: list[str] | None = None,
                regime: str | None = None, valid_bars: int = 1) -> Signal | None:
    """Ortak kurallar: stop doğru tarafta, stop %4'ten geniş değil, maliyet sonrası net
    ödül/risk en az 1:2. Herhangi biri sağlanmazsa None (sinyal yok)."""
    entry = plan.entry_high if plan.long else plan.entry_low
    if entry <= 0 or plan.stop <= 0:
        return None
    if plan.long and plan.stop >= plan.entry_low or (not plan.long and plan.stop <= plan.entry_high):
        return None
    if abs(entry - plan.stop) / entry * 100 > MAX_STOP_PCT:
        return None
    if not plan.long and not ctx.allow_short:
        return None
    cost = ctx.cost_per_unit(entry)
    target = plan.target if plan.target is not None else target_for(entry, plan.stop, cost, plan.long)
    if (plan.long and target <= plan.entry_high) or (not plan.long and target >= plan.entry_low):
        return None
    rr = net_rr(entry, plan.stop, target, cost)
    if rr < plan.min_net_rr - 1e-9:
        return None
    action = Action.AL if plan.long else Action.SAT
    bar_start = ctx.candles[-1].start_ms
    from app.data.candles import INTERVAL_MINUTES
    tf_sec = INTERVAL_MINUTES[ctx.candles[-1].interval] * 60
    return Signal(
        bot_id=bot_id, strategy_id=strategy, symbol=ctx.symbol, exchange=ctx.exchange,
        timeframe=ctx.timeframe, action=action, confidence=max(0, min(100, int(confidence))),
        entry_low=px(plan.entry_low), entry_high=px(plan.entry_high), stop_loss=px(plan.stop),
        take_profit=px(target), expected_net_rr=px(rr), estimated_cost_pct=px(cost / entry * 100),
        market_regime=regime, valid_for_seconds=tf_sec * valid_bars, reason=reason,
        data_quality=DataQuality.IYI, risk_warnings=warnings or [],
        details={**(details or {}),
                 "dedup_key": f"{bot_id}:{ctx.symbol}:{ctx.timeframe}:{strategy.split('+')[0]}:{bar_start}:{action.value}"},
    )


# --- Swing noktaları ve seviyeler ----------------------------------------

def pivots(highs: list[float], lows: list[float], end: int, lookback: int = 60, wing: int = 2
           ) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """[end-lookback, end) aralığında, her iki yanında `wing` mum olan tepe ve dipler.
    Sağ kanadı tamamlanmamış (geleceğe bakan) pivotlar alınmaz."""
    tops, bottoms = [], []
    for k in range(max(wing, end - lookback), end - wing):
        if all(highs[k] > highs[k - j] for j in range(1, wing + 1)) and all(highs[k] >= highs[k + j] for j in range(1, wing + 1)):
            tops.append((k, highs[k]))
        if all(lows[k] < lows[k - j] for j in range(1, wing + 1)) and all(lows[k] <= lows[k + j] for j in range(1, wing + 1)):
            bottoms.append((k, lows[k]))
    return tops, bottoms


def cluster_levels(points: list[tuple[int, float]], tol_pct: float = 0.3) -> list[tuple[float, int]]:
    """Birbirine %tol_pct yakın pivotları seviye olarak birleştirir → (seviye, temas sayısı)."""
    levels: list[list[float]] = []
    for _, price in sorted(points, key=lambda p: p[1]):
        if levels and abs(price - levels[-1][-1]) / levels[-1][-1] * 100 <= tol_pct:
            levels[-1].append(price)
        else:
            levels.append([price])
    return [(sum(g) / len(g), len(g)) for g in levels]


# --- Mum formasyonları (yalnızca doğrulama amaçlı; tek başına sinyal üretmez) ---

def candle_pattern(o: list[float], h: list[float], l: list[float], c: list[float], i: int) -> tuple[str | None, int]:
    """Son mumdaki formasyon ve yönü: (+1 boğa, -1 ayı, 0 nötr)."""
    if i < 2:
        return None, 0
    body = abs(c[i] - o[i])
    rng = h[i] - l[i] or 1e-12
    upper, lower = h[i] - max(o[i], c[i]), min(o[i], c[i]) - l[i]
    pb = abs(c[i - 1] - o[i - 1])
    if c[i - 1] < o[i - 1] and c[i] > o[i] and c[i] >= o[i - 1] and o[i] <= c[i - 1] and body > pb:
        return "bullish_engulfing", 1
    if c[i - 1] > o[i - 1] and c[i] < o[i] and c[i] <= o[i - 1] and o[i] >= c[i - 1] and body > pb:
        return "bearish_engulfing", -1
    p2b = abs(c[i - 2] - o[i - 2])
    if c[i - 2] < o[i - 2] and pb < p2b * 0.4 and c[i] > o[i] and c[i] > (o[i - 2] + c[i - 2]) / 2:
        return "morning_star", 1
    if c[i - 2] > o[i - 2] and pb < p2b * 0.4 and c[i] < o[i] and c[i] < (o[i - 2] + c[i - 2]) / 2:
        return "evening_star", -1
    if lower >= 2 * body and upper <= body and body / rng >= 0.1:
        return "hammer", 1
    if upper >= 2 * body and lower <= body and body / rng >= 0.1:
        return "shooting_star", -1
    if body / rng < 0.1:
        return "doji", 0
    if h[i] <= h[i - 1] and l[i] >= l[i - 1]:
        return "inside_bar", 0
    return None, 0
