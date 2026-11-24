"""Panelin kullanacağı HTTP uçları."""

from __future__ import annotations

import hmac
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.bots.registry import BOTS, BOTS_BY_ID, IMPLEMENTATIONS, IMPLEMENTED_LAYERS
from app.config import Settings, get_settings
from app.database.models import AuditLog, BotState, SystemState
from app.database.session import get_session

router = APIRouter()


def require_admin(
    x_dashboard_token: str = Header(default=""), settings: Settings = Depends(get_settings)
) -> None:
    """Yazma işlemleri DASHBOARD_TOKEN ister; token ayarlı değilse yazma kapalıdır."""
    if not settings.dashboard_token:
        raise HTTPException(503, "Yazma işlemleri kapalı: DASHBOARD_TOKEN ayarlanmamış")
    if not hmac.compare_digest(x_dashboard_token, settings.dashboard_token):
        raise HTTPException(401, "Geçersiz erişim anahtarı")


@router.get("/health")
def health(session: Session = Depends(get_session), settings: Settings = Depends(get_settings)) -> dict:
    try:
        session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001 – sağlık kontrolü hatayı raporlar, fırlatmaz
        db_ok = False
    state = session.get(SystemState, 1)
    return {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "mode": settings.mode.value,
        "exchange": settings.exchange,
        "bybit_demo": settings.bybit_demo,
        "trading_enabled": bool(state and state.trading_enabled),
        "safe_mode": bool(state and state.safe_mode),
    }


@router.get("/config")
def config(settings: Settings = Depends(get_settings)) -> dict:
    return settings.public_summary()


@router.get("/bots")
def list_bots(session: Session = Depends(get_session)) -> list[dict]:
    states = {s.bot_id: s for s in session.scalars(select(BotState))}
    out = []
    for info in BOTS:
        st = states.get(info.bot_id)
        out.append({
            "bot_id": info.bot_id,
            "name": info.name,
            "role": info.role,
            "kind": info.kind,
            "implemented": info.bot_id in IMPLEMENTATIONS or info.bot_id in IMPLEMENTED_LAYERS,
            "planned_stage": info.stage,
            "enabled": st.enabled if st else False,
            "weight": str(st.weight) if st else "0",
            "status": st.status if st else "kayıt yok",
            "signals_total": st.signals_total if st else 0,
            "signals_accepted": st.signals_accepted if st else 0,
            "last_analysis_at": st.last_analysis_at.isoformat() if st and st.last_analysis_at else None,
            "last_error": st.last_error if st else None,
        })
    return out


class BotUpdate(BaseModel):
    enabled: bool | None = None
    weight: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("2"))


@router.patch("/bots/{bot_id}", dependencies=[Depends(require_admin)])
def update_bot(bot_id: str, body: BotUpdate, session: Session = Depends(get_session)) -> dict:
    info = BOTS_BY_ID.get(bot_id)
    if info is None:
        raise HTTPException(404, "Böyle bir bot yok")
    if info.kind == "guard" and body.enabled is False:
        raise HTTPException(409, f"{info.name} zorunlu güvenlik katmanıdır, kapatılamaz")
    st = session.get(BotState, bot_id)
    before = {"enabled": st.enabled, "weight": str(st.weight)}
    if body.enabled is not None:
        st.enabled = body.enabled
    if body.weight is not None:
        st.weight = body.weight
    session.add(AuditLog(event="bot_guncellendi", actor="panel",
                         detail={"bot_id": bot_id, "before": before,
                                 "after": {"enabled": st.enabled, "weight": str(st.weight)}}))
    return {"bot_id": bot_id, "enabled": st.enabled, "weight": str(st.weight)}


class KillSwitch(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


@router.post("/system/kill-switch", dependencies=[Depends(require_admin)])
def kill_switch(body: KillSwitch, session: Session = Depends(get_session)) -> dict:
    """Acil durdurma: yeni işlemler kapanır, sistem güvenli moda geçer.
    Mevcut koruyucu stop emirlerine dokunulmaz (şartname bölüm 20)."""
    st = session.get(SystemState, 1)
    st.trading_enabled = False
    st.safe_mode = True
    st.safe_mode_reason = body.reason
    session.add(AuditLog(event="acil_durdurma", actor="panel", detail={"reason": body.reason}))
    return {"trading_enabled": False, "safe_mode": True, "reason": body.reason}


class Resume(BaseModel):
    confirm: bool


@router.post("/system/resume", dependencies=[Depends(require_admin)])
def resume(body: Resume, session: Session = Depends(get_session)) -> dict:
    if not body.confirm:
        raise HTTPException(400, "Yeniden başlatma için confirm=true gerekir")
    st = session.get(SystemState, 1)
    st.trading_enabled = True
    st.safe_mode = False
    st.safe_mode_reason = None
    session.add(AuditLog(event="yeniden_baslatildi", actor="panel"))
    return {"trading_enabled": True, "safe_mode": False}


@router.get("/audit")
def audit(limit: int = 50, session: Session = Depends(get_session)) -> list[dict]:
    rows = session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 200)))
    return [{"event": r.event, "actor": r.actor, "detail": r.detail, "at": r.created_at.isoformat()} for r in rows]


class ClosePosition(BaseModel):
    confirm: bool
    reason: str = Field(default="elle kapatma", max_length=200)


@router.post("/positions/{position_id}/close", dependencies=[Depends(require_admin)])
async def close_position(position_id: int, body: ClosePosition, request: Request) -> dict:
    """Manuel pozisyon kapatma (şartname 21.6). Onay ve erişim anahtarı ister."""
    if not body.confirm:
        raise HTTPException(400, "Kapatma için confirm=true gerekir")
    ex = getattr(request.app.state, "execution", None)
    if ex is None:
        raise HTTPException(503, "Emir katmanı devre dışı")
    return await ex.close_position(position_id, body.reason)
