"""BOT08 – Portföy ve sermaye takibi (şartname bölüm 11).

Tek ortak kasa. Botlara ayrı bütçe dağıtılmaz; sermaye yalnız ana robotun onayladığı
pozisyonlara ayrılır. Kapanan işlemin gerçekleşmiş net kârının tamamı kasaya eklenir
(bileşik), ayrı kilitli kâr kasası yoktur. Drawdown geçmiş en yüksek özkaynaktan
hesaplanır ve gerçekleşmemiş zararları da içerir. Para yatırma/çekme bu sürümde yoktur
(paper kasa); eklendiğinde performanstan ayrıştırılacaktır.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import EquitySnapshot, Position

ZERO = Decimal(0)


@dataclass
class AccountState:
    starting_capital: Decimal
    equity: Decimal                 # başlangıç + gerçekleşmiş + gerçekleşmemiş
    realized_net: Decimal
    unrealized_net: Decimal
    fees_paid: Decimal
    margin_in_use: Decimal          # açık pozisyonların teminatı
    available: Decimal              # özkaynak - teminat
    open_risk: Decimal              # açık pozisyonların stopa kadar risk toplamı
    open_positions: int
    open_symbols: list[str]
    peak_equity: Decimal
    drawdown_pct: Decimal
    day_start_equity: Decimal
    week_start_equity: Decimal
    month_start_equity: Decimal
    day_pnl: Decimal
    week_pnl: Decimal
    month_pnl: Decimal
    consecutive_losses: int
    last_loss_at: datetime | None
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: (str(v.quantize(Decimal("0.0001"))) if isinstance(v, Decimal) else
                    v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}


def _period_starts(now: datetime) -> tuple[datetime, datetime, datetime]:
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    month = day.replace(day=1)
    return day, week, month


def account_state(session: Session, starting_capital: Decimal, mode: str = "PAPER",
                  now: datetime | None = None) -> AccountState:
    now = now or datetime.now(timezone.utc)
    rows = list(session.scalars(select(Position).where(Position.mode == mode)))
    closed = sorted((p for p in rows if p.status == "KAPALI" and p.closed_at), key=lambda p: p.closed_at)
    opened = [p for p in rows if p.status != "KAPALI"]

    realized = sum((p.realized_net_pnl or ZERO for p in closed), ZERO)
    unrealized = sum((p.unrealized_net_pnl or ZERO for p in opened), ZERO)
    fees = sum((p.entry_fee + p.exit_fee for p in rows), ZERO)
    margin = sum((p.margin_usd or (p.qty * p.entry_price / max(p.leverage, 1)) for p in opened), ZERO)
    open_risk = sum((p.risk_usd or abs(p.entry_price - p.current_stop) * p.qty for p in opened), ZERO)
    equity = starting_capital + realized + unrealized

    # Dönem başı özkaynak: o ana kadar kapanmış işlemlerin gerçekleşmiş sonucu ile
    day, week, month = _period_starts(now)
    def eq_at(t: datetime) -> Decimal:
        return starting_capital + sum((p.realized_net_pnl or ZERO for p in closed if _aware(p.closed_at) < t), ZERO)
    day_eq, week_eq, month_eq = eq_at(day), eq_at(week), eq_at(month)

    # Zirve özkaynak: başlangıç, her kapanış sonrası değer, kayıtlı anlık görüntüler ve şimdiki değer
    peak = starting_capital
    running = starting_capital
    for p in closed:
        running += p.realized_net_pnl or ZERO
        peak = max(peak, running)
    snap_peak = session.scalars(select(EquitySnapshot.peak_equity).order_by(EquitySnapshot.id.desc()).limit(1)).first()
    if snap_peak is not None:
        peak = max(peak, snap_peak)
    peak = max(peak, equity)
    dd = (peak - equity) / peak * 100 if peak > 0 else ZERO

    streak, last_loss = 0, None
    for p in reversed(closed):
        if (p.realized_net_pnl or ZERO) < 0:
            streak += 1
            last_loss = last_loss or _aware(p.closed_at)
        else:
            break

    return AccountState(
        starting_capital=starting_capital, equity=equity, realized_net=realized, unrealized_net=unrealized,
        fees_paid=fees, margin_in_use=margin, available=equity - margin, open_risk=open_risk,
        open_positions=len(opened), open_symbols=sorted({p.symbol for p in opened}),
        peak_equity=peak, drawdown_pct=dd, day_start_equity=day_eq, week_start_equity=week_eq,
        month_start_equity=month_eq, day_pnl=equity - day_eq, week_pnl=equity - week_eq,
        month_pnl=equity - month_eq, consecutive_losses=streak, last_loss_at=last_loss, as_of=now,
    )


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def record_snapshot(session: Session, st: AccountState) -> EquitySnapshot:
    snap = EquitySnapshot(equity=st.equity, available=st.available, in_positions=st.margin_in_use,
                          unrealized_pnl=st.unrealized_net, peak_equity=st.peak_equity)
    session.add(snap)
    return snap
