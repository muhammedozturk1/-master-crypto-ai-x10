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

import time

from app.bots.bot09_execution import ExecutionService, audit, safe_mode
from app.database.models import Position, ProtectionChange, ScanSnapshot
from app.engines import profit_lock as PL
from app.indicators import core as I
from app.database.session import session_scope
from app.exchange.bybit_public import BybitError
from app.execution.brokers import SimBroker

log = logging.getLogger("x10.monitor")
ZERO = Decimal(0)


MISSING_BEFORE_ESTIMATE = 8   # ~2 dk: borsanın kapanış kaydını yazması için tanınan süre


class PositionMonitor:
    def __init__(self, execution: ExecutionService, every_sec: int = 15):
        self.x = execution
        self.b = execution.b
        self.every = every_sec
        self.last_checked: dict[int, int] = {}
        self.missing: dict[int, int] = {}
        self._atr: dict[str, tuple[float, Decimal | None]] = {}
        self._ticks: dict[str, Decimal] = {}
        self._steps: dict[str, Decimal] = {}

    def _open(self) -> list[dict]:
        with session_scope() as s:
            rows = s.scalars(select(Position).where(Position.status != "KAPALI", Position.venue == self.x.venue))
            return [{"id": p.id, "symbol": p.symbol, "long": p.side == "LONG", "qty": p.qty, "entry": p.entry_price,
                     "stop": p.current_stop, "tp": p.take_profit, "entry_fee": p.entry_fee,
                     "opened_ms": int(p.opened_at.timestamp() * 1000), "status": p.status,
                     # kısmi kapatmadan sonraki kapanış kayıtları: kısmi emir ikinci kez sayılmasın
                     "since_ms": int((p.partial_at or p.opened_at).timestamp() * 1000) + (1000 if p.partial_at else 0)}
                    for p in rows]

    def _mark(self, pid: int, price: Decimal) -> None:
        with session_scope() as s:
            p = s.get(Position, pid)
            if p is None or p.status == "KAPALI":
                return
            d = 1 if p.side == "LONG" else -1
            est_exit_fee = p.qty * price * self.x.fee_pct / 100
            p.last_price = price
            p.unrealized_net_pnl = ((price - p.entry_price) * p.qty * d - p.entry_fee - est_exit_fee
                                    + (p.partial_realized or Decimal(0)))   # kısmi kâr dahil toplam

    def _load_ticks(self) -> None:
        with session_scope() as s:
            snap = s.scalars(select(ScanSnapshot).where(ScanSnapshot.ok.is_(True)).order_by(ScanSnapshot.id.desc()).limit(1)).first()
            for c in (snap.result.get("candidates", []) if snap else []):
                if c.get("tick_size") not in (None, "", "0"):
                    self._ticks[c["symbol"]] = Decimal(c["tick_size"])
                if c.get("qty_step") not in (None, "", "0"):
                    self._steps[c["symbol"]] = Decimal(c["qty_step"])

    async def atr(self, symbol: str) -> Decimal | None:
        """15 dakikalık ATR(14), 5 dakika önbellekli."""
        hit = self._atr.get(symbol)
        if hit and time.monotonic() - hit[0] < 300:
            return hit[1]
        cs = await self.x.b.pub.klines(symbol, "15", 40) if hasattr(self.x.b, "pub") else []
        H, L, C = [float(c.high) for c in cs], [float(c.low) for c in cs], [float(c.close) for c in cs]
        a = I.atr(H, L, C, 14)
        val = Decimal(str(a[-2])) if len(a) > 1 and a[-2] else None  # son kapanmış mum
        self._atr[symbol] = (time.monotonic(), val)
        return val

    async def protect(self, pid: int, price: Decimal) -> str | None:
        """Kâr koruma motoru: zirve, kilit ve stop güncellemesi (şartname 14)."""
        with session_scope() as s:
            p = s.get(Position, pid)
            if p is None or p.status != "ACIK":
                return None
            view = PL.PositionView(p.side == "LONG", p.qty, p.entry_price, p.entry_fee, p.initial_stop,
                                   p.current_stop, p.peak_net_pnl_pct or Decimal(0), p.high_water or p.entry_price,
                                   p.lock_state or 0, p.strategy_id, self.x.fee_pct, self._ticks.get(p.symbol))
            symbol, tp = p.symbol, p.take_profit
            long, entry, init_stop, partial_done = p.side == "LONG", p.entry_price, p.initial_stop, p.partial_taken
        cfg = self.x.s
        if cfg.partial_tp_enabled and not partial_done:
            r_dist = abs(entry - init_stop) * Decimal(str(cfg.partial_tp_r))
            trigger = entry + r_dist if long else entry - r_dist
            if (long and price >= trigger) or (not long and price <= trigger):
                res = await self.x.take_partial(pid, Decimal(str(cfg.partial_tp_fraction)), self._steps.get(symbol))
                if res.get("status") == "DOLDU":
                    return "moved"
                with session_scope() as s:   # kısmi kapatma sonrası güncel görünüm
                    p = s.get(Position, pid)
                    view = PL.PositionView(p.side == "LONG", p.qty, p.entry_price, p.entry_fee, p.initial_stop,
                                           p.current_stop, p.peak_net_pnl_pct or Decimal(0), p.high_water or p.entry_price,
                                           p.lock_state or 0, p.strategy_id, self.x.fee_pct, self._ticks.get(p.symbol))
        try:
            atr = await self.atr(symbol)
        except Exception:  # noqa: BLE001 – ATR yoksa trailing bileşeni devre dışı kalır
            atr = None
        d = PL.evaluate(view, price, atr)
        with session_scope() as s:
            p = s.get(Position, pid)
            p.peak_net_pnl_pct, p.high_water = d.peak_net_pct, d.high_water
            p.lock_state = d.state
        if d.exit_now:
            audit("kar_kilidi_cikis", {"position_id": pid, "reason": d.reason, "components": d.components})
            await self.x.close_position(pid, "kâr kilidi: " + d.reason)
            return "exit"
        if d.new_stop is None:
            return None
        if not isinstance(self.b, SimBroker):
            try:  # Bybit trading-stop mevcut stopu tek işlemde değiştirir: korumasız an olmaz
                await self.b.set_protection(symbol, d.new_stop, tp)
            except Exception as exc:  # noqa: BLE001
                with session_scope() as s:
                    s.get(Position, pid).lock_state = PL.ERROR
                audit("kar_kilidi_hatasi", {"position_id": pid, "error": str(exc)[:300], "target_stop": str(d.new_stop)})
                return "error"
        with session_scope() as s:
            p = s.get(Position, pid)
            old = p.current_stop
            p.current_stop = d.new_stop
            s.add(ProtectionChange(position_id=pid, old_stop=old, new_stop=d.new_stop, lock_state=d.state,
                                   reason=(d.reason + f"; bileşenler {d.components}")[:2000]))
        log.info("Stop güncellendi #%s %s: %s → %s (%s)", pid, symbol, view.current_stop, d.new_stop, PL.STATE_NAMES[d.state])
        return "moved"

    async def cycle(self) -> dict:
        rep = {"checked": 0, "closed": 0, "errors": 0}
        self._load_ticks()
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
            if p["status"] == "ACIK":
                r = await self.protect(p["id"], candles[-1].close)
                return r == "exit"
        return False

    async def _exchange(self, p: dict) -> bool:
        ep = await self.b.position(p["symbol"])
        if ep is not None:
            self.missing.pop(p["id"], None)
            self._mark(p["id"], ep.mark_price)
            if ep.stop_loss is None:  # borsa tarafındaki koruma kaybolmuş
                await self.b.set_protection(p["symbol"], p["stop"], p["tp"])
                audit("stop_yeniden_kuruldu", {"position_id": p["id"], "symbol": p["symbol"]})
            if p["status"] == "ACIK":
                await self.protect(p["id"], ep.mark_price)
            return False
        # Borsada pozisyon yok: stop/hedef ile kapanmış
        cr = await self.b.closed_result(p["symbol"], p["long"], p["entry"], p["qty"], p["since_ms"])
        if cr is None:
            n = self.missing[p["id"]] = self.missing.get(p["id"], 0) + 1
            if n < MISSING_BEFORE_ESTIMATE:
                return False
            # Pozisyon borsada kesin olarak yok (sorgu hatası değil: boş yanıt) → açık risk yok.
            # Kapanış kaydı da emir geçmişi de gelmedi: çıkış fiyatı borsadaki son stoptan (fiyat
            # hedefi geçtiyse hedeften) TAHMİN edilir ve açıkça işaretlenir. Güvenli moda gerek yok.
            try:
                px = await self.b.last_price(p["symbol"])
            except Exception:  # noqa: BLE001
                px = p["stop"]
            beyond_tp = (px >= p["tp"]) if p["long"] else (px <= p["tp"])
            est = p["tp"] if beyond_tp else p["stop"]
            fee = p["qty"] * est * self.x.fee_pct / 100
            self.x.finalize(p["id"], est, fee, "borsada kapandı (çıkış fiyatı tahmini: kayıt alınamadı)")
            audit("kapanis_tahmini", {"position_id": p["id"], "symbol": p["symbol"], "tahmini_cikis": str(est),
                                      "son_fiyat": str(px), "neden": "closed-pnl ve emir geçmişi boş döndü"})
            log.warning("#%s %s borsada kapalı, kayıt yok: çıkış %s olarak tahmin edildi", p["id"], p["symbol"], est)
            self.missing.pop(p["id"], None)
            return True
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
        # Limit emri doldu ama pozisyon kaydı birkaç saniye sonra yazılacak: sahipsiz sayılmaz
        orphan = ex - db_symbols - self.x.pending_symbols()
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
