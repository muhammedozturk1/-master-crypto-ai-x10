"""Açık pozisyon izleme ve borsa uzlaştırması (BOT09'un izleme tarafı).

Her döngüde (varsayılan 15 sn):
  - Anlık fiyat ve gerçekleşmemiş net K/Z güncellenir (tahmini çıkış komisyonu düşülerek).
  - Bybit: pozisyon borsada kapanmışsa (stop veya hedef) kapanış sonucu borsadan alınır ve
    net K/Z hesaplanır. Stop borsada durduğu için koruma bu döngünün hızına bağlı değildir.
  - Simülasyon: 1 dakikalık mumların en yüksek/en düşük değerleriyle stop/hedef tetiklenir.
    Aynı mumda ikisi de görülürse muhafazakâr varsayımla önce stop sayılır; boşluklu açılışta
    çıkış mumun açılış fiyatından yapılır.
  - Uzlaştırma: borsada olup veritabanında olmayan pozisyon ("sahipsiz") veya ters durum
    görülürse yeni işlemler durur ve sistem güvenli moda geçer.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from sqlalchemy import select

from app.bots.bot09_execution import ExecutionService, audit, safe_mode
from app.database.models import Position
from app.database.session import session_scope
from app.exchange.bybit_public import BybitError
from app.execution.brokers import SimBroker

log = logging.getLogger("x10.monitor")
ZERO = Decimal(0)


class PositionMonitor:
    def __init__(self, execution: ExecutionService, every_sec: int = 15):
        self.x = execution
        self.b = execution.b
        self.every = every_sec
        self.last_checked: dict[int, int] = {}
        self.missing: dict[int, int] = {}

    def _open(self) -> list[dict]:
        with session_scope() as s:
            rows = s.scalars(select(Position).where(Position.status != "KAPALI", Position.venue == self.x.venue))
            return [{"id": p.id, "symbol": p.symbol, "long": p.side == "LONG", "qty": p.qty, "entry": p.entry_price,
                     "stop": p.current_stop, "tp": p.take_profit, "entry_fee": p.entry_fee,
                     "opened_ms": int(p.opened_at.timestamp() * 1000), "status": p.status} for p in rows]

    def _mark(self, pid: int, price: Decimal) -> None:
        with session_scope() as s:
            p = s.get(Position, pid)
            if p is None or p.status == "KAPALI":
                return
            d = 1 if p.side == "LONG" else -1
            est_exit_fee = p.qty * price * self.x.fee_pct / 100
            p.last_price = price
            p.unrealized_net_pnl = (price - p.entry_price) * p.qty * d - p.entry_fee - est_exit_fee

    async def cycle(self) -> dict:
        rep = {"checked": 0, "closed": 0, "errors": 0}
        positions = self._open()
        for p in positions:
            rep["checked"] += 1
            try:
                if isinstance(self.b, SimBroker):
                    closed = await self._sim(p)
                else:
                    closed = await self._exchange(p)
                rep["closed"] += int(closed)
            except Exception as exc:  # noqa: BLE001 – bir pozisyonun hatası diğerlerini durdurmaz
                rep["errors"] += 1
                log.warning("İzleme hatası #%s %s: %s", p["id"], p["symbol"], exc)
        if not isinstance(self.b, SimBroker):
            await self._reconcile({p["symbol"] for p in positions})
        return rep

    async def _sim(self, p: dict) -> bool:
        since = self.last_checked.get(p["id"], p["opened_ms"])
        candles = await self.b.recent_range(p["symbol"], since)
        stop, tp, long = p["stop"], p["tp"], p["long"]
        for c in candles:
            o, h, l = c.open, c.high, c.low
            hit_stop = l <= stop if long else h >= stop
            hit_tp = tp is not None and (h >= tp if long else l <= tp)
            if hit_stop:   # aynı mumda ikisi de varsa önce stop (muhafazakâr)
                px = min(o, stop) if long else max(o, stop)
                reason = "stop-loss"
            elif hit_tp:
                px = max(o, tp) if long else min(o, tp)
                reason = "take-profit"
            else:
                continue
            fee = p["qty"] * px * self.x.fee_pct / 100
            self.x.finalize(p["id"], px, fee, reason)
            self.last_checked.pop(p["id"], None)
            return True
        if candles:
            self.last_checked[p["id"]] = candles[-1].start_ms
            self._mark(p["id"], candles[-1].close)
        return False

    async def _exchange(self, p: dict) -> bool:
        ep = await self.b.position(p["symbol"])
        if ep is not None:
            self.missing.pop(p["id"], None)
            self._mark(p["id"], ep.mark_price)
            if ep.stop_loss is None:  # borsa tarafındaki koruma kaybolmuş
                await self.b.set_protection(p["symbol"], p["stop"], p["tp"])
                audit("stop_yeniden_kuruldu", {"position_id": p["id"], "symbol": p["symbol"]})
            return False
        # Borsada pozisyon yok: stop/hedef ile kapanmış
        cr = await self.b.closed_result(p["symbol"], p["long"], p["entry"], p["qty"], p["opened_ms"])
        if cr is None:
            n = self.missing[p["id"]] = self.missing.get(p["id"], 0) + 1
            if n >= 4:
                safe_mode(f"{p['symbol']} pozisyonu borsada yok ve kapanış kaydı bulunamadı", {"position_id": p["id"]})
            return False
        exit_fee = cr.close_fee if cr.close_fee is not None else p["qty"] * cr.exit_price * self.x.fee_pct / 100
        reason = "stop-loss" if ((cr.exit_price <= p["stop"]) if p["long"] else (cr.exit_price >= p["stop"])) else "borsada kapandı"
        self.x.finalize(p["id"], cr.exit_price, exit_fee, reason, gross=cr.gross_pnl, exchange_pnl=cr.exchange_closed_pnl)
        return True

    async def _reconcile(self, db_symbols: set[str]) -> None:
        try:
            ex = await self.b.all_symbols()
        except BybitError as exc:
            log.warning("Uzlaştırma yapılamadı: %s", exc)
            return
        orphan = ex - db_symbols
        if orphan:
            safe_mode(f"borsada kayıtsız pozisyon: {', '.join(sorted(orphan))}", {"orphan": sorted(orphan)})

    async def loop(self) -> None:
        while True:
            try:
                rep = await self.cycle()
                if rep["checked"]:
                    log.info("İzleme: %s", rep)
            except Exception:  # noqa: BLE001
                log.exception("İzleme döngüsü hatası")
            await asyncio.sleep(self.every)
