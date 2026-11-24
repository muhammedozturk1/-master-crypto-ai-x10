"""MASTER CRYPTO AI X10 uygulama girişi."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.market import router as market_router
from app.api.routes import router
from app.api.signals import router as signals_router
from app.api.backtest import router as backtest_router
from app.engines.performance import learning_check
from app.config import get_settings
from app.database.session import configure, init_db
from app.engines.arbitrage_service import ArbitrageService
from app.engines.bot_runner import BotRunner, latest_candidates
from app.engines.calendar_service import CalendarService
from app.engines.scanner_service import scanner_loop
from app.bots.bot08_portfolio import account_state, record_snapshot
from app.database.session import session_scope
from app.exchange.bybit_public import BybitPublic
from app.bots.bot09_execution import ExecutionService
from app.config import TradingMode
from app.engines.position_monitor import PositionMonitor
from app.exchange.bybit_private import BybitPrivate
from app.execution.brokers import BybitBroker, SimBroker
from decimal import Decimal


def build_executor(settings, market):
    """Anahtar varsa Bybit (demo veya canlı), yoksa gerçek fiyatlı simülasyon.
    Canlı modda anahtar yoksa hiçbir şey çalıştırılmaz (simülasyon canlı yerine geçmez)."""
    if not settings.execution_enabled:
        return None
    fee = Decimal(str(settings.taker_fee_pct))
    if settings.bybit_api_key and settings.bybit_api_secret:
        priv = BybitPrivate(settings.bybit_api_key, settings.bybit_api_secret, settings.bybit_rest_url,
                            settings.market_type.value)
        venue = "BYBIT_DEMO" if settings.bybit_demo else "BYBIT"
        return ExecutionService(BybitBroker(priv, market, venue, fee), settings)
    if settings.mode == TradingMode.AUTONOMOUS_LIVE:
        log.error("Canlı modda API anahtarı yok: emir katmanı kapalı")
        return None
    return ExecutionService(SimBroker(market, fee), settings)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("x10")
logging.getLogger("httpx").setLevel(logging.WARNING)  # her HTTP isteğini loglama


async def equity_loop(settings, every_sec: int = 300) -> None:
    """BOT08: kasa anlık görüntüsü (zirve özkaynak ve drawdown takibi için)."""
    while True:
        try:
            with session_scope() as s:
                record_snapshot(s, account_state(s, settings.risk.starting_capital_usd, settings.mode.value))
                learning_check(s, settings.mode.value)
        except Exception:  # noqa: BLE001
            log.exception("Kasa anlık görüntüsü alınamadı")
        await asyncio.sleep(every_sec)


BACKTEST_SUITE = [("15m", "strateji"), ("15m", "sistem"), ("1h", "strateji"), ("1h", "sistem")]


async def backtest_suite(app: FastAPI, settings, delay_sec: int = 120) -> None:
    """Doğrulama paketi: her (zaman dilimi, mod) için henüz yoksa bir backtest çalıştırır, sırayla.
    15m için 30 gün, 1h için 90 gün; en likit 5 parite; kâr kilidi açık/kapalı karşılaştırmalı."""
    from app.backtesting.service import DEFAULT_BOTS, run_job
    from app.database.models import BacktestRun
    from sqlalchemy import select
    await asyncio.sleep(delay_sec)
    for tf, mode in BACKTEST_SUITE:
        symbols = [c["symbol"] for c in latest_candidates(5)]
        with session_scope() as s:
            done = {(r.params.get("timeframe"), r.params.get("mode")) for r in s.scalars(select(BacktestRun))
                    if r.status in ("BITTI", "CALISIYOR")}
            if (tf, mode) in done or not symbols:
                continue
            req = {"timeframe": tf, "days": 30 if tf == "15m" else 90, "symbols": symbols, "mode": mode,
                   "compare_profit_lock": True, "bots": list(DEFAULT_BOTS[tf]), "auto": True}
            run = BacktestRun(params=req)
            s.add(run)
            s.flush()
            rid = run.id
        log.info("Doğrulama backtest'i başlatıldı #%s: %s %s %s", rid, tf, mode, symbols)
        await run_job(rid, req, app.state.market, settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure(settings.sqlalchemy_url)
    init_db()
    app.state.market = getattr(app.state, "market", None) or BybitPublic(category=settings.market_type.value)
    app.state.calendar = CalendarService() if settings.calendar_enabled else None
    app.state.arbitrage = ArbitrageService(
        symbols_provider=lambda: [c["symbol"] for c in latest_candidates(10)] or ["BTCUSDT", "ETHUSDT"],
    ) if settings.arbitrage_enabled else None
    providers = {}
    if app.state.calendar:
        providers["calendar"] = lambda: app.state.calendar.state
    if app.state.arbitrage:
        providers["arbitrage"] = lambda: app.state.arbitrage.state
    app.state.execution = build_executor(settings, app.state.market)
    if app.state.execution and hasattr(app.state.execution.b, "p"):
        # Anahtarı doğrula: geçersizse simülasyona DÜŞÜLMEZ, emir katmanı kapatılır
        try:
            bal = await app.state.execution.b.p.wallet_balance()
            log.info("Bybit %s bağlantısı doğrulandı: toplam özkaynak %s USD (sistem kendi 200 USD kasasıyla boyutlar)",
                     app.state.execution.venue, bal.get("total_equity"))
        except Exception as exc:  # noqa: BLE001
            log.error("Bybit API anahtarı doğrulanamadı, emir katmanı KAPALI: %s", exc)
            with session_scope() as s:
                from app.database.models import AuditLog
                s.add(AuditLog(event="anahtar_dogrulanamadi", actor="BOT09", detail={"error": str(exc)[:300]}))
            await app.state.execution.b.p.close()
            app.state.execution = None
    app.state.monitor = PositionMonitor(app.state.execution, settings.monitor_interval_sec) if app.state.execution else None
    app.state.runner = BotRunner(app.state.market, settings, providers, app.state.execution) if settings.runner_enabled else None

    tasks = []
    if settings.scanner_enabled:
        tasks.append(asyncio.create_task(scanner_loop(app.state.market, settings)))
    if app.state.calendar:
        tasks.append(asyncio.create_task(app.state.calendar.loop()))
    if app.state.arbitrage:
        tasks.append(asyncio.create_task(app.state.arbitrage.loop()))
    if app.state.runner:
        tasks.append(asyncio.create_task(app.state.runner.loop()))
        tasks.append(asyncio.create_task(equity_loop(settings)))
    if app.state.monitor:
        tasks.append(asyncio.create_task(app.state.monitor.loop()))
    if settings.runner_enabled:
        tasks.append(asyncio.create_task(backtest_suite(app, settings)))
    log.info("Başlatıldı: mod=%s borsa=%s demo=%s tarayıcı=%s bot_döngüsü=%s takvim=%s arbitraj=%s yürütme=%s",
             settings.mode.value, settings.exchange, settings.bybit_demo, settings.scanner_enabled,
             settings.runner_enabled, settings.calendar_enabled, settings.arbitrage_enabled,
             app.state.execution.venue if app.state.execution else "kapalı")
    yield
    for t in tasks:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t
    for svc in (app.state.calendar, app.state.arbitrage):
        if svc:
            await svc.close()
    await app.state.market.close()
    if app.state.execution and hasattr(app.state.execution.b, "p"):
        await app.state.execution.b.p.close()
    app.state.market = app.state.runner = app.state.calendar = app.state.arbitrage = None
    app.state.execution = app.state.monitor = None
    log.info("Durduruldu")


app = FastAPI(title="MASTER CRYPTO AI X10", version="0.4.0", lifespan=lifespan)
app.include_router(router)
app.include_router(market_router)
app.include_router(signals_router)
app.include_router(backtest_router)
