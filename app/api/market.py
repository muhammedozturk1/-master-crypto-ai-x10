"""Piyasa verisi uçları: son tarama sonucu ve kapanmış mumlar."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.candles import TIMEFRAME_TO_INTERVAL, DataQualityError, closed_only, validate_candles
from app.database.models import ScanSnapshot
from app.database.session import get_session
from app.exchange.bybit_public import BybitError, BybitPublic

router = APIRouter(prefix="/market")


def get_market(request: Request) -> BybitPublic:
    return request.app.state.market


@router.get("/scan")
def latest_scan(session: Session = Depends(get_session)) -> dict:
    snap = session.scalars(select(ScanSnapshot).order_by(ScanSnapshot.id.desc()).limit(1)).first()
    if snap is None:
        return {"status": "henuz_tarama_yok"}
    last_ok = snap if snap.ok else session.scalars(
        select(ScanSnapshot).where(ScanSnapshot.ok.is_(True)).order_by(ScanSnapshot.id.desc()).limit(1)).first()
    return {
        "last_run_at": snap.created_at.isoformat(),
        "last_run_ok": snap.ok,
        "last_error": snap.error,
        "data_from": last_ok.created_at.isoformat() if last_ok else None,
        "duration_ms": snap.duration_ms,
        **(last_ok.result if last_ok else {}),
    }


@router.get("/candles/{symbol}")
async def candles(symbol: str, timeframe: str = Query("15m"), limit: int = Query(100, ge=10, le=1000),
                  market: BybitPublic = Depends(get_market)) -> dict:
    interval = TIMEFRAME_TO_INTERVAL.get(timeframe)
    if interval is None:
        raise HTTPException(400, f"Geçersiz zaman dilimi. Seçenekler: {', '.join(TIMEFRAME_TO_INTERVAL)}")
    try:
        raw = await market.klines(symbol.upper(), interval, limit + 1)
    except BybitError as exc:
        raise HTTPException(502, f"Bybit verisi alınamadı: {exc}") from exc
    closed = closed_only(raw)
    quality, problem = "IYI", None
    try:
        validate_candles(closed)
    except DataQualityError as exc:
        quality, problem = "SORUNLU", str(exc)
    return {
        "symbol": symbol.upper(), "timeframe": timeframe,
        "closed_candles": len(closed), "dropped_forming": len(raw) - len(closed),
        "data_quality": quality, "problem": problem,
        "candles": [c.to_dict() for c in closed[-limit:]],
    }


_LIVE_CACHE: dict = {"t": 0.0, "data": None}


@router.get("/live")
async def live_prices(market: BybitPublic = Depends(get_market), session: Session = Depends(get_session)) -> dict:
    """Panel için canlı fiyatlar: BTC, tarayıcının ilk 5 adayı ve açık pozisyonlar. 3 sn önbellek
    (birden çok panel açık olsa da Bybit'e en fazla 3 saniyede bir istek gider)."""
    import time
    from app.database.models import Position
    now = time.monotonic()
    if _LIVE_CACHE["data"] is None or now - _LIVE_CACHE["t"] > 3:
        try:
            ticks = await market.tickers()
        except BybitError as exc:
            raise HTTPException(502, f"Bybit verisi alınamadı: {exc}") from exc
        _LIVE_CACHE.update(t=now, data={t.symbol: t for t in ticks})
    book = _LIVE_CACHE["data"]
    snap = session.scalars(select(ScanSnapshot).where(ScanSnapshot.ok.is_(True)).order_by(ScanSnapshot.id.desc()).limit(1)).first()
    cands = [c["symbol"] for c in (snap.result.get("candidates", []) if snap else [])][:5]
    open_syms = [p.symbol for p in session.scalars(select(Position).where(Position.status != "KAPALI"))]
    wanted = list(dict.fromkeys(["BTCUSDT", *open_syms, *cands]))[:8]
    out = []
    for s in wanted:
        t = book.get(s)
        if t is None:
            continue
        out.append({"symbol": s, "price": str(t.last_price), "bid": str(t.bid), "ask": str(t.ask),
                    "change_24h_pct": str(t.change_24h_pct.quantize(t.change_24h_pct.__class__("0.01"))),
                    "funding": str(t.funding_rate) if t.funding_rate is not None else None,
                    "open_position": s in open_syms})
    return {"at": time.time(), "tickers": out}
