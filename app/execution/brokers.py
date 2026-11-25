"""Emir yürütücüleri: Bybit (demo veya canlı) ve gerçek fiyatlı simülasyon.

İkisi aynı arayüzü sunar; BOT09 hangisiyle çalıştığını bilmek zorunda değildir. Her pozisyon
hangi ortamda açıldığını `venue` alanında taşır: SIM, BYBIT_DEMO, BYBIT.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.exchange.bybit_private import BybitPrivate
from app.exchange.bybit_public import BybitPublic

ZERO = Decimal(0)


@dataclass
class Fill:
    status: str                     # FILLED / OPEN / PARTIAL / PARTIAL_DONE / REJECTED / UNKNOWN / NOT_FOUND
    # OPEN: borsada bekliyor, dolum yok · PARTIAL: kısmen doldu, hâlâ açık
    # PARTIAL_DONE: kısmen doldu ve kapandı (kalanı iptal) · REJECTED: dolum olmadan kapandı
    filled_qty: Decimal = ZERO
    avg_price: Decimal = ZERO
    fee: Decimal = ZERO
    exchange_order_id: str | None = None
    error: str | None = None


@dataclass
class ExPosition:
    symbol: str
    long: bool
    size: Decimal
    avg_price: Decimal
    mark_price: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None


@dataclass
class ClosedResult:
    exit_price: Decimal
    gross_pnl: Decimal
    open_fee: Decimal | None
    close_fee: Decimal | None
    exchange_closed_pnl: Decimal | None


def _d(x) -> Decimal | None:
    return Decimal(str(x)) if x not in (None, "", "0", 0) else None


def _fee(order: dict, fallback: Decimal) -> Decimal:
    if order.get("cumExecFee") not in (None, ""):
        return Decimal(order["cumExecFee"])
    detail = order.get("cumFeeDetail")
    if isinstance(detail, dict) and detail:
        return sum((Decimal(str(v)) for v in detail.values()), ZERO)
    return fallback


class BybitBroker:
    def __init__(self, private: BybitPrivate, public: BybitPublic, venue: str, taker_fee_pct: Decimal):
        self.p, self.pub, self.venue, self.fee_pct = private, public, venue, taker_fee_pct

    async def prepare(self, symbol: str, leverage: int) -> None:
        await self.p.set_leverage(symbol, leverage)

    async def place_market(self, symbol: str, long: bool, qty: Decimal, client_id: str,
                           stop: Decimal | None, tp: Decimal | None, reduce_only: bool = False) -> Fill:
        side = "Buy" if long else "Sell"
        res = await self.p.create_order(symbol, side, qty, client_id, stop, tp, reduce_only)
        return Fill("SENT", exchange_order_id=res.get("orderId"))

    async def place_limit(self, symbol: str, long: bool, qty: Decimal, price: Decimal, client_id: str,
                          stop: Decimal | None, tp: Decimal | None) -> Fill:
        res = await self.p.create_order(symbol, "Buy" if long else "Sell", qty, client_id, stop, tp, limit_price=price)
        return Fill("SENT", exchange_order_id=res.get("orderId"))

    async def cancel(self, symbol: str, client_id: str) -> None:
        await self.p.cancel_order(symbol, client_id)

    async def best_quote(self, symbol: str) -> tuple[Decimal, Decimal]:
        ob = await self.pub.orderbook(symbol, 1)
        if not ob.bids or not ob.asks:
            raise RuntimeError(f"{symbol}: emir defteri boş")
        return ob.bids[0][0], ob.asks[0][0]

    async def order_status(self, client_id: str) -> Fill:
        o = await self.p.get_order(client_id)
        if o is None:
            return Fill("NOT_FOUND")
        filled = Decimal(o.get("cumExecQty") or "0")
        avg = Decimal(o.get("avgPrice") or "0") if o.get("avgPrice") else ZERO
        st = o.get("orderStatus", "")
        closed = st in ("Rejected", "Cancelled", "Deactivated", "PartiallyFilledCanceled")
        if st == "Filled":
            status = "FILLED"
        elif filled > 0:
            status = "PARTIAL_DONE" if closed else "PARTIAL"
        elif closed:
            status = "REJECTED"
        elif st in ("New", "Untriggered", "Created"):
            status = "OPEN"
        else:
            status = "UNKNOWN"
        return Fill(status, filled, avg, _fee(o, filled * avg * self.fee_pct / 100), o.get("orderId"),
                    o.get("rejectReason") if status == "REJECTED" else None)

    async def position(self, symbol: str) -> ExPosition | None:
        lst = await self.p.positions(symbol)
        if not lst:
            return None
        x = lst[0]
        return ExPosition(symbol, x.get("side") == "Buy", Decimal(x["size"]), Decimal(x.get("avgPrice") or "0"),
                          Decimal(x.get("markPrice") or "0"), _d(x.get("stopLoss")), _d(x.get("takeProfit")))

    async def all_symbols(self) -> set[str]:
        return {x["symbol"] for x in await self.p.positions()}

    async def set_protection(self, symbol: str, stop: Decimal | None, tp: Decimal | None) -> None:
        await self.p.trading_stop(symbol, stop, tp)

    async def closed_result(self, symbol: str, long: bool, entry: Decimal, qty: Decimal, since_ms: int) -> ClosedResult | None:
        rows = await self.p.closed_pnl(symbol, since_ms)
        if not rows:
            return None
        size = sum((Decimal(r.get("closedSize") or "0") for r in rows), ZERO) or qty
        exit_value = sum((Decimal(r.get("cumExitValue") or "0") for r in rows), ZERO)
        exit_price = exit_value / size if size else Decimal(rows[0].get("avgExitPrice") or "0")
        gross = (exit_price - entry) * size * (1 if long else -1)
        of = [r.get("openFee") for r in rows if r.get("openFee") not in (None, "")]
        cf = [r.get("closeFee") for r in rows if r.get("closeFee") not in (None, "")]
        return ClosedResult(exit_price, gross,
                            sum((Decimal(x) for x in of), ZERO) if of else None,
                            sum((Decimal(x) for x in cf), ZERO) if cf else None,
                            sum((Decimal(r.get("closedPnl") or "0") for r in rows), ZERO))

    async def last_price(self, symbol: str) -> Decimal:
        pos = await self.position(symbol)
        if pos and pos.mark_price:
            return pos.mark_price
        cs = await self.pub.klines(symbol, "1", 2)
        return cs[-1].close


class SimBroker:
    """Gerçek Bybit fiyatlarıyla simülasyon. Piyasa emri anlık en iyi alış/satış fiyatından ve
    taker komisyonuyla gerçekleşir. Stop ve hedef, izleyicinin çektiği 1 dakikalık mumların
    en yüksek/en düşük değerleriyle tetiklenir; boşluklu fiyatta stop fiyatından değil mumun
    açılışından kapatılır (muhafazakâr varsayım)."""

    venue = "SIM"

    def __init__(self, public: BybitPublic, taker_fee_pct: Decimal, maker_fee_pct: Decimal = Decimal("0.02")):
        self.pub, self.fee_pct, self.maker_pct = public, taker_fee_pct, maker_fee_pct
        self._orders: dict[str, Fill] = {}
        self._limits: dict[str, dict] = {}

    async def prepare(self, symbol: str, leverage: int) -> None:
        return None

    async def _quote(self, symbol: str) -> tuple[Decimal, Decimal]:
        ob = await self.pub.orderbook(symbol, 1)
        if not ob.bids or not ob.asks:
            raise RuntimeError(f"{symbol}: fiyat yok")
        return ob.bids[0][0], ob.asks[0][0]

    async def place_market(self, symbol, long, qty, client_id, stop, tp, reduce_only=False) -> Fill:
        bid, ask = await self._quote(symbol)
        price = ask if long else bid
        f = Fill("FILLED", qty, price, qty * price * self.fee_pct / 100, f"sim-{client_id}")
        self._orders[client_id] = f
        return f

    async def best_quote(self, symbol: str) -> tuple[Decimal, Decimal]:
        return await self._quote(symbol)

    async def place_limit(self, symbol, long, qty, price, client_id, stop, tp) -> Fill:
        """Limit emir: yerleştirildikten SONRAKİ 1 dk mumlarında fiyat limite değerse limitten,
        maker komisyonuyla dolar (yerleştirme dakikası sayılmaz: muhafazakâr)."""
        import time
        now = int(time.time() * 1000)
        self._limits[client_id] = {"symbol": symbol, "long": long, "qty": qty, "price": price,
                                   "after_ms": now - now % 60_000 + 60_000}
        self._orders[client_id] = Fill("OPEN")
        return Fill("SENT", exchange_order_id=f"sim-{client_id}")

    async def cancel(self, symbol: str, client_id: str) -> None:
        await self.order_status(client_id)          # son bir dolum kontrolü
        if self._orders.get(client_id, Fill("NOT_FOUND")).status == "OPEN":
            self._orders[client_id] = Fill("REJECTED", error="iptal edildi")
        self._limits.pop(client_id, None)

    async def order_status(self, client_id: str) -> Fill:
        lim = self._limits.get(client_id)
        if lim and self._orders[client_id].status == "OPEN":
            cs = await self.pub.klines(lim["symbol"], "1", 30)
            px = lim["price"]
            if any(c.start_ms >= lim["after_ms"] and ((c.low <= px) if lim["long"] else (c.high >= px)) for c in cs):
                self._orders[client_id] = Fill("FILLED", lim["qty"], px, lim["qty"] * px * self.maker_pct / 100,
                                               f"sim-{client_id}")
                self._limits.pop(client_id, None)
        return self._orders.get(client_id, Fill("NOT_FOUND"))

    async def last_price(self, symbol: str) -> Decimal:
        cs = await self.pub.klines(symbol, "1", 2)
        return cs[-1].close

    async def recent_range(self, symbol: str, since_ms: int):
        """since_ms'den bu yana 1 dk mumları (açılış, yüksek, düşük, kapanış)."""
        cs = await self.pub.klines(symbol, "1", 30)
        return [c for c in cs if c.start_ms + 60_000 > since_ms]
