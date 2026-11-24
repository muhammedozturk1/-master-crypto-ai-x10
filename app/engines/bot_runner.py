"""Çalışan botları yeni mum kapandığında çalıştırır ve sinyalleri kaydeder.

Akış (her zaman dilimi için): mum kapanır → o zaman diliminde çalışan açık botlar seçilir →
coin tarayıcının son aday listesi alınır → her aday için kapanmış mumlar (ve gerekiyorsa
funding, open interest, long/short oranı, son işlemler, emir defteri) çekilir ve kalite
kontrolünden geçer → botlar analiz yapar → piyasa geneli botlar (takvim, arbitraj) bir kez
çalışır → sinyaller tekrarsız kaydedilir. Botlar işlem açmaz; kararı ana robot verecek.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from app.bots.base import MarketContext, WorkerBot
from app.bots.registry import IMPLEMENTATIONS
from app.config import MarketType, Settings
from app.data.candles import INTERVAL_MINUTES, TIMEFRAME_TO_INTERVAL, closed_only, now_ms, validate_candles
from app.database.models import BotState, ScanSnapshot, SignalRecord, SystemState
from app.database.models import Decision as DecisionRecord
from app.engines.calendar_service import fng_sentiment, veto_status
from decimal import Decimal

from app.bots.bot07_risk import RiskManager, TradeRequest
from app.bots.bot08_portfolio import account_state
from app.database.models import AuditLog
from app.master_robot.decision_engine import Decision, MarketInputs, decide
from app.database.session import session_scope
from app.domain.signals import Action, Signal
from app.exchange.bybit_public import BybitPublic

log = logging.getLogger("x10.runner")

HTF = {"5m": "15m", "15m": "1h", "30m": "4h", "1h": "4h", "4h": "1d"}
DERIV_PERIOD = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h"}
CLOSE_DELAY_MS = 8_000  # borsanın mumu kesinleştirmesi için bekleme


@dataclass
class RunReport:
    timeframe: str
    started_at: str
    bots: list[str] = field(default_factory=list)
    symbols: int = 0
    analyzed: int = 0
    data_problems: dict[str, str] = field(default_factory=dict)
    missing_derivatives: int = 0
    signals_new: int = 0
    signals_trade: int = 0
    decisions: dict[str, int] = field(default_factory=dict)
    bot_errors: dict[str, str] = field(default_factory=dict)
    duration_ms: int = 0


def latest_candidates(limit: int) -> list[dict]:
    with session_scope() as s:
        snap = s.scalars(select(ScanSnapshot).where(ScanSnapshot.ok.is_(True))
                         .order_by(ScanSnapshot.id.desc()).limit(1)).first()
        return (snap.result.get("candidates", []) if snap else [])[:limit]


def active_bots() -> list[WorkerBot]:
    with session_scope() as s:
        enabled = {b.bot_id for b in s.scalars(select(BotState).where(BotState.enabled.is_(True)))}
    return [cls() for bid, cls in IMPLEMENTATIONS.items() if bid in enabled]


def save_signals(signals: list[Signal], bot_ids: list[str], errors: dict[str, str],
                 new_keys: set[str] | None = None) -> tuple[int, int]:
    """Sinyalleri tekrarsız kaydeder; bot durumlarını günceller. (yeni, işlem sinyali) döndürür.
    new_keys verilirse yeni kaydedilen sinyallerin anahtarları buna eklenir."""
    new = trade = 0
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        keys = [sg.details.get("dedup_key") for sg in signals if sg.details.get("dedup_key")]
        existing = set(s.scalars(select(SignalRecord.dedup_key).where(SignalRecord.dedup_key.in_(keys)))) if keys else set()
        per_bot: dict[str, int] = {}
        for sg in signals:
            key = sg.details.get("dedup_key")
            if key and key in existing:
                continue
            existing.add(key)
            if new_keys is not None and key:
                new_keys.add(key)
            s.add(SignalRecord(
                dedup_key=key, bot_id=sg.bot_id, strategy_id=sg.strategy_id, symbol=sg.symbol,
                timeframe=sg.timeframe, action=sg.action.value, confidence=sg.confidence,
                payload=sg.model_dump(mode="json"), created_at=sg.created_at,
            ))
            new += 1
            if sg.action != Action.BEKLE:
                trade += 1
                per_bot[sg.bot_id] = per_bot.get(sg.bot_id, 0) + 1
        for bid in bot_ids:
            st = s.get(BotState, bid)
            if st is None:
                continue
            st.last_analysis_at = now
            st.signals_total += per_bot.get(bid, 0)
            st.status = "hata" if bid in errors else "calisiyor"
            st.last_error = errors.get(bid)
    return new, trade


def save_decision(d: Decision) -> bool:
    """Kararı kaydeder ve dayandığı sinyalleri karara bağlar. Aynı karar ikinci kez yazılmaz."""
    with session_scope() as s:
        if s.scalars(select(DecisionRecord.id).where(DecisionRecord.decision_key == d.key)).first():
            return False
        rec = DecisionRecord(
            decision_key=d.key, symbol=d.symbol, timeframe=d.timeframe, direction=d.direction,
            outcome=d.outcome, score=d.score, strategy_id=d.strategy, market_regime=d.regime.regime.value,
            reason=d.reason[:2000], contributions=d.contributions, plan=d.plan, model_version=d.model_version,
        )
        s.add(rec)
        s.flush()
        if d.signal_keys:
            for sr in s.scalars(select(SignalRecord).where(SignalRecord.dedup_key.in_(d.signal_keys))):
                sr.decision_id = rec.id
                if d.outcome in ("RISK_KONTROLU", "YUKSEK_ONCELIK") and sr.action == ("AL" if d.direction == "LONG" else "SAT"):
                    st = s.get(BotState, sr.bot_id)
                    if st:
                        st.signals_accepted += 1
    return True


class BotRunner:
    def __init__(self, market: BybitPublic, settings: Settings,
                 providers: dict[str, Callable[[], object]] | None = None):
        self.market = market
        self.settings = settings
        self.providers = providers or {}   # piyasa geneli botlara verilecek durumlar (takvim, arbitraj)
        self.last_bar: dict[str, int] = {}
        self.reports: dict[str, RunReport] = {}

    async def _fetch(self, symbol: str, tf: str, limit: int):
        raw = await self.market.klines(symbol, TIMEFRAME_TO_INTERVAL[tf], limit + 1)
        cs = closed_only(raw)
        validate_candles(cs)
        return cs

    async def _derivatives(self, symbol: str, tf: str) -> dict:
        """BOT06 için ek veriler; alınamayan kalem eksik bırakılır (sinyal üretimi durmaz)."""
        p = DERIV_PERIOD.get(tf, "15min")
        names = ("open_interest", "funding", "account_ratio", "trades", "orderbook")
        results = await asyncio.gather(
            self.market.open_interest(symbol, p, 48),
            self.market.funding_history(symbol, 10),
            self.market.account_ratio(symbol, p, 24),
            self.market.recent_trades(symbol, 1000),
            self.market.orderbook(symbol, 50),
            return_exceptions=True,
        )
        return {n: r for n, r in zip(names, results) if not isinstance(r, Exception)}

    async def run_timeframe(self, tf: str) -> RunReport:
        t0 = time.monotonic()
        rep = RunReport(tf, datetime.now(timezone.utc).isoformat())
        all_bots = [b for b in active_bots() if tf in b.timeframes]
        symbol_bots = [b for b in all_bots if not b.market_wide]
        wide_bots = [b for b in all_bots if b.market_wide]
        rep.bots = [b.bot_id for b in all_bots]
        linear = self.settings.market_type == MarketType.LINEAR
        need_deriv = linear and any(b.needs_derivatives for b in symbol_bots)
        cands = latest_candidates(self.settings.scan_top_n) if symbol_bots else []
        self._instruments = {c["symbol"]: c for c in cands}
        rep.symbols = len(cands)
        signals: list[Signal] = []
        errors: dict[str, str] = {}
        sem = asyncio.Semaphore(self.settings.runner_concurrency)
        contexts: dict[str, MarketContext] = {}

        btc: list = []
        if all_bots:
            try:  # göreceli güç (BOT02) ve piyasa geneli botların zaman ekseni için BTC
                btc = await self._fetch("BTCUSDT", tf, 260)
            except Exception as exc:  # noqa: BLE001
                rep.data_problems["BTCUSDT(referans)"] = str(exc)[:160]
        btc_closes = [float(x.close) for x in btc]

        def run_bots(bots: list[WorkerBot], ctx: MarketContext) -> None:
            for bot in bots:
                try:
                    signals.extend(bot.analyze(ctx))
                except Exception as exc:  # noqa: BLE001 – bir botun hatası diğerlerini durdurmaz
                    errors[bot.bot_id] = f"{ctx.symbol}: {exc}"[:300]
                    log.exception("%s analizi başarısız (%s)", bot.bot_id, ctx.symbol)

        async def one(c: dict) -> None:
            sym = c["symbol"]
            async with sem:
                try:
                    candles = await self._fetch(sym, tf, 260)
                    htf_tf = HTF.get(tf)
                    htf = await self._fetch(sym, htf_tf, 120) if htf_tf else None
                except Exception as exc:  # noqa: BLE001 – kalite sorunu veya bağlantı hatası
                    rep.data_problems[sym] = str(exc)[:160]
                    return
                extra: dict = {"btc_closes": btc_closes} if btc_closes else {}
                if need_deriv:
                    d = await self._derivatives(sym, tf)
                    if len(d) < 5:
                        rep.missing_derivatives += 1
                    extra.update(d)
            ctx = MarketContext(symbol=sym, timeframe=tf, candles=candles, htf_candles=htf,
                                spread_pct=float(c.get("spread_pct") or 0), allow_short=linear,
                                taker_fee_pct=self.settings.taker_fee_pct, extra=extra)
            rep.analyzed += 1
            contexts[sym] = ctx
            run_bots(symbol_bots, ctx)

        if symbol_bots and cands:
            await asyncio.gather(*(one(c) for c in cands))
        if wide_bots and btc:
            extra = {name: fn() for name, fn in self.providers.items()}
            run_bots(wide_bots, MarketContext(symbol="PIYASA", timeframe=tf, candles=btc,
                                              allow_short=linear, extra=extra))
        new_keys: set[str] = set()
        rep.signals_new, rep.signals_trade = save_signals(signals, rep.bots, errors, new_keys)
        rep.bot_errors = errors
        try:
            rep.decisions = self._decide(contexts, signals, new_keys)
        except Exception:  # noqa: BLE001 – karar hatası sinyal kaydını bozmaz
            log.exception("Ana robot kararı başarısız (%s)", tf)
            rep.decisions = {"HATA": 1}
        rep.duration_ms = int((time.monotonic() - t0) * 1000)
        self.reports[tf] = rep
        log.info("Bot turu %s [%s]: %d/%d sembol, %d yeni sinyal (%d işlem), %d veri sorunu, %d eksik türev, "
                 "kararlar %s, %d ms", tf, ",".join(rep.bots), rep.analyzed, rep.symbols, rep.signals_new,
                 rep.signals_trade, len(rep.data_problems), rep.missing_derivatives, rep.decisions or "{}", rep.duration_ms)
        return rep

    def market_inputs(self) -> MarketInputs:
        mi = MarketInputs()
        cal = self.providers.get("calendar")
        st = cal() if cal else None
        if st is not None and getattr(st, "fetched_at", None):
            v = veto_status(st.events, datetime.now(timezone.utc), self.settings.news_blackout_before_min,
                            self.settings.news_blackout_after_min)
            mi.news_veto = v["veto_active"]
            if v["veto_active"]:
                ev = v["veto_events"][0]
                mi.veto_reason = f"haber vetosu: {ev['title']} ({ev['time']})"
            mi.sentiment = fng_sentiment(st.fear_greed)
        with session_scope() as s:
            mi.bot_weights = {b.bot_id: float(b.weight) for b in s.scalars(select(BotState))}
            sysst = s.get(SystemState, 1)
            if sysst:
                mi.trading_enabled, mi.safe_mode = sysst.trading_enabled, sysst.safe_mode
        return mi

    def _decide(self, contexts: dict[str, MarketContext], signals: list[Signal], new_keys: set[str]) -> dict[str, int]:
        """Yeni işlem sinyali olan her sembol için ana robot kararı; kararlar kaydedilir."""
        fresh = {s.symbol for s in signals if s.action != Action.BEKLE and s.details.get("dedup_key") in new_keys}
        if not fresh:
            return {}
        mi = self.market_inputs()
        out: dict[str, int] = {}
        for sym in sorted(fresh):
            ctx = contexts.get(sym)
            if ctx is None:
                continue
            d = decide(ctx, [s for s in signals if s.symbol == sym], mi)
            if d and d.outcome in ("RISK_KONTROLU", "YUKSEK_ONCELIK"):
                self._risk_check(d)
            if d and save_decision(d):
                out[d.outcome] = out.get(d.outcome, 0) + 1
        return out

    def _risk_check(self, d: Decision) -> None:
        """BOT07 zorunlu risk kontrolü ve pozisyon büyüklüğü; sonuç karara işlenir."""
        p = d.plan or {}
        long = d.direction == "LONG"
        inst = getattr(self, "_instruments", {}).get(d.symbol, {})
        def dec(x):
            return Decimal(str(x)) if x not in (None, "", "None") else None
        req = TradeRequest(
            symbol=d.symbol, long=long,
            entry=dec(p["entry_high"] if long else p["entry_low"]), stop=dec(p["stop_loss"]),
            target=dec(p["take_profit"]), cost_pct=dec(p.get("estimated_cost_pct")) or Decimal("0.12"),
            regime=d.regime.regime.value, qty_step=dec(inst.get("qty_step")),
            min_qty=dec(inst.get("min_order_qty")), min_notional=dec(inst.get("min_notional")),
        )
        with session_scope() as s:
            acc = account_state(s, self.settings.risk.starting_capital_usd, self.settings.mode.value)
            v = RiskManager(self.settings.risk).evaluate(req, acc)
            if v.halt:
                st = s.get(SystemState, 1)
                if v.halt == "SAFE_MODE":
                    st.safe_mode = True
                    st.safe_mode_reason = v.reasons[0]
                st.trading_enabled = False
                s.add(AuditLog(event="risk_durdurma", actor="BOT07", detail={"halt": v.halt, "reasons": v.reasons}))
        d.contributions["risk"] = v.to_dict()
        if v.approved:
            d.outcome = "ONAYLANDI"
            d.plan = {**p, "qty": str(v.qty), "notional": str(v.notional), "margin": str(v.margin),
                      "leverage": v.leverage, "risk_usd": str(v.risk_usd)}
            d.reason += f"; BOT07 onayı: {v.qty} adet, {v.notional:.2f} USD, risk {v.risk_usd:.3f} USD"
        else:
            d.outcome = "RISK_RED"
            d.reason += "; BOT07 reddi: " + "; ".join(v.reasons)

    def due_timeframes(self, at_ms: int | None = None) -> list[str]:
        t = now_ms() if at_ms is None else at_ms
        due = []
        for tf in self.settings.runner_timeframes:
            step = INTERVAL_MINUTES[TIMEFRAME_TO_INTERVAL[tf]] * 60_000
            boundary = t - (t % step)
            if t - boundary >= CLOSE_DELAY_MS and self.last_bar.get(tf) != boundary:
                due.append(tf)
                self.last_bar[tf] = boundary
        return due

    async def loop(self) -> None:
        await asyncio.sleep(20)  # ilk taramanın bitmesini bekle
        while True:
            for tf in self.due_timeframes():
                try:
                    await self.run_timeframe(tf)
                except Exception:  # noqa: BLE001
                    log.exception("Bot turu başarısız: %s", tf)
            await asyncio.sleep(15)
