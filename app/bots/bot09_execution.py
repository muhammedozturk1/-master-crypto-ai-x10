"""BOT09 – Emir ve pozisyon yönetimi (şartname bölüm 12). Uygulama katmanıdır: strateji
veya işlem kararı vermez, yalnız ana robotun onaylanmış (BOT07 risk onayı almış) kararını
uygular ve açık pozisyonları izler.

Emir açma süreci (12.2): 1 karar → 2 risk (BOT07, önceden) → 3 sistem/bakiye → 4 minimum
miktar (BOT07) → 5 güncel fiyat, kayma ve ödül/risk yeniden kontrolü → 6 benzersiz emir
kimliği, gönderimden ÖNCE kaydedilir → 7 limit (PostOnly, plan fiyatı; varsayılan) veya piyasa
emri, stop-loss ve hedef emirle birlikte
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
from decimal import ROUND_DOWN, ROUND_UP, Decimal

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


LIMIT_WAIT_SEC = {"5m": 120, "15m": 300, "1h": 900}   # limit giriş emri en fazla bu kadar bekler
DEFAULT_LIMIT_WAIT_SEC = 300


def _round_tick(x: Decimal, tick: Decimal | None, down: bool) -> Decimal:
    """Fiyatı tick adımına yuvarlar: alışta aşağı, satışta yukarı (hep daha iyi fiyat yönüne)."""
    if not tick or tick <= 0:
        return x
    n = (x / tick).to_integral_value(rounding=ROUND_DOWN if down else ROUND_UP)
    return (n * tick).normalize()


def safe_mode(reason: str, detail: dict | None = None) -> None:
    with session_scope() as s:
        st = s.get(SystemState, 1)
        if st.safe_mode and st.safe_mode_reason == reason[:500]:
            return  # aynı neden zaten kayıtlı: her izleme turunda yeniden yazma
        st.trading_enabled = False
        st.safe_mode = True
        st.safe_mode_reason = reason[:500]
        s.add(AuditLog(event="guvenli_mod", actor="BOT09", detail={"reason": reason, **(detail or {})}))
    log.error("GÜVENLİ MOD: %s", reason)


def audit(event: str, detail: dict) -> None:
    with session_scope() as s:
        s.add(AuditLog(event=event, actor="BOT09", detail=detail))


class ExecutionService:
    def __init__(self, broker, settings: Settings, poll_attempts: int = 6, poll_delay: float = 0.5,
                 limit_poll_sec: float = 5.0):
        self.b = broker
        self.s = settings
        self.venue = broker.venue
        self.poll_attempts, self.poll_delay = poll_attempts, poll_delay
        self.limit_poll_sec = limit_poll_sec
        self.fee_pct = Decimal(str(settings.taker_fee_pct))
        self.maker_pct = Decimal(str(settings.maker_fee_pct))
        self.pending: dict[str, asyncio.Task] = {}      # client_id → bekleyen limit emir izleyicisi
        self.blocked: set[str] = set()   # borsanın işlem izni vermediği pariteler (ör. sözleşme imzası gereken)

    def pending_symbols(self) -> set[str]:
        """Bekleyen (dolmamış) giriş emri olan pariteler; uzlaştırma bunları sahipsiz saymaz."""
        with session_scope() as s:
            return set(s.scalars(select(Order.symbol).where(Order.purpose == "GIRIS", Order.status == "BEKLIYOR")))

    async def wait_pending(self) -> None:
        """Testler ve kapanış için: bekleyen limit emir izleyicilerinin bitmesini bekler."""
        while self.pending:
            await asyncio.gather(*list(self.pending.values()), return_exceptions=True)

    # ------------------------------------------------------------------
    async def open_from_decision(self, decision_id: int, symbol: str, direction: str, plan: dict,
                                 qty_step: Decimal | None = None, tick_size: Decimal | None = None,
                                 timeframe: str | None = None) -> dict:
        if self.s.mode not in EXECUTING_MODES:
            return {"status": "ATLANDI", "reason": f"{self.s.mode.value} modunda emir gönderilmez"}
        long = direction == "LONG"
        if symbol in self.blocked:
            return {"status": "ATLANDI", "reason": "borsa bu paritede işleme izin vermiyor (sözleşme onayı gerekli)"}
        with session_scope() as s:
            st = s.get(SystemState, 1)
            if not st.trading_enabled or st.safe_mode:
                return {"status": "ATLANDI", "reason": "yeni işlemler durdurulmuş / güvenli mod"}
            open_syms = [p.symbol for p in s.scalars(select(Position).where(Position.status != "KAPALI"))]
            waiting = list(s.scalars(select(Order.symbol).where(Order.purpose == "GIRIS", Order.status == "BEKLIYOR")))
            if len(open_syms) + len(waiting) >= self.s.risk.max_open_positions or symbol in open_syms + waiting:
                return {"status": "ATLANDI", "reason": "pozisyon sınırı (bekleyen emirler dahil) veya aynı paritede pozisyon/emir"}

        qty = Decimal(plan["qty"])
        stop, tp = Decimal(plan["stop_loss"]), Decimal(plan["take_profit"])
        entry_hi, entry_lo = Decimal(plan["entry_high"]), Decimal(plan["entry_low"])
        risk_usd = Decimal(plan.get("risk_usd") or "0")
        cost_pct = Decimal(plan.get("estimated_cost_pct") or "0.12")
        plan_entry = entry_hi if long else entry_lo
        limit_mode = self.s.entry_order_type == "limit"

        # 5) Güncel fiyat, kayma ve ödül/risk yeniden kontrolü
        try:
            if limit_mode:
                bid, ask = await self.b.best_quote(symbol)
                price = (bid + ask) / 2
            else:
                price = await self.b.last_price(symbol)
        except Exception as exc:  # noqa: BLE001
            return {"status": "ATLANDI", "reason": f"güncel fiyat alınamadı: {exc}"}
        if (long and price <= stop) or (not long and price >= stop):
            return {"status": "ATLANDI", "reason": "fiyat stop seviyesine ulaşmış"}
        if limit_mode:
            # Plan fiyatına (veya daha iyisine) maker emir: fiyat kaçtıysa emir plan fiyatında bekler,
            # geri gelmezse süre sonunda iptal edilir. Karşı tarafı almaması için en iyi alış/satışı geçmez.
            entry_px = min(plan_entry, bid) if long else max(plan_entry, ask)
            entry_px = _round_tick(entry_px, tick_size, down=long)
        else:
            slip_limit = abs(plan_entry - stop) * Decimal("0.25")
            if (long and price > plan_entry + slip_limit) or (not long and price < plan_entry - slip_limit):
                return {"status": "ATLANDI", "reason": f"fiyat kaçtı: plan {plan_entry}, şimdi {price}"}
            entry_px = price
        cost = entry_px * cost_pct / 100
        rr = (abs(tp - entry_px) - cost) / (abs(entry_px - stop) + cost)
        if rr < self.s.risk.min_reward_risk:
            return {"status": "ATLANDI", "reason": f"güncel fiyatla net ödül/risk {rr:.2f} < {self.s.risk.min_reward_risk}"}
        unit_risk = abs(entry_px - stop) + cost
        if risk_usd > 0 and qty * unit_risk > risk_usd * Decimal("1.05"):
            qty = _round_down(risk_usd / unit_risk, qty_step)
        if qty <= 0:
            return {"status": "ATLANDI", "reason": "güncel fiyatla miktar sıfıra düştü"}

        # 6) Benzersiz emir kimliği, gönderimden önce kayıt
        client_id = f"x10-{decision_id}-E"
        try:
            with session_scope() as s:
                s.add(Order(client_order_id=client_id, symbol=symbol, side="Buy" if long else "Sell",
                            order_type="Limit" if limit_mode else "Market", purpose="GIRIS", qty=qty,
                            price=entry_px if limit_mode else None,
                            status="BEKLIYOR" if limit_mode else "GONDERILIYOR"))
        except IntegrityError:
            return {"status": "ATLANDI", "reason": "bu karar için emir zaten gönderilmiş"}

        # 7) Gönder
        try:
            await self.b.prepare(symbol, self.s.risk.leverage)
            if limit_mode:
                sent = await self.b.place_limit(symbol, long, qty, entry_px, client_id, stop, tp)
            else:
                sent = await self.b.place_market(symbol, long, qty, client_id, stop, tp)
        except BybitTimeout as exc:
            sent = Fill("UNKNOWN", error=str(exc))
            audit("emir_zaman_asimi", {"client_id": client_id, "error": str(exc)})
        except BybitError as exc:
            self._update_order(client_id, status="REDDEDILDI")
            audit("emir_reddedildi", {"client_id": client_id, "error": str(exc)})
            if "110126" in str(exc) or "agreement" in str(exc).lower():
                self.blocked.add(symbol)
                log.warning("%s: Bybit bu kontrat için sözleşme onayı istiyor; parite emir için atlanacak", symbol)
            return {"status": "REDDEDILDI", "reason": str(exc)}

        if limit_mode:
            wait = LIMIT_WAIT_SEC.get(timeframe or "", DEFAULT_LIMIT_WAIT_SEC)
            self._update_order(client_id, exchange_order_id=sent.exchange_order_id)
            task = asyncio.create_task(self._await_limit(decision_id, symbol, long, plan, client_id, stop, tp,
                                                         cost_pct, wait))
            self.pending[client_id] = task
            task.add_done_callback(lambda _t, c=client_id: self.pending.pop(c, None))
            audit("limit_emir_kondu", {"client_id": client_id, "symbol": symbol, "side": "LONG" if long else "SHORT",
                                       "price": str(entry_px), "qty": str(qty), "wait_sec": wait})
            log.info("Limit giriş emri %s %s %s @ %s, en fazla %d sn beklenecek", symbol,
                     "LONG" if long else "SHORT", qty, entry_px, wait)
            return {"status": "BEKLIYOR", "price": str(entry_px), "qty": str(qty), "wait_sec": wait}

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
        return await self._record_position(decision_id, symbol, long, plan, client_id, fill, stop, tp, cost_pct)

    async def _await_limit(self, decision_id: int, symbol: str, long: bool, plan: dict, client_id: str,
                           stop: Decimal, tp: Decimal, cost_pct: Decimal, wait_sec: float) -> dict:
        """Limit emri dolana, reddedilene veya süre dolana dek izler. Süre dolarsa (ya da sistem
        durdurulursa) iptal eder; iptal sonrası son durum sorgulanır, kısmi dolum varsa o kadarı
        pozisyon olarak kaydedilir (stop/hedef emre bağlı olduğundan borsada da korunur)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_sec
        fill = Fill("UNKNOWN")
        try:
            while True:
                try:
                    fill = await self.b.order_status(client_id)
                except Exception as exc:  # noqa: BLE001 – geçici hata: süre içinde tekrar sorgulanır
                    fill = Fill("UNKNOWN", error=str(exc))
                if fill.status in ("FILLED", "REJECTED", "PARTIAL_DONE"):
                    break
                halted = self._halted()
                if loop.time() >= deadline or halted:
                    try:
                        await self.b.cancel(symbol, client_id)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Limit emir iptali başarısız (%s): %s", client_id, exc)
                    fill = await self._poll(client_id)
                    if fill.status in ("OPEN", "UNKNOWN", "NOT_FOUND") and fill.filled_qty <= 0:
                        # iptal sonrası durum doğrulanamadı: borsada emir kalmış olabilir
                        ep = None if isinstance(self.b, SimBroker) else await self.b.position(symbol)
                        if ep is not None:
                            fill = Fill("FILLED", ep.size, ep.avg_price, ep.size * ep.avg_price * self.maker_pct / 100)
                        elif fill.status != "OPEN":
                            fill = Fill("REJECTED", error="iptal edildi")
                        else:
                            self._update_order(client_id, status="BILINMIYOR")
                            safe_mode(f"limit emir iptal edilemedi: {client_id}", {"symbol": symbol})
                            return {"status": "BILINMIYOR"}
                    break
                await asyncio.sleep(self.limit_poll_sec)
        except asyncio.CancelledError:
            raise
        if fill.filled_qty <= 0:
            self._update_order(client_id, status="IPTAL")
            audit("limit_emir_dolmadi", {"client_id": client_id, "symbol": symbol,
                                         "reason": fill.error or "süre doldu, fiyat limit seviyesine gelmedi"})
            log.info("Limit emir dolmadı, iptal: %s %s", symbol, client_id)
            return {"status": "IPTAL"}
        if fill.status == "PARTIAL":
            fill = Fill("PARTIAL_DONE", fill.filled_qty, fill.avg_price, fill.fee, fill.exchange_order_id)
        return await self._record_position(decision_id, symbol, long, plan, client_id, fill, stop, tp, cost_pct)

    def _halted(self) -> bool:
        with session_scope() as s:
            st = s.get(SystemState, 1)
            return bool(st and (not st.trading_enabled or st.safe_mode))

    async def _record_position(self, decision_id: int, symbol: str, long: bool, plan: dict, client_id: str,
                               fill: Fill, stop: Decimal, tp: Decimal, cost_pct: Decimal) -> dict:
        # 9) Gerçek giriş fiyatıyla pozisyonu kaydet
        status = "DOLDU" if fill.status == "FILLED" else "KISMI"
        cost = fill.avg_price * cost_pct / 100
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
            o.avg_fill_price = fill.avg_price
            o.exchange_order_id = fill.exchange_order_id or o.exchange_order_id
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

    async def recover_pending(self) -> list[dict]:
        """Açılışta: yeniden başlatmadan önce konmuş, durumu belirsiz kalan limit giriş emirleri.
        Önce iptal edilir, sonra son durum sorgulanır; dolum varsa pozisyon olarak kaydedilir."""
        from app.database.models import Decision as DecisionRow
        with session_scope() as s:
            rows = [(o.client_order_id, o.symbol, o.side == "Buy") for o in
                    s.scalars(select(Order).where(Order.purpose == "GIRIS", Order.status == "BEKLIYOR"))]
        out = []
        for cid, symbol, long in rows:
            try:
                await self.b.cancel(symbol, cid)
            except Exception as exc:  # noqa: BLE001
                log.warning("Açılışta bekleyen emir iptal edilemedi %s: %s", cid, exc)
            fill = await self._poll(cid)
            if fill.filled_qty > 0:
                did = int(cid.split("-")[1])
                with session_scope() as s:
                    plan = dict((s.get(DecisionRow, did).plan or {}))
                res = await self._record_position(did, symbol, long, plan, cid, fill, Decimal(plan["stop_loss"]),
                                                  Decimal(plan["take_profit"]),
                                                  Decimal(plan.get("estimated_cost_pct") or "0.12"))
            elif fill.status in ("REJECTED", "NOT_FOUND"):
                self._update_order(cid, status="IPTAL")
                res = {"status": "IPTAL"}
            else:
                self._update_order(cid, status="BILINMIYOR")
                safe_mode(f"açılışta bekleyen emir doğrulanamadı: {cid}", {"symbol": symbol})
                res = {"status": "BILINMIYOR"}
            audit("bekleyen_emir_kurtarildi", {"client_id": cid, **{k: str(v) for k, v in res.items()}})
            out.append({"client_id": cid, **res})
        return out

    async def _poll(self, client_id: str) -> Fill:
        f = Fill("UNKNOWN")
        for _ in range(self.poll_attempts):
            try:
                f = await self.b.order_status(client_id)
            except BybitError as exc:
                f = Fill("UNKNOWN", error=str(exc))
            if f.status in ("FILLED", "REJECTED", "PARTIAL_DONE"):
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
