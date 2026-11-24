"""BOT09 – Emir ve pozisyon yönetimi (şartname bölüm 12). Uygulama katmanıdır: strateji
veya işlem kararı vermez, yalnız ana robotun onaylanmış (BOT07 risk onayı almış) kararını
uygular ve açık pozisyonları izler.

Emir açma süreci (12.2): 1 karar → 2 risk (BOT07, önceden) → 3 sistem/bakiye → 4 minimum
miktar (BOT07) → 5 güncel fiyat, kayma ve ödül/risk yeniden kontrolü → 6 benzersiz emir
kimliği, gönderimden ÖNCE kaydedilir → 7 piyasa emri, stop-loss ve hedef emirle birlikte
borsa tarafına eklenir → 8 gerçekleşme doğrulanır → 9 gerçek giriş fiyatı kaydedilir →
10 stop borsada doğrulanır; kurulamazsa pozisyon hemen kapatılır ve sistem güvenli moda
geçer → 11 kâr koruma motoru (Aşama 7) → 12 pozisyon izlemeye alınır.

Emir güvenliği (12.3): aynı emir iki kez gönderilmez (benzersiz kimlik + veritabanı kısıtı);
zaman aşımında körlemesine tekrar yok, önce borsadaki gerçek durum sorgulanır; kısmi
gerçekleşmede pozisyon gerçekleşen miktarla açılır (stop tüm pozisyonu kapsar); borsa ile
veritabanı uyuşmazlığında yeni işlemler durur.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import Settings, TradingMode
from app.database.models import AuditLog, Order, Position, SystemState
from app.database.session import session_scope
from app.exchange.bybit_private import BybitTimeout
from app.exchange.bybit_public import BybitError
from app.execution.brokers import Fill, SimBroker

log = logging.getLogger("x10.execution")
ZERO = Decimal(0)
EXECUTING_MODES = {TradingMode.PAPER, TradingMode.AUTONOMOUS_LIVE}


def _round_down(x: Decimal, step: Decimal | None) -> Decimal:
    if not step or step <= 0:
        return x
    return (x / step).to_integral_value(rounding=ROUND_DOWN) * step


def safe_mode(reason: str, detail: dict | None = None) -> None:
    with session_scope() as s:
        st = s.get(SystemState, 1)
        st.trading_enabled = False
        st.safe_mode = True
        st.safe_mode_reason = reason[:500]
        s.add(AuditLog(event="guvenli_mod", actor="BOT09", detail={"reason": reason, **(detail or {})}))
    log.error("GÜVENLİ MOD: %s", reason)


def audit(event: str, detail: dict) -> None:
    with session_scope() as s:
        s.add(AuditLog(event=event, actor="BOT09", detail=detail))


class ExecutionService:
    def __init__(self, broker, settings: Settings, poll_attempts: int = 6, poll_delay: float = 0.5):
        self.b = broker
        self.s = settings
        self.venue = broker.venue
        self.poll_attempts, self.poll_delay = poll_attempts, poll_delay
        self.fee_pct = Decimal(str(settings.taker_fee_pct))

    # ------------------------------------------------------------------
    async def open_from_decision(self, decision_id: int, symbol: str, direction: str, plan: dict,
                                 qty_step: Decimal | None = None) -> dict:
        if self.s.mode not in EXECUTING_MODES:
            return {"status": "ATLANDI", "reason": f"{self.s.mode.value} modunda emir gönderilmez"}
        long = direction == "LONG"
        with session_scope() as s:
            st = s.get(SystemState, 1)
            if not st.trading_enabled or st.safe_mode:
                return {"status": "ATLANDI", "reason": "yeni işlemler durdurulmuş / güvenli mod"}
            open_rows = list(s.scalars(select(Position).where(Position.status != "KAPALI")))
            if len(open_rows) >= self.s.risk.max_open_positions or any(p.symbol == symbol for p in open_rows):
                return {"status": "ATLANDI", "reason": "pozisyon sınırı veya aynı paritede açık pozisyon"}

        qty = Decimal(plan["qty"])
        stop, tp = Decimal(plan["stop_loss"]), Decimal(plan["take_profit"])
        entry_hi, entry_lo = Decimal(plan["entry_high"]), Decimal(plan["entry_low"])
        risk_usd = Decimal(plan.get("risk_usd") or "0")
        cost_pct = Decimal(plan.get("estimated_cost_pct") or "0.12")

        # 5) Güncel fiyat, kayma ve ödül/risk yeniden kontrolü
        try:
            price = await self.b.last_price(symbol)
        except Exception as exc:  # noqa: BLE001
            return {"status": "ATLANDI", "reason": f"güncel fiyat alınamadı: {exc}"}
        plan_entry = entry_hi if long else entry_lo
        slip_limit = abs(plan_entry - stop) * Decimal("0.25")
        if (long and price > plan_entry + slip_limit) or (not long and price < plan_entry - slip_limit):
            return {"status": "ATLANDI", "reason": f"fiyat kaçtı: plan {plan_entry}, şimdi {price}"}
        if (long and price <= stop) or (not long and price >= stop):
            return {"status": "ATLANDI", "reason": "fiyat stop seviyesine ulaşmış"}
        cost = price * cost_pct / 100
        rr = (abs(tp - price) - cost) / (abs(price - stop) + cost)
        if rr < self.s.risk.min_reward_risk:
            return {"status": "ATLANDI", "reason": f"güncel fiyatla net ödül/risk {rr:.2f} < {self.s.risk.min_reward_risk}"}
        unit_risk = abs(price - stop) + cost
        if risk_usd > 0 and qty * unit_risk > risk_usd * Decimal("1.05"):
            qty = _round_down(risk_usd / unit_risk, qty_step)
        if qty <= 0:
            return {"status": "ATLANDI", "reason": "güncel fiyatla miktar sıfıra düştü"}

        # 6) Benzersiz emir kimliği, gönderimden önce kayıt
        client_id = f"x10-{decision_id}-E"
        try:
            with session_scope() as s:
                s.add(Order(client_order_id=client_id, symbol=symbol, side="Buy" if long else "Sell",
                            order_type="Market", purpose="GIRIS", qty=qty, status="GONDERILIYOR"))
        except IntegrityError:
            return {"status": "ATLANDI", "reason": "bu karar için emir zaten gönderilmiş"}

        # 7) Gönder
        try:
            await self.b.prepare(symbol, self.s.risk.leverage)
            sent = await self.b.place_market(symbol, long, qty, client_id, stop, tp)
        except BybitTimeout as exc:
            sent = Fill("UNKNOWN", error=str(exc))
            audit("emir_zaman_asimi", {"client_id": client_id, "error": str(exc)})
        except BybitError as exc:
            self._update_order(client_id, status="REDDEDILDI")
            audit("emir_reddedildi", {"client_id": client_id, "error": str(exc)})
            return {"status": "REDDEDILDI", "reason": str(exc)}

        # 8) Gerçekleşmeyi doğrula (körlemesine tekrar YOK: durum sorgulanır)
        fill = sent if sent.status == "FILLED" else await self._poll(client_id)
        if fill.status in ("UNKNOWN", "NOT_FOUND") and not isinstance(self.b, SimBroker):
            ep = await self.b.position(symbol)
            if ep is not None:
                fill = Fill("FILLED", ep.size, ep.avg_price, ep.size * ep.avg_price * self.fee_pct / 100)
        if fill.status in ("UNKNOWN", "NOT_FOUND"):
            self._update_order(client_id, status="BILINMIYOR")
            safe_mode(f"emir durumu doğrulanamadı: {client_id}", {"symbol": symbol})
            return {"status": "BILINMIYOR", "reason": "emir durumu doğrulanamadı; güvenli mod"}
        if fill.filled_qty <= 0:
            self._update_order(client_id, status="REDDEDILDI")
            return {"status": "REDDEDILDI", "reason": fill.error or "emir gerçekleşmedi"}

        # 9) Gerçek giriş fiyatıyla pozisyonu kaydet
        status = "DOLDU" if fill.status == "FILLED" else "KISMI"
        with session_scope() as s:
            o = s.scalars(select(Order).where(Order.client_order_id == client_id)).one()
            pos = Position(decision_id=decision_id, mode=self.s.mode.value, venue=self.venue, symbol=symbol,
                           side="LONG" if long else "SHORT", strategy_id=(plan.get("strategy") or "")[:64],
                           qty=fill.filled_qty, leverage=self.s.risk.leverage, entry_price=fill.avg_price,
                           initial_stop=stop, current_stop=stop, take_profit=tp, entry_fee=fill.fee,
                           last_price=fill.avg_price, high_water=fill.avg_price, unrealized_net_pnl=-fill.fee,
                           risk_usd=fill.filled_qty * (abs(fill.avg_price - stop) + cost),
                           margin_usd=fill.filled_qty * fill.avg_price / self.s.risk.leverage)
            s.add(pos)
            s.flush()
            o.position_id, o.status, o.filled_qty = pos.id, status, fill.filled_qty
            o.avg_fill_price, o.exchange_order_id = fill.avg_price, fill.exchange_order_id
            pos_id = pos.id

        # 10) Stopun borsada olduğunu doğrula
        if not isinstance(self.b, SimBroker):
            ok = await self._verify_stop(symbol, stop, tp)
            if not ok:
                await self.close_position(pos_id, "koruyucu stop kurulamadı")
                safe_mode(f"{symbol}: koruyucu stop kurulamadı, pozisyon kapatıldı")
                return {"status": "KAPATILDI", "reason": "stop kurulamadı", "position_id": pos_id}
        audit("pozisyon_acildi", {"position_id": pos_id, "symbol": symbol, "side": "LONG" if long else "SHORT",
                                  "qty": str(fill.filled_qty), "price": str(fill.avg_price), "venue": self.venue})
        log.info("Pozisyon açıldı #%s %s %s %s @ %s (%s)", pos_id, symbol, "LONG" if long else "SHORT",
                 fill.filled_qty, fill.avg_price, self.venue)
        return {"status": status, "position_id": pos_id, "qty": str(fill.filled_qty), "price": str(fill.avg_price)}

    async def _poll(self, client_id: str) -> Fill:
        f = Fill("UNKNOWN")
        for _ in range(self.poll_attempts):
            try:
                f = await self.b.order_status(client_id)
            except BybitError as exc:
                f = Fill("UNKNOWN", error=str(exc))
            if f.status in ("FILLED", "REJECTED"):
                return f
            await asyncio.sleep(self.poll_delay)
        return f

    async def _verify_stop(self, symbol: str, stop: Decimal, tp: Decimal) -> bool:
        for attempt in range(3):
            try:
                ep = await self.b.position(symbol)
                if ep is None:
                    return True  # pozisyon yok (anında kapanmış) – izleyici kaydedecek
                if ep.stop_loss is not None:
                    return True
                await self.b.set_protection(symbol, stop, tp)
            except BybitError as exc:
                log.warning("Stop doğrulama denemesi %d başarısız: %s", attempt + 1, exc)
            await asyncio.sleep(self.poll_delay)
        return False

    def _update_order(self, client_id: str, **fields) -> None:
        with session_scope() as s:
            o = s.scalars(select(Order).where(Order.client_order_id == client_id)).first()
            if o:
                for k, v in fields.items():
                    setattr(o, k, v)

    # ------------------------------------------------------------------
    async def close_position(self, position_id: int, reason: str) -> dict:
        """Elle veya acil kapatma: azaltan (reduce-only) piyasa emri."""
        with session_scope() as s:
            p = s.get(Position, position_id)
            if p is None or p.status == "KAPALI":
                return {"status": "YOK"}
            symbol, long, qty, entry, entry_fee = p.symbol, p.side == "LONG", p.qty, p.entry_price, p.entry_fee
            p.status = "KAPANIYOR"
        client_id = f"x10-{position_id}-X"
        try:
            with session_scope() as s:
                s.add(Order(client_order_id=client_id, position_id=position_id, symbol=symbol,
                            side="Sell" if long else "Buy", order_type="Market", purpose="CIKIS", qty=qty,
                            status="GONDERILIYOR"))
        except IntegrityError:
            pass  # önceki kapatma denemesi: durum sorgulanır
        try:
            sent = await self.b.place_market(symbol, not long, qty, client_id, None, None, reduce_only=True)
        except BybitTimeout:
            sent = Fill("UNKNOWN")
        except BybitError as exc:
            audit("kapatma_hatasi", {"position_id": position_id, "error": str(exc)})
            return {"status": "HATA", "reason": str(exc)}
        fill = sent if sent.status == "FILLED" else await self._poll(client_id)
        if isinstance(self.b, SimBroker) and fill.status == "FILLED":
            self.finalize(position_id, fill.avg_price, fill.fee, reason)
        self._update_order(client_id, status="DOLDU" if fill.status == "FILLED" else fill.status,
                           filled_qty=fill.filled_qty, avg_fill_price=fill.avg_price or None)
        audit("pozisyon_kapatma", {"position_id": position_id, "reason": reason, "fill": fill.status})
        return {"status": fill.status, "reason": reason}

    def finalize(self, position_id: int, exit_price: Decimal, exit_fee: Decimal, reason: str,
                 gross: Decimal | None = None, exchange_pnl: Decimal | None = None) -> None:
        with session_scope() as s:
            p = s.get(Position, position_id)
            if p is None or p.status == "KAPALI":
                return
            d = 1 if p.side == "LONG" else -1
            g = gross if gross is not None else (exit_price - p.entry_price) * p.qty * d
            p.exit_fee = exit_fee
            p.realized_net_pnl = g - p.entry_fee - exit_fee
            p.last_price, p.unrealized_net_pnl = exit_price, ZERO
            p.status, p.closed_at = "KAPALI", datetime.now(timezone.utc)
            p.lock_state = 5  # CLOSED
            s.add(AuditLog(event="pozisyon_kapandi", actor="BOT09", detail={
                "position_id": position_id, "symbol": p.symbol, "reason": reason, "exit": str(exit_price),
                "net_pnl": str(p.realized_net_pnl), "exchange_closed_pnl": str(exchange_pnl) if exchange_pnl is not None else None}))
        log.info("Pozisyon kapandı #%s: %s, çıkış %s", position_id, reason, exit_price)
