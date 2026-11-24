"""Sinyal ve NR7 tarayıcı uçları."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Decision, SignalRecord
from app.database.session import get_session

router = APIRouter()


def _row(r: SignalRecord) -> dict:
    p = r.payload
    return {
        "id": r.id, "created_at": r.created_at.isoformat(), "bot_id": r.bot_id, "strategy": r.strategy_id,
        "symbol": r.symbol, "timeframe": r.timeframe, "action": r.action, "confidence": r.confidence,
        "entry": [p.get("entry_low"), p.get("entry_high")], "stop_loss": p.get("stop_loss"),
        "take_profit": p.get("take_profit"), "expected_net_rr": p.get("expected_net_rr"),
        "reason": p.get("reason"), "risk_warnings": p.get("risk_warnings", []),
    }


def _decision_brief(session: Session, decision_id: int | None) -> dict | None:
    if decision_id is None:
        return None
    d = session.get(Decision, decision_id)
    return {"outcome": d.outcome, "score": d.score, "direction": d.direction} if d else None


@router.get("/signals")
def signals(bot_id: str | None = None, action: str | None = None, include_wait: bool = False,
            limit: int = Query(50, ge=1, le=500), session: Session = Depends(get_session)) -> list[dict]:
    q = select(SignalRecord).order_by(SignalRecord.id.desc()).limit(limit)
    if bot_id:
        q = q.where(SignalRecord.bot_id == bot_id.upper())
    if action:
        q = q.where(SignalRecord.action == action.upper())
    elif not include_wait:
        q = q.where(SignalRecord.action != "BEKLE")
    return [_row(r) for r in session.scalars(q)]


@router.get("/nr7")
def nr7_scanner(hours: int = Query(6, ge=1, le=72), session: Session = Depends(get_session)) -> list[dict]:
    """NR7 tarayıcı ekranı (şartname 21.5): son saatlerde NR7 görülen pariteler."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = session.scalars(select(SignalRecord).where(SignalRecord.bot_id == "BOT10",
                                                      SignalRecord.created_at >= since)
                           .order_by(SignalRecord.id.desc()))
    out, seen = [], set()
    for r in rows:
        d = r.payload.get("details", {})
        key = (r.symbol, r.timeframe, d.get("nr7_start"))
        if key in seen:
            continue  # aynı NR7 için en güncel durumu göster
        seen.add(key)
        out.append({
            "symbol": r.symbol, "timeframe": r.timeframe, "status": d.get("status"),
            "nr7_high": d.get("nr7_high"), "nr7_low": d.get("nr7_low"), "nr7_range": d.get("nr7_range"),
            "volume_ratio": d.get("volume_ratio"), "trend": d.get("trend"), "htf_trend": d.get("htf_trend"),
            "breakout": d.get("breakout"), "inside_bar": d.get("inside_bar"), "squeeze": d.get("squeeze"),
            "action": r.action, "confidence": r.confidence, "seen_at": r.created_at.isoformat(),
            "main_robot_decision": _decision_brief(session, r.decision_id),
        })
    return out


@router.get("/runner")
def runner_status(request: Request) -> dict:
    runner = getattr(request.app.state, "runner", None)
    if runner is None:
        return {"enabled": False}
    return {"enabled": True, "last_runs": {tf: asdict(r) for tf, r in runner.reports.items()}}


@router.get("/calendar")
def calendar(request: Request) -> dict:
    """Ekonomik takvim vetosu ve Fear & Greed (BOT05 kaynağı)."""
    from app.config import get_settings
    from app.engines.calendar_service import MISSING_SOURCES, fng_sentiment, veto_status
    svc = getattr(request.app.state, "calendar", None)
    if svc is None:
        return {"enabled": False}
    st, s = svc.state, get_settings()
    now = datetime.now(timezone.utc)
    upcoming = [e.to_dict() for e in st.events if e.time >= now - timedelta(hours=1) and e.impact == "High"][:15]
    return {
        "enabled": True, "fetched_at": st.fetched_at.isoformat() if st.fetched_at else None,
        **veto_status(st.events, now, s.news_blackout_before_min, s.news_blackout_after_min),
        "fear_greed": st.fear_greed, "fear_greed_label": st.fear_greed_label,
        "sentiment": fng_sentiment(st.fear_greed), "high_impact_events": upcoming,
        "errors": st.errors, "not_connected_sources": MISSING_SOURCES,
    }


@router.get("/arbitrage")
def arbitrage(request: Request) -> dict:
    """Arbitraj gözlem raporu (BOT04 kaynağı). Gözlem modu: işlem yapılmaz."""
    svc = getattr(request.app.state, "arbitrage", None)
    if svc is None:
        return {"enabled": False}
    st = svc.state
    return {
        "enabled": True, "mode": "gozlem", "checked_at": st.checked_at.isoformat() if st.checked_at else None,
        "best": st.best(), "triangular": st.triangular, "cross_exchange": st.cross, "errors": st.errors,
        "note": "Net değerler komisyon sonrasıdır; borsalar arası transfer ücreti ve süresi dahil değildir.",
    }


def _decision_row(d: Decision) -> dict:
    return {
        "id": d.id, "created_at": d.created_at.isoformat(), "symbol": d.symbol, "timeframe": d.timeframe,
        "direction": d.direction, "outcome": d.outcome, "score": d.score, "strategy": d.strategy_id,
        "regime": d.market_regime, "reason": d.reason, "plan": d.plan, "contributions": d.contributions,
        "model_version": d.model_version,
    }


@router.get("/decisions")
def decisions(outcome: str | None = None, symbol: str | None = None, limit: int = Query(50, ge=1, le=500),
              session: Session = Depends(get_session)) -> list[dict]:
    """Ana robot kararları; reddedilen fırsatlar ve nedenleri dahil (şartname 21.2)."""
    q = select(Decision).order_by(Decision.id.desc()).limit(limit)
    if outcome:
        q = q.where(Decision.outcome == outcome.upper())
    if symbol:
        q = q.where(Decision.symbol == symbol.upper())
    return [_decision_row(d) for d in session.scalars(q)]


@router.get("/master")
def master_status(request: Request, session: Session = Depends(get_session)) -> dict:
    """Ana robot kontrol merkezi özeti: son karar, son 24 saatin karar dağılımı, piyasa girdileri."""
    from sqlalchemy import func
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    counts = dict(session.execute(select(Decision.outcome, func.count()).where(Decision.created_at >= since)
                                  .group_by(Decision.outcome)).all())
    last = session.scalars(select(Decision).order_by(Decision.id.desc()).limit(1)).first()
    runner = getattr(request.app.state, "runner", None)
    mi = runner.market_inputs() if runner else None
    return {
        "model_version": "kural-v1",
        "last_decision": _decision_row(last) if last else None,
        "decisions_24h": counts,
        "inputs": {"news_veto": mi.news_veto, "veto_reason": mi.veto_reason, "sentiment": mi.sentiment,
                   "trading_enabled": mi.trading_enabled, "safe_mode": mi.safe_mode,
                   "bot_weights": mi.bot_weights} if mi else None,
        "execution": "Emir gönderimi yok: risk katmanı (Aşama 5) ve emir katmanı (Aşama 6) henüz devrede değil.",
    }
