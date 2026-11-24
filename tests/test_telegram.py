import asyncio
from decimal import Decimal as D

import httpx

from app.database import session as dbs
from app.database.models import AuditLog, Decision, Position, ProtectionChange
from app.notifications.telegram import TelegramNotifier, format_event, install


def test_format_event():
    t = format_event("pozisyon_acildi", {"symbol": "BTCUSDT", "side": "LONG", "qty": "0.001", "price": "60000", "venue": "BYBIT_DEMO"})
    assert "Yeni işlem açıldı" in t and "Coin: BTCUSDT" in t and "Ortam: BYBIT_DEMO" in t
    assert format_event("bot_guncellendi", {}) is None   # her olay bildirilmez


def test_audit_and_protection_events_are_sent(tmp_path):
    sent = []
    def handler(req):
        import json
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})
    n = TelegramNotifier("tok", "123", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    dbs.configure(f"sqlite:///{tmp_path}/t.db")
    dbs.init_db()
    install(n)
    try:
        with dbs.session_scope() as s:
            s.add(AuditLog(event="guvenli_mod", actor="BOT09", detail={"reason": "test"}))
            s.add(Decision(id=1, symbol="X", outcome="ONAYLANDI", score=80, reason="t", contributions={}))
            s.add(Position(id=1, decision_id=1, mode="PAPER", symbol="X", side="LONG", strategy_id="s", qty=D(1),
                           entry_price=D(100), initial_stop=D(98), current_stop=D(98)))
        with dbs.session_scope() as s:
            s.add(ProtectionChange(position_id=1, old_stop=D(98), new_stop=D(100.2), lock_state=1, reason="r"))
        assert asyncio.run(n.drain()) == 2
    finally:
        install(None)
    assert sent[0]["chat_id"] == "123" and "Güvenli mod" in sent[0]["text"]
    assert "98 → 100.2" in sent[1]["text"] and "BREAK_EVEN" in sent[1]["text"]


def test_send_failure_is_isolated():
    n = TelegramNotifier("tok", "1", httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert asyncio.run(n.send("x")) is False and n.failed == 1
