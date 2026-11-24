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
from app.engines.bot_runner import BotRunner
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
    app.state.runner = BotRunner(app.state.market, settings) if settings.runner_enabled else None
    tasks = []
    if settings.scanner_enabled:
        tasks.append(asyncio.create_task(scanner_loop(app.state.market, settings)))
    if app.state.runner:
        tasks.append(asyncio.create_task(app.state.runner.loop()))
    log.info("Başlatıldı: mod=%s borsa=%s demo=%s tarayıcı=%s bot_döngüsü=%s", settings.mode.value,
             settings.exchange, settings.bybit_demo, settings.scanner_enabled, settings.runner_enabled)
    yield
    for t in tasks:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t
    await app.state.market.close()
    app.state.market = None
    app.state.runner = None
    log.info("Durduruldu")


app = FastAPI(title="MASTER CRYPTO AI X10", version="0.3.0", lifespan=lifespan)
app.include_router(router)
app.include_router(market_router)
app.include_router(signals_router)
