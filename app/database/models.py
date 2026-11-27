"""Veritabanı tabloları. Para ve fiyat alanları Numeric (ondalık) tutulur."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

PRICE = Numeric(28, 10)
MONEY = Numeric(20, 8)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class BotState(Base):
    """Her botun açık/kapalı durumu, ana robotun verdiği ağırlık ve sayaçlar."""

    __tablename__ = "bot_state"
    bot_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    weight: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("1"))
    status: Mapped[str] = mapped_column(String(32), default="beklemede")
    signals_total: Mapped[int] = mapped_column(Integer, default=0)
    signals_accepted: Mapped[int] = mapped_column(Integer, default=0)
    last_analysis_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SignalRecord(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("uq_signals_dedup", "dedup_key", unique=True),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedup_key: Mapped[str | None] = mapped_column(String(160))
    bot_id: Mapped[str] = mapped_column(ForeignKey("bot_state.bot_id"), index=True)
    strategy_id: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    action: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"))


class Decision(Base):
    """Ana robotun her kararı: açılan, reddedilen ve vetolanan fırsatlar."""

    __tablename__ = "decisions"
    __table_args__ = (Index("uq_decisions_key", "decision_key", unique=True),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_key: Mapped[str | None] = mapped_column(String(160))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str | None] = mapped_column(String(8))
    direction: Mapped[str | None] = mapped_column(String(5))
    plan: Mapped[dict | None] = mapped_column(JSON)
    # YUKSEK_ONCELIK / RISK_KONTROLU / DOGRULAMA / IZLEME / REDDEDILDI / CELISKI / VETO / SISTEM_DURDU
    outcome: Mapped[str] = mapped_column(String(16))
    score: Mapped[int] = mapped_column(Integer)
    strategy_id: Mapped[str | None] = mapped_column(String(64))
    market_regime: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    contributions: Mapped[dict] = mapped_column(JSON, default=dict)
    model_version: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id"))
    mode: Mapped[str] = mapped_column(String(20))  # PAPER / AUTONOMOUS_LIVE ...
    venue: Mapped[str | None] = mapped_column(String(12))  # SIM / BYBIT_DEMO / BYBIT
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(5))  # LONG / SHORT
    strategy_id: Mapped[str] = mapped_column(String(64))
    qty: Mapped[Decimal] = mapped_column(PRICE)
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    entry_price: Mapped[Decimal] = mapped_column(PRICE)
    initial_stop: Mapped[Decimal] = mapped_column(PRICE)
    current_stop: Mapped[Decimal] = mapped_column(PRICE)
    take_profit: Mapped[Decimal | None] = mapped_column(PRICE)
    peak_net_pnl_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("0"))
    lock_state: Mapped[int] = mapped_column(Integer, default=0)  # kâr kilidi durum makinesi 0-6
    entry_fee: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    exit_fee: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    realized_net_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    # Pozisyon izleyicinin güncellediği anlık değerler ve açılıştaki risk/teminat
    last_price: Mapped[Decimal | None] = mapped_column(PRICE)
    high_water: Mapped[Decimal | None] = mapped_column(PRICE)  # long: en yüksek, short: en düşük fiyat
    # Kısmi kâr alma: yarısı 1R'de kapatıldıysa; o kapatmanın net sonucu (giriş komisyonu hariç)
    partial_taken: Mapped[bool | None] = mapped_column(Boolean, default=False)
    partial_realized: Mapped[Decimal | None] = mapped_column(MONEY, default=Decimal("0"))
    partial_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entry_note: Mapped[str | None] = mapped_column(Text)   # işlem notu: neden alındı
    unrealized_net_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    risk_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    margin_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(String(12), default="ACIK", index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("client_order_id", name="uq_client_order_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64))  # çift emir koruması
    exchange_order_id: Mapped[str | None] = mapped_column(String(64))
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(20))
    purpose: Mapped[str] = mapped_column(String(16))  # GIRIS / STOP / HEDEF / CIKIS
    qty: Mapped[Decimal] = mapped_column(PRICE)
    filled_qty: Mapped[Decimal] = mapped_column(PRICE, default=Decimal("0"))
    price: Mapped[Decimal | None] = mapped_column(PRICE)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(PRICE)
    status: Mapped[str] = mapped_column(String(16), default="OLUSTURULDU")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProtectionChange(Base):
    """Stop ve kâr kilidi değişikliklerinin kaydı (şartname 14.9)."""

    __tablename__ = "protection_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), index=True)
    old_stop: Mapped[Decimal] = mapped_column(PRICE)
    new_stop: Mapped[Decimal] = mapped_column(PRICE)
    lock_state: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equity: Mapped[Decimal] = mapped_column(MONEY)
    available: Mapped[Decimal] = mapped_column(MONEY)
    in_positions: Mapped[Decimal] = mapped_column(MONEY)
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY)
    peak_equity: Mapped[Decimal] = mapped_column(MONEY)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SystemState(Base):
    """Tek satırlık sistem durumu: çalışıyor, güvenli mod, acil durdurma."""

    __tablename__ = "system_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    safe_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    safe_mode_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ScanSnapshot(Base):
    """Coin tarayıcının her turunun sonucu: seçilenler ve elenme nedenleri."""

    __tablename__ = "scan_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    selected: Mapped[int] = mapped_column(Integer, default=0)
    total_symbols: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(12), default="CALISIYOR")  # CALISIYOR / BITTI / HATA
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Ayar değişiklikleri, acil durdurmalar ve sistem olayları."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event: Mapped[str] = mapped_column(String(48), index=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(32), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
