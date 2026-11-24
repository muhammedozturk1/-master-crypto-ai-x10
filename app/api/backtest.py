"""Backtest ve performans uçları."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes import require_admin
from app.backtesting.service import DEFAULT_BOTS, MAX_DAYS, MAX_SYMBOLS, run_job
from app.config import Settings, get_settings
from app.database.models import AuditLog, BacktestRun, BotState
from app.database.session import get_session
from app.engines.bot_runner import latest_candidates
from app.engines.performance import report

router = APIRouter()


class BacktestRequest(BaseModel):
    timeframe: str = Field(default="15m", pattern="^(5m|15m|1h)$")
    days: int = Field(default=30, ge=3, le=MAX_DAYS)
    symbols: list[str] | None = Field(default=None, max_length=MAX_SYMBOLS)
    mode: str = Field(default="strateji", pattern="^(strateji|sistem)$")
    compare_profit_lock: bool = True


@router.post("/backtest", dependencies=[Depends(require_admin)])
async def start_backtest(body: BacktestRequest, request: Request, session: Session = Depends(get_session),
                         settings: Settings = Depends(get_settings)) -> dict:
    if session.scalars(select(BacktestRun).where(BacktestRun.status == "CALISIYOR")).first():
        raise HTTPException(409, "Zaten çalışan bir backtest var")
    symbols = [s.upper() for s in (body.symbols or [c["symbol"] for c in latest_candidates(5)])]
    if not symbols:
        raise HTTPException(400, "Sembol yok (tarayıcı henüz aday üretmedi)")
    req = {**body.model_dump(), "symbols": symbols, "bots": list(DEFAULT_BOTS[body.timeframe])}
    run = BacktestRun(params=req)
    session.add(run)
    session.flush()
    rid = run.id
    session.commit()
    market = request.app.state.market
    request.app.state.backtest_task = asyncio.create_task(run_job(rid, req, market, settings))
    return {"id": rid, "status": "CALISIYOR", "params": req,
            "note": "Sonuç için GET /backtest/{id}; süre sembol ve gün sayısına göre birkaç dakika."}


@router.get("/backtest")
def list_backtests(session: Session = Depends(get_session)) -> list[dict]:
    rows = session.scalars(select(BacktestRun).order_by(BacktestRun.id.desc()).limit(20))
    return [{"id": r.id, "status": r.status, "params": r.params, "created_at": r.created_at.isoformat(),
             "summary": (r.result or {}).get("summary"), "error": r.error} for r in rows]


@router.get("/backtest/{run_id}")
def get_backtest(run_id: int, session: Session = Depends(get_session)) -> dict:
    r = session.get(BacktestRun, run_id)
    if r is None:
        raise HTTPException(404, "Böyle bir backtest yok")
    return {"id": r.id, "status": r.status, "params": r.params, "result": r.result, "error": r.error,
            "created_at": r.created_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None}


class ApplyWeights(BaseModel):
    confirm: bool


@router.post("/backtest/{run_id}/apply-weights", dependencies=[Depends(require_admin)])
def apply_weights(run_id: int, body: ApplyWeights, session: Session = Depends(get_session)) -> dict:
    """Backtest'in önerdiği bot ağırlıklarını kullanıcı onayıyla uygular (otomatik değil)."""
    if not body.confirm:
        raise HTTPException(400, "Uygulamak için confirm=true gerekir")
    r = session.get(BacktestRun, run_id)
    if r is None or r.status != "BITTI":
        raise HTTPException(409, "Tamamlanmış backtest yok")
    applied = {}
    for bot, w in (r.result or {}).get("suggested_weights", {}).items():
        st = session.get(BotState, bot)
        if st is not None:
            applied[bot] = {"old": str(st.weight), "new": str(w["weight"])}
            st.weight = Decimal(str(w["weight"]))
    session.add(AuditLog(event="agirlik_uygulandi", actor="panel", detail={"backtest_id": run_id, "weights": applied}))
    return {"applied": applied}


@router.get("/performance")
def performance(session: Session = Depends(get_session), settings: Settings = Depends(get_settings)) -> dict:
    """Paper/canlı kapanan işlemlerin performansı (strateji, bot, sembol)."""
    return report(session, settings.mode.value, float(settings.risk.starting_capital_usd))
