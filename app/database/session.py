"""Veritabanı bağlantısı ve ilk kurulum."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.bots.registry import BOTS
from app.database.models import AuditLog, Base, BotState, SystemState

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def configure(url: str) -> Engine:
    global _engine, _SessionLocal
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        db_path = url.split("///", 1)[-1]
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        kwargs = {"connect_args": {"check_same_thread": False}}
    _engine = create_engine(url, **kwargs)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Veritabanı yapılandırılmadı")
    return _engine


def _migrate(engine: Engine) -> None:
    """Mevcut tablolara sonradan eklenen sütunlar (create_all eski tabloları değiştirmez)."""
    insp = inspect(engine)
    if "signals" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("signals")}
        with engine.begin() as conn:
            if "dedup_key" not in cols:
                conn.execute(text("ALTER TABLE signals ADD COLUMN dedup_key VARCHAR(160)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_signals_dedup ON signals (dedup_key)"))


def init_db() -> None:
    """Tabloları oluşturur, 10 botu ve sistem durumunu ilk kez kaydeder."""
    Base.metadata.create_all(get_engine())
    _migrate(get_engine())
    with session_scope() as s:
        existing = set(s.scalars(select(BotState.bot_id)))
        for bot in BOTS:
            if bot.bot_id not in existing:
                s.add(BotState(bot_id=bot.bot_id))
        if s.get(SystemState, 1) is None:
            s.add(SystemState(id=1))
            s.add(AuditLog(event="sistem_kuruldu", detail={"bots": len(BOTS)}))


def session_scope() -> "_Scope":
    return _Scope()


class _Scope:
    def __enter__(self) -> Session:
        if _SessionLocal is None:
            raise RuntimeError("Veritabanı yapılandırılmadı")
        self.session = _SessionLocal()
        return self.session

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.session.commit()
        else:
            self.session.rollback()
        self.session.close()


def get_session() -> Iterator[Session]:
    """FastAPI bağımlılığı."""
    with session_scope() as s:
        yield s
