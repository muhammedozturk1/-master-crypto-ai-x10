"""Sinyal ve NR7 tarayıcı uçları."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

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
    fp = getattr(runner, "footprint", None)
    return {"enabled": True, "last_runs": {tf: asdict(r) for tf, r in runner.reports.items()},
            "footprint": fp.status() if fp else {"enabled": False}}


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


@router.get("/portfolio")
def portfolio(session: Session = Depends(get_session)) -> dict:
    """BOT08 kasa durumu: tek ortak kasa, bileşik, gerçekleşmemiş K/Z dahil özkaynak."""
    from app.bots.bot08_portfolio import account_state
    from app.config import get_settings
    s = get_settings()
    st = account_state(session, s.risk.starting_capital_usd, s.mode.value)
    return {"mode": s.mode.value, **st.to_dict(),
            "note": "Gerçekleşmemiş K/Z garanti edilmiş kâr değildir. Para yatırma/çekme yok (paper kasa)."}


@router.get("/risk")
def risk_status(session: Session = Depends(get_session)) -> dict:
    """BOT07 durumu: limitler, kullanım ve şu an yeni işlemi engelleyen nedenler."""
    from app.bots.bot07_risk import RiskManager
    from app.bots.bot08_portfolio import account_state
    from app.config import HARD_CAPS, get_settings
    from app.database.models import SystemState
    s = get_settings()
    lim = s.risk
    acc = account_state(session, lim.starting_capital_usd, s.mode.value)
    blocks, halt = RiskManager(lim).account_blocks(acc)
    sysst = session.get(SystemState, 1)
    day_limit = acc.day_start_equity * lim.daily_loss_pct / 100
    week_limit = acc.week_start_equity * lim.weekly_loss_pct / 100
    return {
        "limits": {k: str(v) for k, v in lim.model_dump().items()},
        "hard_caps": {k: str(v) for k, v in HARD_CAPS.items()},
        "usage": {
            "risk_budget_usd": str((acc.equity * lim.risk_per_trade_pct / 100).quantize(acc.equity.__class__("0.0001"))),
            "daily_pnl": str(acc.day_pnl.quantize(acc.equity.__class__("0.01"))), "daily_limit": str(day_limit.quantize(acc.equity.__class__("0.01"))),
            "weekly_pnl": str(acc.week_pnl.quantize(acc.equity.__class__("0.01"))), "weekly_limit": str(week_limit.quantize(acc.equity.__class__("0.01"))),
            "drawdown_pct": str(acc.drawdown_pct.quantize(acc.equity.__class__("0.01"))),
            "open_positions": acc.open_positions, "open_risk_usd": str(acc.open_risk),
            "margin_in_use": str(acc.margin_in_use), "consecutive_losses": acc.consecutive_losses,
        },
        "new_trades_blocked_by": blocks,
        "system": {"trading_enabled": sysst.trading_enabled if sysst else None,
                   "safe_mode": sysst.safe_mode if sysst else None,
                   "safe_mode_reason": sysst.safe_mode_reason if sysst else None},
    }


def _note(session: Session, p) -> str | None:
    """Kayıtlı işlem notu; eski pozisyonlarda (not alanı eklenmeden önce) kayıtlı karardan üretilir."""
    if p.entry_note:
        return p.entry_note
    try:
        from app.notifications.trade_note import build_note
        return build_note(session, p.decision_id)
    except Exception:  # noqa: BLE001
        return None


@router.get("/positions")
def positions(status: str | None = None, limit: int = Query(50, ge=1, le=500),
              session: Session = Depends(get_session)) -> list[dict]:
    """Açık pozisyon ekranı (şartname 21.4) ve kapanmış pozisyonlar."""
    from app.database.models import Position
    q = select(Position).order_by(Position.id.desc()).limit(limit)
    if status:
        q = q.where(Position.status == status.upper())
    out = []
    for p in session.scalars(q):
        out.append({
            "id": p.id, "decision_id": p.decision_id, "venue": p.venue, "mode": p.mode, "symbol": p.symbol,
            "side": p.side, "strategy": p.strategy_id, "qty": str(p.qty), "leverage": p.leverage,
            "notional_usd": str((p.qty * p.entry_price).quantize(Decimal("0.01"))),
            "entry_price": str(p.entry_price), "last_price": str(p.last_price) if p.last_price is not None else None,
            "initial_stop": str(p.initial_stop), "current_stop": str(p.current_stop),
            "take_profit": str(p.take_profit) if p.take_profit is not None else None,
            "unrealized_net_pnl": str(p.unrealized_net_pnl) if p.unrealized_net_pnl is not None else None,
            "realized_net_pnl": str(p.realized_net_pnl) if p.realized_net_pnl is not None else None,
            "fees": str(p.entry_fee + p.exit_fee), "risk_usd": str(p.risk_usd) if p.risk_usd is not None else None,
            "lock_state": p.lock_state, "peak_net_pnl_pct": str(p.peak_net_pnl_pct or 0), "status": p.status,
            "partial_taken": bool(p.partial_taken),
            "partial_realized": str(p.partial_realized) if p.partial_realized else None,
            "note": _note(session, p),
            "opened_at": p.opened_at.isoformat(), "closed_at": p.closed_at.isoformat() if p.closed_at else None,
            "duration_min": int(((p.closed_at or datetime.now(timezone.utc)) - (p.opened_at if p.opened_at.tzinfo else p.opened_at.replace(tzinfo=timezone.utc))).total_seconds() // 60),
        })
    return out


@router.get("/positions/history")
def position_history(limit: int = Query(100, ge=1, le=1000), session: Session = Depends(get_session)) -> dict:
    """İşlem geçmişi: kapanmış pozisyonlar, çıkış fiyatı ve nedeni, toplamlar."""
    from app.database.models import AuditLog, Position
    rows = list(session.scalars(select(Position).where(Position.status == "KAPALI")
                                .order_by(Position.closed_at.desc()).limit(limit)))
    ids = {p.id for p in rows}
    reasons: dict[int, str] = {}
    for a in session.scalars(select(AuditLog).where(AuditLog.event == "pozisyon_kapandi")):
        pid = (a.detail or {}).get("position_id")
        if pid in ids:
            reasons[pid] = (a.detail or {}).get("reason", "")
    trades = []
    for p in rows:
        reason = reasons.get(p.id, "")
        trades.append({
            "id": p.id, "symbol": p.symbol, "side": p.side, "strategy": (p.strategy_id or "").split("+")[0],
            "qty": str(p.qty), "entry_price": str(p.entry_price),
            "exit_price": str(p.last_price) if p.last_price is not None else None,
            "exit_estimated": "tahmini" in reason, "reason": reason,
            "partial_taken": bool(p.partial_taken),
            "partial_realized": str(p.partial_realized) if p.partial_realized else None,
            "net_pnl": str(p.realized_net_pnl) if p.realized_net_pnl is not None else None,
            "fees": str((p.entry_fee or 0) + (p.exit_fee or 0)), "venue": p.venue, "note": _note(session, p),
            "opened_at": p.opened_at.isoformat(), "closed_at": p.closed_at.isoformat() if p.closed_at else None,
        })
    nets = [Decimal(t["net_pnl"]) for t in trades if t["net_pnl"] is not None]
    wins = [n for n in nets if n > 0]
    return {"count": len(nets), "wins": len(wins), "losses": len(nets) - len(wins),
            "total_net": str(sum(nets, Decimal(0))), "trades": trades}


@router.get("/orders")
def orders(limit: int = Query(50, ge=1, le=500), session: Session = Depends(get_session)) -> list[dict]:
    from app.database.models import Order
    return [{"id": o.id, "client_order_id": o.client_order_id, "exchange_order_id": o.exchange_order_id,
             "position_id": o.position_id, "symbol": o.symbol, "side": o.side, "purpose": o.purpose,
             "qty": str(o.qty), "filled_qty": str(o.filled_qty),
             "avg_fill_price": str(o.avg_fill_price) if o.avg_fill_price is not None else None,
             "order_type": o.order_type, "price": str(o.price) if o.price is not None else None,
             "status": o.status, "created_at": o.created_at.isoformat()}
            for o in session.scalars(select(Order).order_by(Order.id.desc()).limit(limit))]


@router.get("/positions/{position_id}/protection")
def protection_history(position_id: int, session: Session = Depends(get_session)) -> dict:
    """Kâr kilidi ve stop değişikliklerinin kaydı (şartname 14.9)."""
    from app.database.models import Position, ProtectionChange
    from app.engines.profit_lock import STATE_NAMES
    p = session.get(Position, position_id)
    if p is None:
        return {"error": "pozisyon yok"}
    rows = session.scalars(select(ProtectionChange).where(ProtectionChange.position_id == position_id)
                           .order_by(ProtectionChange.id))
    return {
        "position_id": p.id, "symbol": p.symbol, "side": p.side, "state": STATE_NAMES.get(p.lock_state, p.lock_state),
        "peak_net_pnl_pct": str(p.peak_net_pnl_pct), "initial_stop": str(p.initial_stop), "current_stop": str(p.current_stop),
        "changes": [{"at": r.created_at.isoformat(), "old_stop": str(r.old_stop), "new_stop": str(r.new_stop),
                     "state": STATE_NAMES.get(r.lock_state, r.lock_state), "reason": r.reason} for r in rows],
        "note": "Gerçekleşmemiş kâr garanti değildir; kayma ve fiyat boşlukları korunan tutarı değiştirebilir.",
    }


@router.get("/portfolio/history")
def portfolio_history(hours: int = Query(24, ge=1, le=720), session: Session = Depends(get_session)) -> list[dict]:
    """Özkaynak eğrisi (BOT08 5 dakikalık anlık görüntüleri)."""
    from app.database.models import EquitySnapshot
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = session.scalars(select(EquitySnapshot).where(EquitySnapshot.created_at >= since).order_by(EquitySnapshot.id))
    return [{"t": r.created_at.isoformat(), "equity": str(r.equity), "peak": str(r.peak_equity)} for r in rows]
