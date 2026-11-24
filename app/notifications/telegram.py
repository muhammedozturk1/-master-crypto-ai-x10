"""Telegram bildirimleri (şartname bölüm 22). TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID yoksa kapalıdır.

Olaylar denetim kaydından (AuditLog) ve koruma değişikliklerinden (ProtectionChange) toplanır;
böylece bildirim, işlemin kendisini hiçbir zaman yavaşlatmaz veya bozmaz. Gönderim başarısız
olursa işlem etkilenmez, yalnız log düşer. Güncel kasa bildirimde kendi kaydımızdan verilir;
Bybit tarafında doğrulama için /positions ve /portfolio kullanılır.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone

import httpx
from sqlalchemy import event

from app.database.models import AuditLog, ProtectionChange

log = logging.getLogger("x10.telegram")

TITLES = {
    "sistem_basladi": "🟢 Sistem başlatıldı",
    "acil_durdurma": "⛔ Acil durdurma",
    "yeniden_baslatildi": "▶️ Sistem yeniden başlatıldı",
    "guvenli_mod": "🛑 Güvenli mod",
    "risk_durdurma": "⚠️ Risk limiti: yeni işlemler durdu",
    "pozisyon_acildi": "📈 Yeni işlem açıldı",
    "pozisyon_kapandi": "✅ Pozisyon kapandı",
    "kar_kilidi_cikis": "🔒 Kâr kilidi çıkışı",
    "kar_kilidi_hatasi": "❗ Kâr kilidi güncellenemedi",
    "emir_reddedildi": "❗ Emir reddedildi",
    "emir_zaman_asimi": "⏱ Emir zaman aşımı (durum sorgulanıyor)",
    "anahtar_dogrulanamadi": "❗ Bybit API anahtarı doğrulanamadı",
    "scalping_pasif": "⏸ Scalping botu pasife alındı",
    "gunluk_rapor": "📊 Günlük rapor",
}


def format_event(ev: str, d: dict) -> str | None:
    title = TITLES.get(ev)
    if title is None:
        return None
    lines = ["MASTER CRYPTO AI X10", title]
    keys = {"symbol": "Coin", "side": "Yön", "qty": "Miktar", "price": "Giriş", "venue": "Ortam", "reason": "Neden",
            "exit": "Çıkış", "net_pnl": "Net K/Z (USD)", "halt": "Durum", "error": "Hata", "trades": "İşlem",
            "expectancy_usd": "Beklenen (USD)", "equity": "Kasa (USD)", "day_pnl": "Gün K/Z (USD)", "open": "Açık pozisyon"}
    for k, label in keys.items():
        if k in d and d[k] not in (None, ""):
            v = d[k]
            if isinstance(v, list):
                v = "; ".join(map(str, v))
            lines.append(f"{label}: {v}")
    if "reasons" in d:
        lines.append("Neden: " + "; ".join(map(str, d["reasons"]))[:300])
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, client: httpx.AsyncClient | None = None):
        self.token, self.chat_id = token, chat_id
        self.client = client or httpx.AsyncClient(timeout=10.0)
        self.queue: deque[str] = deque(maxlen=200)
        self.sent = 0
        self.failed = 0

    def push(self, text: str) -> None:
        self.queue.append(text)

    async def send(self, text: str) -> bool:
        try:
            r = await self.client.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                       json={"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True})
            ok = r.status_code == 200 and r.json().get("ok")
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram gönderilemedi: %s", exc.__class__.__name__)  # token loglanmaz
            ok = False
        self.sent += int(bool(ok))
        self.failed += int(not ok)
        return bool(ok)

    async def drain(self) -> int:
        n = 0
        while self.queue:
            await self.send(self.queue.popleft())
            n += 1
            await asyncio.sleep(0.05)  # Telegram hız sınırı
        return n

    async def loop(self) -> None:
        while True:
            await self.drain()
            await asyncio.sleep(2)

    async def close(self) -> None:
        await self.client.aclose()


_active: TelegramNotifier | None = None


def install(notifier: TelegramNotifier | None) -> None:
    """Veritabanı olaylarını bildirime bağlar (insert sonrası, işlemi yavaşlatmadan kuyruğa)."""
    global _active
    _active = notifier
    if not event.contains(AuditLog, "after_insert", _on_audit):
        event.listen(AuditLog, "after_insert", _on_audit)
        event.listen(ProtectionChange, "after_insert", _on_protection)


def _on_audit(mapper, conn, target: AuditLog) -> None:
    if _active is None:
        return
    text = format_event(target.event, target.detail or {})
    if text:
        _active.push(text)


def _on_protection(mapper, conn, target: ProtectionChange) -> None:
    if _active is None:
        return
    from app.engines.profit_lock import STATE_NAMES
    _active.push(f"MASTER CRYPTO AI X10\n🔐 Kâr koruma yükseltildi\nPozisyon #{target.position_id}\n"
                 f"Stop: {target.old_stop} → {target.new_stop}\nDurum: {STATE_NAMES.get(target.lock_state, target.lock_state)}")


def daily_report_text(st, perf_summary: dict) -> str:
    return (f"MASTER CRYPTO AI X10\n📊 Günlük rapor ({datetime.now(timezone.utc).date()})\n"
            f"Kasa: {st.equity:.2f} USD (başlangıç {st.starting_capital})\nBugün: {st.day_pnl:+.2f} USD · "
            f"Hafta: {st.week_pnl:+.2f} USD\nDüşüş: %{st.drawdown_pct:.2f} · Açık pozisyon: {st.open_positions}\n"
            f"Toplam işlem: {perf_summary.get('trades', 0)} · Net: {perf_summary.get('net_profit', 0)} USD")
