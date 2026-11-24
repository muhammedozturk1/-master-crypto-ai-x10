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
from app.database.models import BotState, ScanSnapshot, SignalRecord
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


def save_signals(signals: list[Signal], bot_ids: list[str], errors: dict[str, str]) -> tuple[int, int]:
    """Sinyalleri tekrarsız kaydeder; bot durumlarını günceller. (yeni, işlem sinyali) döndürür."""
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
        rep.symbols = len(cands)
        signals: list[Signal] = []
        errors: dict[str, str] = {}
        sem = asyncio.Semaphore(self.settings.runner_concurrency)

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
            run_bots(symbol_bots, ctx)

        if symbol_bots and cands:
            await asyncio.gather(*(one(c) for c in cands))
        if wide_bots and btc:
            extra = {name: fn() for name, fn in self.providers.items()}
            run_bots(wide_bots, MarketContext(symbol="PIYASA", timeframe=tf, candles=btc,
                                              allow_short=linear, extra=extra))
        rep.signals_new, rep.signals_trade = save_signals(signals, rep.bots, errors)
        rep.bot_errors = errors
        rep.duration_ms = int((time.monotonic() - t0) * 1000)
        self.reports[tf] = rep
        log.info("Bot turu %s [%s]: %d/%d sembol, %d yeni sinyal (%d işlem), %d veri sorunu, %d eksik türev, %d ms",
                 tf, ",".join(rep.bots), rep.analyzed, rep.symbols, rep.signals_new, rep.signals_trade,
                 len(rep.data_problems), rep.missing_derivatives, rep.duration_ms)
        return rep

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
