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
    "limit_emir_kondu": "📝 Limit giriş emri kondu (dolması bekleniyor)",
    "limit_emir_dolmadi": "↩️ Limit emir dolmadı, iptal edildi",
    "kismi_kar_alindi": "💰 Kısmi kâr alındı",
    "kapanis_tahmini": "ℹ️ Kapanış fiyatı tahmini (borsa kaydı yok)",
    "anahtar_yetkisi_eksik": "❗ Bybit API anahtarı yetkisi uygun değil",
}


def _num(x, digits=None) -> str:
    from decimal import Decimal, InvalidOperation
    from app.notifications.trade_note import tr_num
    try:
        return tr_num(Decimal(str(x)), digits)
    except (InvalidOperation, ValueError, TypeError):
        return str(x)


def _coin(sym) -> str:
    sym = str(sym or "")
    return sym[:-4] if sym.endswith("USDT") else sym


def _side(s) -> str:
    return {"LONG": "ALIŞ (yükseliş)", "SHORT": "SATIŞ (düşüş)", "Buy": "ALIŞ", "Sell": "SATIŞ"}.get(str(s), str(s))


def _close_reason(r) -> str:
    r = str(r or "").lower()
    if "take" in r or "hedef" in r:
        return "🎯 Hedefe ulaştı"
    if "kilit" in r or "lock" in r:
        return "🔒 Kâr kilidinden çıktı"
    if "tahmini" in r:
        return "Borsada stop/kâr kilidiyle kapandı"
    if "stop" in r:
        return "🛑 Zarar durdur çalıştı"
    if "elle" in r or "panel" in r:
        return "✋ Elle kapatıldı"
    return str(r) or "—"


def format_event(ev: str, d: dict) -> str | None:
    title = TITLES.get(ev)
    if title is None:
        return None
    head = "🤖 MASTER CRYPTO AI X10"
    if ev == "limit_emir_kondu":
        return (f"{head}\n📝 Emir kondu: {_coin(d.get('symbol'))} {_side(d.get('side'))}\n"
                f"Fiyat {_num(d.get('price'))} seviyesine gelirse işleme girilecek "
                f"(en fazla {int(d.get('wait_sec') or 0) // 60} dk beklenir).")
    if ev == "limit_emir_dolmadi":
        return f"{head}\n↩️ {_coin(d.get('symbol'))}: fiyat beklenen seviyeye gelmedi, emir iptal edildi. İşleme girilmedi."
    if ev == "pozisyon_acildi":
        lines = [head, f"📈 İşleme girildi: {_coin(d.get('symbol'))} {_side(d.get('side'))}",
                 f"Giriş fiyatı: {_num(d.get('price'))} · Miktar: {_num(d.get('qty'))}"]
        if d.get("note"):
            lines += ["", str(d["note"])[:3000]]
        return "\n".join(lines)
    if ev == "pozisyon_kapandi":
        net = d.get("net_pnl")
        try:
            netf = float(net)
        except (TypeError, ValueError):
            netf = 0.0
        mark = "🟢 KÂR" if netf > 0 else "🔴 ZARAR" if netf < 0 else "⚪ BAŞABAŞ"
        return (f"{head}\n✅ İşlem kapandı: {_coin(d.get('symbol'))}\n{_close_reason(d.get('reason'))}\n"
                f"Çıkış fiyatı: {_num(d.get('exit'))}\n{mark}: {'+' if netf > 0 else ''}{_num(net, 2)} $")
    if ev == "kismi_kar_alindi":
        return (f"{head}\n💰 Kısmi kâr: {_coin(d.get('symbol'))} pozisyonun yarısı {_num(d.get('price'))} "
                f"fiyatından kapatıldı (+{_num(d.get('net'), 2)} $). Kalan yarı için zarar durdur girişe çekildi.")
    if ev == "gunluk_rapor":
        return None  # günlük rapor daily_report_text ile ayrıca gönderilir
    lines = [head, title]
    keys = {"symbol": "Coin", "side": "Yön", "reason": "Neden", "halt": "Durum", "error": "Hata",
            "problems": "Sorun", "tahmini_cikis": "Tahmini çıkış"}
    for k, label in keys.items():
        if k in d and d[k] not in (None, ""):
            v = d[k]
            if isinstance(v, list):
                v = "; ".join(map(str, v))
            if k == "symbol":
                v = _coin(v)
            if k == "side":
                v = _side(v)
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
        self.last_error: str | None = None

    def status(self) -> dict:
        return {"enabled": True, "sent": self.sent, "failed": self.failed, "last_error": self.last_error}

    def push(self, text: str) -> None:
        self.queue.append(text)

    async def send(self, text: str) -> bool:
        try:
            r = await self.client.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                       json={"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True})
            ok = r.status_code == 200 and r.json().get("ok")
            if not ok:
                try:
                    desc = r.json().get("description", "")
                except ValueError:
                    desc = ""
                self.last_error = f"HTTP {r.status_code}: {desc}"[:200]
                log.warning("Telegram mesajı reddedildi: %s", self.last_error)   # token loglanmaz
        except Exception as exc:  # noqa: BLE001
            self.last_error = exc.__class__.__name__
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
    from sqlalchemy import text as sql
    try:
        sym = conn.execute(sql("select symbol from positions where id = :i"), {"i": target.position_id}).scalar()
    except Exception:  # noqa: BLE001
        sym = None
    state = {1: "zarar riski kalmadı (başabaş)", 2: "kârın bir kısmı kilitlendi", 3: "kâr takip ediliyor",
             4: "çıkış hazırlanıyor"}.get(target.lock_state, "koruma güncellendi")
    _active.push(f"🤖 MASTER CRYPTO AI X10\n🔐 Kâr koruması: {_coin(sym) or '#' + str(target.position_id)}\n"
                 f"Zarar durdur {_num(target.old_stop)} → {_num(target.new_stop)} seviyesine taşındı; {state}.")


def daily_report_text(st, perf_summary: dict) -> str:
    day = st.day_pnl
    return (f"🤖 MASTER CRYPTO AI X10\n📊 Günlük rapor — {datetime.now(timezone.utc).strftime('%d.%m.%Y')}\n"
            f"Kasa: {_num(st.equity, 2)} $ (başlangıç {_num(st.starting_capital, 0)} $)\n"
            f"Bugün: {'🟢 +' if day > 0 else '🔴 ' if day < 0 else ''}{_num(day, 2)} $ · Bu hafta: {_num(st.week_pnl, 2)} $\n"
            f"Zirveden düşüş: %{_num(st.drawdown_pct, 2)} · Açık işlem: {st.open_positions}\n"
            f"Toplam {perf_summary.get('trades', 0)} işlem · net {_num(perf_summary.get('net_profit', 0), 2)} $")
