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
from app.config import get_settings
from app.database.session import configure, init_db
from app.engines.arbitrage_service import ArbitrageService
from app.engines.bot_runner import BotRunner, latest_candidates
from app.engines.calendar_service import CalendarService
from app.engines.scanner_service import scanner_loop
from app.exchange.bybit_public import BybitPublic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("x10")


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
    app.state.runner = BotRunner(app.state.market, settings, providers) if settings.runner_enabled else None

    tasks = []
    if settings.scanner_enabled:
        tasks.append(asyncio.create_task(scanner_loop(app.state.market, settings)))
    if app.state.calendar:
        tasks.append(asyncio.create_task(app.state.calendar.loop()))
    if app.state.arbitrage:
        tasks.append(asyncio.create_task(app.state.arbitrage.loop()))
    if app.state.runner:
        tasks.append(asyncio.create_task(app.state.runner.loop()))
    log.info("Başlatıldı: mod=%s borsa=%s demo=%s tarayıcı=%s bot_döngüsü=%s takvim=%s arbitraj=%s",
             settings.mode.value, settings.exchange, settings.bybit_demo, settings.scanner_enabled,
             settings.runner_enabled, settings.calendar_enabled, settings.arbitrage_enabled)
    yield
    for t in tasks:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t
    for svc in (app.state.calendar, app.state.arbitrage):
        if svc:
            await svc.close()
    await app.state.market.close()
    app.state.market = app.state.runner = app.state.calendar = app.state.arbitrage = None
    log.info("Durduruldu")


app = FastAPI(title="MASTER CRYPTO AI X10", version="0.4.0", lifespan=lifespan)
app.include_router(router)
app.include_router(market_router)
app.include_router(signals_router)
