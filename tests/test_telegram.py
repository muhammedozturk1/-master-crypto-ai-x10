import asyncio
from decimal import Decimal as D

import httpx

from app.database import session as dbs
from app.database.models import AuditLog, Decision, Position, ProtectionChange
from app.notifications.telegram import TelegramNotifier, format_event, install


def test_format_event():
    t = format_event("pozisyon_acildi", {"symbol": "BTCUSDT", "side": "LONG", "qty": "0.001", "price": "60000", "venue": "BYBIT_DEMO"})
    assert "İşleme girildi: BTC ALIŞ (yükseliş)" in t and "Giriş fiyatı: 60.000" in t and "Miktar: 0,001" in t
    c = format_event("pozisyon_kapandi", {"symbol": "AKEUSDT", "reason": "take-profit", "exit": "0.00103", "net_pnl": "5.87"})
    assert "İşlem kapandı: AKE" in c and "🎯 Hedefe ulaştı" in c and "Çıkış fiyatı: 0,00103" in c and "🟢 KÂR: +5,87 $" in c
    l = format_event("pozisyon_kapandi", {"symbol": "X", "reason": "stop-loss", "exit": "1", "net_pnl": "-2.01"})
    assert "🛑 Zarar durdur çalıştı" in l and "🔴 ZARAR: -2,01 $" in l
    k = format_event("limit_emir_kondu", {"symbol": "AKEUSDT", "side": "SHORT", "price": "0.00109", "wait_sec": 300})
    assert "Emir kondu: AKE SATIŞ (düşüş)" in k and "0,00109" in k and "5 dk" in k
    assert format_event("bot_guncellendi", {}) is None   # her olay bildirilmez
    for msg in (t, c, l, k):
        assert "SHORT" not in msg and "LONG" not in msg and "BYBIT_DEMO" not in msg


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
    assert "Kâr koruması: X" in sent[1]["text"] and "98 → 100,2" in sent[1]["text"] and "başabaş" in sent[1]["text"]


def test_send_failure_is_isolated():
    n = TelegramNotifier("tok", "1", httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert asyncio.run(n.send("x")) is False and n.failed == 1
