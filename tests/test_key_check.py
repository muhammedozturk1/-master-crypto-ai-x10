"""Açılışta API anahtarı yetki denetimi."""

import asyncio

from app.main import check_key_permissions
from tests.fake_bybit_private import FakeBybitPrivate


def run(fake):
    return asyncio.run(check_key_permissions(fake.client()))


def test_key_with_trade_permission_ok(tmp_path):
    from app.database.session import configure, init_db
    configure(f"sqlite:///{tmp_path}/k.db"); init_db()
    assert run(FakeBybitPrivate())["ok"] is True


def test_read_only_or_missing_contract_permission_blocks(tmp_path):
    from sqlalchemy import select
    from app.database.models import AuditLog
    from app.database.session import configure, init_db, session_scope
    configure(f"sqlite:///{tmp_path}/k2.db"); init_db()
    r = run(FakeBybitPrivate(key_read_only=1))
    assert r["ok"] is False and "işlem yetkisi yok" in r["problem"]
    r = run(FakeBybitPrivate(key_permissions={"ContractTrade": ["Position"], "Wallet": []}))
    assert r["ok"] is False
    with session_scope() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.event == "anahtar_yetkisi_eksik")).first() is not None


def test_withdraw_permission_blocks(tmp_path):
    from app.database.session import configure, init_db
    configure(f"sqlite:///{tmp_path}/k3.db"); init_db()
    r = run(FakeBybitPrivate(key_permissions={"ContractTrade": ["Order", "Position"], "Wallet": ["Withdraw"]}))
    assert r["ok"] is False and "PARA ÇEKME" in r["problem"]


def test_unknown_endpoint_does_not_block(tmp_path):
    class NoQuery(FakeBybitPrivate):
        def handler(self, req):  # noqa: D401
            import httpx
            if req.url.path == "/v5/user/query-api":
                return httpx.Response(404)
            return super().handler(req)
    from app.database.session import configure, init_db
    configure(f"sqlite:///{tmp_path}/k4.db"); init_db()
    r = run(NoQuery())
    assert r["ok"] is None
