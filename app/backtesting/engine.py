"""Backtest motoru (şartname bölüm 19).

Gelecek verisi sızıntısı olmaması için kurallar:
  - Sinyal yalnız KAPANMIŞ mumla, o muma kadarki veriyle üretilir (bot bağlamı son 260 mum).
  - Üst zaman dilimi mumlarından yalnız sinyal anında kapanmış olanlar kullanılır.
  - Giriş, sinyal mumundan SONRAKİ mumun açılışında yapılır.
  - Stop ve hedef mum içinde test edilir; aynı mumda ikisi de görülürse önce stop
    (muhafazakâr); boşluklu açılış stopu geçmişse çıkış açılış fiyatından.
  - Kâr kilidi stopu mum kapanışında hesaplanır ve ANCAK bir sonraki mumdan itibaren geçerlidir.
Maliyetler: taker komisyonu (giriş ve çıkış), yarım spread ve kayma her işlem yönünde.

İki mod:
  strateji : her strateji sinyali bağımsız işlem, işlem başına sabit 1 USD risk (strateji ve bot
             karşılaştırması ve ağırlık önerisi için)
  sistem   : ana robot birleştirmesi (75+ puan) + BOT07 risk katmanı + 200 USD bileşik kasa
             (gerçek çalışma biçimine en yakın)

Bilinen sınırlar (raporda da yazılır): BOT06 (geçmiş funding/OI/işlem akışı), BOT05 (geçmiş
takvim) ve BOT04 backtest'te yoktur; emir defteri derinliği ve kısmi gerçekleşme modellenmez.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.bots.base import MarketContext
from app.bots.bot07_risk import RiskManager, TradeRequest
from app.bots.bot08_portfolio import AccountState
from app.bots.registry import IMPLEMENTATIONS
from app.backtesting.metrics import Trade, breakdown, monte_carlo, summarize, windows
from app.config import RiskLimits
from app.data.candles import Candle
from app.domain.signals import Action, Signal
from app.engines import profit_lock as PL
from app.indicators import core as I
from app.master_robot.decision_engine import MarketInputs, decide

WINDOW = 260
CANDLE_BOTS = ("BOT01", "BOT02", "BOT03", "BOT10")


@dataclass
class BacktestParams:
    timeframe: str = "15m"
    mode: str = "strateji"            # strateji | sistem
    profit_lock: bool = True
    bots: tuple[str, ...] = ("BOT01", "BOT02", "BOT10")
    fee_pct: float = 0.055
    half_spread_pct: float = 0.01
    slippage_pct: float = 0.02
    fixed_risk_usd: float = 1.0       # strateji modunda işlem başı risk
    start_capital: float = 200.0
    allow_short: bool = True
    limits: RiskLimits = field(default_factory=RiskLimits)


@dataclass
class _Pos:
    symbol: str
    strategy: str
    bot_id: str
    long: bool
    entry: float
    qty: float
    stop: float
    initial_stop: float
    tp: float
    entry_ms: int
    entry_fee: float
    risk_usd: float
    peak_pct: Decimal = Decimal(0)
    high_water: Decimal | None = None
    state: int = 0
    peak_net_usd: float = 0.0


@dataclass
class _Pending:
    symbol: str
    strategy: str
    bot_id: str
    long: bool
    stop: float
    tp: float
    qty: float | None                 # None → girişte sabit riskle hesapla
    risk_usd: float


def _fill(price: float, long: bool, p: BacktestParams, entering: bool) -> float:
    adj = (p.half_spread_pct + p.slippage_pct) / 100
    worse_up = long == entering       # long giriş / short çıkış → daha yüksek fiyat
    return price * (1 + adj) if worse_up else price * (1 - adj)


class Backtester:
    def __init__(self, data: dict[str, list[Candle]], htf: dict[str, list[Candle]] | None,
                 btc: list[Candle] | None, params: BacktestParams):
        self.data, self.htf, self.btc, self.p = data, htf or {}, btc or [], params
        self.bots = [IMPLEMENTATIONS[b]() for b in params.bots if b in IMPLEMENTATIONS and b in CANDLE_BOTS]
        self.idx = {s: {c.start_ms: i for i, c in enumerate(cs)} for s, cs in data.items()}
        self.atr = {s: I.atr([float(c.high) for c in cs], [float(c.low) for c in cs], [float(c.close) for c in cs], 14)
                    for s, cs in data.items()}
        self.htf_end = {s: [c.end_ms for c in cs] for s, cs in self.htf.items()}
        self.btc_idx = {c.start_ms: i for i, c in enumerate(self.btc)}
        self.trades: list[Trade] = []
        self.equity: list[tuple[int, float]] = []
        self.open: list[_Pos] = []
        self.pending: list[_Pending] = []
        self.realized = 0.0
        self.skipped: dict[str, int] = {}
        self.decisions: dict[str, int] = {}

    # ------------------------------------------------------------------
    def run(self) -> dict:
        times = sorted({c.start_ms for cs in self.data.values() for c in cs})
        for t in times:
            for sym, cs in self.data.items():
                i = self.idx[sym].get(t)
                if i is None:
                    continue
                self._fill_pending(sym, cs[i])
                self._process_bar(sym, cs, i)
            self._mark_equity(t)
            for sym, cs in self.data.items():
                i = self.idx[sym].get(t)
                if i is not None and i >= WINDOW - 1 and i < len(cs) - 1:
                    self._signals(sym, cs, i)
        # Dönem sonunda açık kalanlar son kapanıştan kapatılır
        for pos in list(self.open):
            last = self.data[pos.symbol][-1]
            self._close(pos, _fill(float(last.close), pos.long, self.p, False), last.end_ms, "donem_sonu")
        return self._report(times)

    def _fill_pending(self, sym: str, bar: Candle) -> None:
        for pe in [x for x in self.pending if x.symbol == sym]:
            self.pending.remove(pe)
            o = float(bar.open)
            entry = _fill(o, pe.long, self.p, True)
            if (pe.long and entry <= pe.stop) or (not pe.long and entry >= pe.stop):
                self.skipped["acilis_stopun_otesinde"] = self.skipped.get("acilis_stopun_otesinde", 0) + 1
                continue
            cost = entry * (2 * self.p.fee_pct + 2 * self.p.half_spread_pct + 2 * self.p.slippage_pct) / 100
            unit = abs(entry - pe.stop) + cost
            qty = pe.qty if pe.qty is not None else pe.risk_usd / unit
            if qty <= 0:
                continue
            self.open.append(_Pos(sym, pe.strategy, pe.bot_id, pe.long, entry, qty, pe.stop, pe.stop, pe.tp,
                                  bar.start_ms, qty * entry * self.p.fee_pct / 100, qty * unit,
                                  high_water=Decimal(str(entry))))

    def _process_bar(self, sym: str, cs: list[Candle], i: int) -> None:
        bar = cs[i]
        o, h, l, c = float(bar.open), float(bar.high), float(bar.low), float(bar.close)
        for pos in [x for x in self.open if x.symbol == sym]:
            stop_hit = (l <= pos.stop) if pos.long else (h >= pos.stop)
            tp_hit = (h >= pos.tp) if pos.long else (l <= pos.tp)
            if stop_hit:
                px = min(o, pos.stop) if pos.long else max(o, pos.stop)
                self._close(pos, _fill(px, pos.long, self.p, False), bar.end_ms,
                            "kar_kilidi" if pos.state >= PL.BREAK_EVEN and pos.stop != pos.initial_stop else "stop")
                continue
            if tp_hit:
                px = max(o, pos.tp) if pos.long else min(o, pos.tp)
                self._close(pos, _fill(px, pos.long, self.p, False), bar.end_ms, "hedef")
                continue
            self._lock(pos, c, self.atr[sym][i])

    def _lock(self, pos: _Pos, close: float, atr: float | None) -> None:
        fee = Decimal(str(self.p.fee_pct))
        view = PL.PositionView(pos.long, Decimal(str(pos.qty)), Decimal(str(pos.entry)), Decimal(str(pos.entry_fee)),
                               Decimal(str(pos.initial_stop)), Decimal(str(pos.stop)), pos.peak_pct,
                               pos.high_water or Decimal(str(pos.entry)), pos.state, pos.strategy, fee)
        d = PL.evaluate(view, Decimal(str(close)), Decimal(str(atr)) if atr else None)
        pos.peak_pct, pos.high_water = d.peak_net_pct, d.high_water
        pos.peak_net_usd = max(pos.peak_net_usd, float(d.peak_net_pct) / 100 * pos.qty * pos.entry)
        if not self.p.profit_lock:
            return
        pos.state = d.state
        if d.new_stop is not None:
            pos.stop = float(d.new_stop)        # bir sonraki mumdan itibaren geçerli

    def _close(self, pos: _Pos, px: float, t: int, reason: str) -> None:
        d = 1 if pos.long else -1
        exit_fee = pos.qty * px * self.p.fee_pct / 100
        net = (px - pos.entry) * pos.qty * d - pos.entry_fee - exit_fee
        self.realized += net
        self.open.remove(pos)
        self.trades.append(Trade(pos.symbol, pos.strategy, pos.bot_id, pos.long, pos.entry_ms, t, pos.entry, px,
                                 pos.qty, pos.entry_fee + exit_fee, net, pos.risk_usd, pos.peak_net_usd, reason))

    def _mark_equity(self, t: int) -> None:
        unreal = 0.0
        for pos in self.open:
            i = self.idx[pos.symbol].get(t)
            if i is None:
                continue
            px = float(self.data[pos.symbol][i].close)
            unreal += (px - pos.entry) * pos.qty * (1 if pos.long else -1) - pos.entry_fee
        self.equity.append((t, self.p.start_capital + self.realized + unreal))

    # ------------------------------------------------------------------
    def _ctx(self, sym: str, cs: list[Candle], i: int) -> MarketContext:
        close_ms = cs[i].end_ms
        htf = None
        if sym in self.htf:
            k = bisect.bisect_right(self.htf_end[sym], close_ms)       # yalnız kapanmış üst ZD mumları
            htf = self.htf[sym][max(0, k - 120):k]
        extra = {}
        bi = self.btc_idx.get(cs[i].start_ms)
        if bi is not None:
            extra["btc_closes"] = [float(c.close) for c in self.btc[max(0, bi - WINDOW + 1):bi + 1]]
        return MarketContext(symbol=sym, timeframe=self.p.timeframe, candles=cs[i - WINDOW + 1:i + 1],
                             htf_candles=htf, spread_pct=self.p.half_spread_pct * 2,
                             taker_fee_pct=self.p.fee_pct, allow_short=self.p.allow_short, extra=extra)

    def _signals(self, sym: str, cs: list[Candle], i: int) -> None:
        ctx = self._ctx(sym, cs, i)
        sigs: list[Signal] = []
        for b in self.bots:
            if self.p.timeframe in b.timeframes:
                sigs.extend(s for s in b.analyze(ctx) if s.action != Action.BEKLE)
        if not sigs:
            return
        busy = {(x.symbol, x.strategy.split("+")[0]) for x in self.open} | \
               {(x.symbol, x.strategy.split("+")[0]) for x in self.pending}
        if self.p.mode == "strateji":
            for s in sigs:
                key = (sym, s.strategy_id.split("+")[0])
                if key in busy:
                    continue
                busy.add(key)
                self.pending.append(_Pending(sym, s.strategy_id, s.bot_id, s.action == Action.AL,
                                             float(s.stop_loss), float(s.take_profit), None, self.p.fixed_risk_usd))
            return
        # sistem modu: ana robot + BOT07
        if any(x.symbol == sym for x in self.open + self.pending):
            return
        d = decide(ctx, sigs, MarketInputs())
        if d is None:
            return
        self.decisions[d.outcome] = self.decisions.get(d.outcome, 0) + 1
        if d.outcome not in ("RISK_KONTROLU", "YUKSEK_ONCELIK"):
            return
        plan, long = d.plan, d.direction == "LONG"
        req = TradeRequest(sym, long, Decimal(plan["entry_high"] if long else plan["entry_low"]),
                           Decimal(plan["stop_loss"]), Decimal(plan["take_profit"]),
                           Decimal(plan["estimated_cost_pct"]), regime=d.regime.regime.value)
        v = RiskManager(self.p.limits).evaluate(req, self._account(cs[i].end_ms))
        if not v.approved:
            self.decisions["RISK_RED"] = self.decisions.get("RISK_RED", 0) + 1
            return
        self.pending.append(_Pending(sym, plan["strategy"], plan["bot_id"], long, float(plan["stop_loss"]),
                                     float(plan["take_profit"]), float(v.qty), float(v.risk_usd)))

    def _account(self, now_ms: int) -> AccountState:
        D = lambda x: Decimal(str(round(x, 8)))  # noqa: E731
        now = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week = day - timedelta(days=day.weekday())
        def realized_before(t):
            ms = t.timestamp() * 1000
            return sum(tr.net for tr in self.trades if tr.exit_ms < ms)
        eq = self.equity[-1][1] if self.equity else self.p.start_capital
        peak = max([v for _, v in self.equity] or [eq])
        streak, last_loss = 0, None
        for tr in reversed(self.trades):
            if tr.net < 0:
                streak += 1
                last_loss = last_loss or datetime.fromtimestamp(tr.exit_ms / 1000, timezone.utc)
            else:
                break
        margin = sum(x.qty * x.entry / self.p.limits.leverage for x in self.open)
        day_eq = self.p.start_capital + realized_before(day)
        week_eq = self.p.start_capital + realized_before(week)
        return AccountState(
            starting_capital=D(self.p.start_capital), equity=D(eq), realized_net=D(self.realized),
            unrealized_net=D(eq - self.p.start_capital - self.realized), fees_paid=D(0), margin_in_use=D(margin),
            available=D(eq - margin), open_risk=D(sum(x.risk_usd for x in self.open)),
            open_positions=len(self.open) + len(self.pending), open_symbols=sorted({x.symbol for x in self.open}),
            peak_equity=D(peak), drawdown_pct=D((peak - eq) / peak * 100 if peak > 0 else 0),
            day_start_equity=D(day_eq), week_start_equity=D(week_eq), month_start_equity=D(self.p.start_capital),
            day_pnl=D(eq - day_eq), week_pnl=D(eq - week_eq), month_pnl=D(0),
            consecutive_losses=streak, last_loss_at=last_loss, as_of=now)

    def _report(self, times: list[int]) -> dict:
        start, end = (times[0], times[-1]) if times else (0, 0)
        trades = sorted(self.trades, key=lambda t: t.exit_ms)
        split = start + (end - start) * 0.7
        ins = [t for t in trades if t.exit_ms < split]
        oos = [t for t in trades if t.exit_ms >= split]
        return {
            "params": {"timeframe": self.p.timeframe, "mode": self.p.mode, "profit_lock": self.p.profit_lock,
                       "bots": [b.bot_id for b in self.bots], "fee_pct": self.p.fee_pct,
                       "half_spread_pct": self.p.half_spread_pct, "slippage_pct": self.p.slippage_pct,
                       "symbols": sorted(self.data), "from": _iso(start), "to": _iso(end)},
            "summary": summarize(trades, self.equity, self.p.start_capital),
            "by_strategy": breakdown(trades, lambda t: t.strategy.split("+")[0]),
            "by_bot": breakdown(trades, lambda t: t.bot_id),
            "by_symbol": breakdown(trades, lambda t: t.symbol),
            "in_sample_70": summarize(ins, [(0, self.p.start_capital), (0, self.p.start_capital + sum(t.net for t in ins))],
                                      self.p.start_capital) if ins else None,
            "out_of_sample_30": summarize(oos, [(0, self.p.start_capital), (0, self.p.start_capital + sum(t.net for t in oos))],
                                          self.p.start_capital) if oos else None,
            "walk_forward": windows(trades, start, end, 4),
            "monte_carlo": monte_carlo(trades, self.p.start_capital),
            "decisions": self.decisions or None,
            "skipped": self.skipped,
            "limitations": ["BOT06, BOT05 ve BOT04 geçmiş veri olmadığı için backtest'e dahil değil",
                            "emir defteri derinliği ve kısmi gerçekleşme modellenmedi",
                            "parametre optimizasyonu yapılmadı; walk-forward pencereleri tutarlılık kontrolüdür"],
        }


def _iso(ms: int) -> str | None:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else None
