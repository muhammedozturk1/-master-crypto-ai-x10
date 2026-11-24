"""MASTER CRYPTO AI X10 uygulama girişi."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config import get_settings
from app.database.session import configure, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("x10")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    configure(settings.sqlalchemy_url)
    init_db()
    log.info("Başlatıldı: mod=%s borsa=%s demo=%s", settings.mode.value, settings.exchange, settings.bybit_demo)
    yield
    log.info("Durduruldu")


app = FastAPI(title="MASTER CRYPTO AI X10", version="0.1.0", lifespan=lifespan)
app.include_router(router)
