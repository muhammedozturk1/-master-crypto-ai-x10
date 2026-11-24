"""Backtest performans metrikleri (şartname 19.3)."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class Trade:
    symbol: str
    strategy: str
    bot_id: str
    long: bool
    entry_ms: int
    exit_ms: int
    entry: float
    exit: float
    qty: float
    fees: float
    net: float
    risk_usd: float
    peak_net: float          # pozisyonun ulaştığı en yüksek net kâr (USD)
    exit_reason: str

    @property
    def r_multiple(self) -> float:
        return self.net / self.risk_usd if self.risk_usd > 0 else 0.0


def max_drawdown(curve: list[float]) -> tuple[float, float]:
    """(en büyük düşüş USD, en büyük düşüş %) – zirveden."""
    peak, dd, ddp = -math.inf, 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, peak - v)
            ddp = max(ddp, (peak - v) / peak * 100)
    return dd, ddp


def _daily_returns(equity: list[tuple[int, float]]) -> list[float]:
    by_day: dict = {}
    for t, v in equity:
        by_day[datetime.fromtimestamp(t / 1000, timezone.utc).date()] = v
    vals = [by_day[k] for k in sorted(by_day)]
    return [(b / a - 1) for a, b in zip(vals, vals[1:]) if a > 0]


def summarize(trades: list[Trade], equity: list[tuple[int, float]], start_capital: float) -> dict:
    n = len(trades)
    wins = [t.net for t in trades if t.net > 0]
    losses = [t.net for t in trades if t.net <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    final = equity[-1][1] if equity else start_capital
    dd_usd, dd_pct = max_drawdown([v for _, v in equity] or [start_capital])
    rets = _daily_returns(equity)
    mean = sum(rets) / len(rets) if rets else 0.0
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else 0.0
    downside = [r for r in rets if r < 0]
    dsd = math.sqrt(sum(r * r for r in downside) / len(rets)) if rets and downside else 0.0
    net = final - start_capital
    winners_with_peak = [t for t in trades if t.peak_net > 0 and t.net > 0]
    return {
        "trades": n,
        "net_profit": round(net, 4),
        "total_return_pct": round(net / start_capital * 100, 3),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else (None if not wins else "sonsuz"),
        "win_rate_pct": round(len(wins) / n * 100, 2) if n else None,
        "avg_win": round(gross_win / len(wins), 4) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 4) if losses else None,
        "expectancy_usd": round(sum(t.net for t in trades) / n, 4) if n else None,
        "expectancy_r": round(sum(t.r_multiple for t in trades) / n, 3) if n else None,
        "sharpe_daily_ann": round(mean / sd * math.sqrt(365), 3) if sd > 0 else None,
        "sortino_daily_ann": round(mean / dsd * math.sqrt(365), 3) if dsd > 0 else None,
        "max_drawdown_usd": round(dd_usd, 4),
        "max_drawdown_pct": round(dd_pct, 3),
        "recovery_factor": round(net / dd_usd, 3) if dd_usd > 0 else None,
        "fees_paid": round(sum(t.fees for t in trades), 4),
        "avg_duration_min": round(sum((t.exit_ms - t.entry_ms) / 60_000 for t in trades) / n, 1) if n else None,
        # Kâr kilidi verimliliği: kazanan işlemlerde gerçekleşen net / ulaşılan zirve net
        "profit_lock_efficiency": round(sum(t.net / t.peak_net for t in winners_with_peak) / len(winners_with_peak), 3)
        if winners_with_peak else None,
        "exit_reasons": _count(t.exit_reason for t in trades),
    }


def _count(xs) -> dict:
    d: dict = defaultdict(int)
    for x in xs:
        d[x] += 1
    return dict(d)


def breakdown(trades: list[Trade], key) -> dict:
    groups: dict = defaultdict(list)
    for t in trades:
        groups[key(t)].append(t)
    out = {}
    for k, ts in groups.items():
        wins = sum(t.net for t in ts if t.net > 0)
        loss = -sum(t.net for t in ts if t.net <= 0)
        out[k] = {"trades": len(ts), "net": round(sum(t.net for t in ts), 4),
                  "win_rate_pct": round(sum(1 for t in ts if t.net > 0) / len(ts) * 100, 1),
                  "expectancy_r": round(sum(t.r_multiple for t in ts) / len(ts), 3),
                  "profit_factor": round(wins / loss, 3) if loss > 0 else None}
    return out


def monte_carlo(trades: list[Trade], start_capital: float, runs: int = 500, seed: int = 7) -> dict | None:
    """İşlem sırasını karıştırarak düşüş dağılımı (sıra şansına duyarlılık)."""
    if len(trades) < 10:
        return None
    rng = random.Random(seed)
    nets = [t.net for t in trades]
    dds = []
    for _ in range(runs):
        rng.shuffle(nets)
        eq, curve = start_capital, [start_capital]
        for x in nets:
            eq += x
            curve.append(eq)
        dds.append(max_drawdown(curve)[1])
    dds.sort()
    pick = lambda q: round(dds[min(len(dds) - 1, int(q * len(dds)))], 3)  # noqa: E731
    return {"runs": runs, "max_dd_pct_p50": pick(0.5), "max_dd_pct_p95": pick(0.95), "max_dd_pct_worst": round(dds[-1], 3)}


def windows(trades: list[Trade], start_ms: int, end_ms: int, k: int = 4) -> list[dict]:
    """Walk-forward: dönemi k eşit pencereye böler, her pencerede ayrı sonuç."""
    span = (end_ms - start_ms) / k
    out = []
    for i in range(k):
        a, b = start_ms + i * span, start_ms + (i + 1) * span
        ts = [t for t in trades if a <= t.exit_ms < b]
        out.append({"window": i + 1, "from": datetime.fromtimestamp(a / 1000, timezone.utc).date().isoformat(),
                    "to": datetime.fromtimestamp(b / 1000, timezone.utc).date().isoformat(),
                    "trades": len(ts), "net": round(sum(t.net for t in ts), 4),
                    "expectancy_r": round(sum(t.r_multiple for t in ts) / len(ts), 3) if ts else None})
    return out
