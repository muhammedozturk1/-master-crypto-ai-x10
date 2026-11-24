"""Paper/canlı işlem performansı ve öğrenme kuralları (şartname 18.3 ve 6.5).

- Kapanan pozisyonlardan strateji, bot ve sembol bazında sonuçlar. Bir işlemi birden fazla bot
  desteklese de kâr yalnız bir kez sayılır (işlem planını veren bota yazılır, 21.3).
- Scalping (BOT03) en az 20 kapanmış işlemde maliyet sonrası beklenen değer negatifse otomatik
  pasife alınır (6.5). Diğer botların ağırlıkları otomatik değişmez; öneri üretilir.
- Yapay zekâ/ana robot risk limitlerini hiçbir koşulda yükseltemez; bu modül limitlere dokunmaz.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtesting.metrics import Trade, breakdown, summarize
from app.database.models import AuditLog, BotState, Decision, Position

log = logging.getLogger("x10.performance")
SCALP_MIN_TRADES = 20


def closed_trades(session: Session, mode: str) -> list[Trade]:
    rows = session.scalars(select(Position).where(Position.status == "KAPALI", Position.mode == mode)
                           .order_by(Position.closed_at))
    out = []
    for p in rows:
        dec = session.get(Decision, p.decision_id) if p.decision_id else None
        bot = (dec.plan or {}).get("bot_id") if dec and dec.plan else None
        risk = float(p.risk_usd or abs(p.entry_price - p.initial_stop) * p.qty)
        out.append(Trade(p.symbol, p.strategy_id or "", bot or "?", p.side == "LONG",
                         int(p.opened_at.timestamp() * 1000), int(p.closed_at.timestamp() * 1000),
                         float(p.entry_price), float(p.last_price or p.entry_price), float(p.qty),
                         float((p.entry_fee or Decimal(0)) + (p.exit_fee or Decimal(0))), float(p.realized_net_pnl or 0), risk,
                         float((p.peak_net_pnl_pct or Decimal(0)) / 100 * p.qty * p.entry_price), "kapandi"))
    return out


def report(session: Session, mode: str, start_capital: float) -> dict:
    tr = closed_trades(session, mode)
    eq, curve = start_capital, [(0, start_capital)]
    for t in tr:
        eq += t.net
        curve.append((t.exit_ms, eq))
    return {"mode": mode, "summary": summarize(tr, curve, start_capital),
            "by_strategy": breakdown(tr, lambda t: t.strategy.split("+")[0]),
            "by_bot": breakdown(tr, lambda t: t.bot_id), "by_symbol": breakdown(tr, lambda t: t.symbol)}


def learning_check(session: Session, mode: str) -> list[str]:
    """Otomatik kurallar; yapılan değişiklikleri döndürür."""
    actions = []
    scalp = [t for t in closed_trades(session, mode) if t.bot_id == "BOT03"]
    if len(scalp) >= SCALP_MIN_TRADES:
        exp = sum(t.net for t in scalp) / len(scalp)
        st = session.get(BotState, "BOT03")
        if exp < 0 and st and st.enabled:
            st.enabled = False
            st.status = "pasif_negatif_beklenen_deger"
            session.add(AuditLog(event="scalping_pasif", actor="ogrenme",
                                 detail={"trades": len(scalp), "expectancy_usd": round(exp, 4)}))
            actions.append(f"BOT03 pasife alındı: {len(scalp)} işlemde beklenen değer {exp:.4f} USD")
            log.warning(actions[-1])
    return actions
