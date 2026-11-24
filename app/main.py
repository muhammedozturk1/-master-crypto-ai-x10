"""MASTER CRYPTO AI X10 uygulama girişi."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.market import router as market_router
from app.api.routes import router
from app.config import get_settings
from app.database.session import configure, init_db
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
    task = None
    if settings.scanner_enabled:
        task = asyncio.create_task(scanner_loop(app.state.market, settings))
    log.info("Başlatıldı: mod=%s borsa=%s demo=%s tarayıcı=%s", settings.mode.value, settings.exchange,
             settings.bybit_demo, settings.scanner_enabled)
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await app.state.market.close()
    app.state.market = None
    log.info("Durduruldu")


app = FastAPI(title="MASTER CRYPTO AI X10", version="0.2.0", lifespan=lifespan)
app.include_router(router)
app.include_router(market_router)
