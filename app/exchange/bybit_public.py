"""Bybit v5 herkese açık piyasa verisi istemcisi (API anahtarı gerektirmez).

Demo hesap da gerçek piyasa fiyatlarıyla çalışır; bu yüzden piyasa verisi
her zaman ana Bybit adresinden okunur.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.data.candles import Candle, INTERVAL_MINUTES

log = logging.getLogger("x10.bybit")

MARKET_DATA_URL = "https://api.bybit.com"


class BybitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Ticker:
    symbol: str
    last_price: Decimal
    bid: Decimal
    ask: Decimal
    turnover_24h: Decimal  # USDT cinsinden 24 saatlik işlem hacmi
    change_24h_pct: Decimal
    funding_rate: Decimal | None
    open_interest_value: Decimal | None

    @property
    def spread_pct(self) -> Decimal | None:
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            return None
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100


@dataclass(frozen=True)
class Instrument:
    symbol: str
    status: str
    quote_coin: str
    contract_type: str
    launch_ms: int
    tick_size: Decimal
    qty_step: Decimal
    min_order_qty: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class OrderBook:
    symbol: str
    bids: list[tuple[Decimal, Decimal]]
    asks: list[tuple[Decimal, Decimal]]
    ts_ms: int

    def depth_usd(self, pct: Decimal = Decimal("0.5")) -> tuple[Decimal, Decimal]:
        """Orta fiyatın ±pct% bandındaki alış ve satış derinliği (USDT)."""
        if not self.bids or not self.asks:
            return Decimal(0), Decimal(0)
        mid = (self.bids[0][0] + self.asks[0][0]) / 2
        lo, hi = mid * (1 - pct / 100), mid * (1 + pct / 100)
        bid_usd = sum((p * q for p, q in self.bids if p >= lo), Decimal(0))
        ask_usd = sum((p * q for p, q in self.asks if p <= hi), Decimal(0))
        return bid_usd, ask_usd


def _d(value, default: Decimal | None = Decimal(0)) -> Decimal | None:
    if value in (None, ""):
        return default
    return Decimal(str(value))


class BybitPublic:
    def __init__(self, category: str = "linear", base_url: str = MARKET_DATA_URL,
                 client: httpx.AsyncClient | None = None, retries: int = 2):
        self.category = category
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(base_url=base_url, timeout=10.0)
        self.retries = retries

    async def close(self) -> None:
        if self._own_client:
            await self.client.aclose()

    async def _get(self, path: str, params: dict) -> dict:
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = await self.client.get(path, params=params)
                if r.status_code == 429 or r.status_code >= 500:
                    raise BybitError(f"HTTP {r.status_code}")
                r.raise_for_status()
                body = r.json()
                if body.get("retCode") != 0:
                    raise BybitError(f"retCode {body.get('retCode')}: {body.get('retMsg')}")
                return body["result"]
            except (httpx.TransportError, BybitError) as exc:
                last_exc = exc
                if attempt < self.retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise BybitError(f"{path} başarısız: {last_exc}")

    async def tickers(self) -> list[Ticker]:
        res = await self._get("/v5/market/tickers", {"category": self.category})
        out = []
        for t in res.get("list", []):
            out.append(Ticker(
                symbol=t["symbol"],
                last_price=_d(t.get("lastPrice")),
                bid=_d(t.get("bid1Price")),
                ask=_d(t.get("ask1Price")),
                turnover_24h=_d(t.get("turnover24h")),
                change_24h_pct=_d(t.get("price24hPcnt")) * 100,
                funding_rate=_d(t.get("fundingRate"), None),
                open_interest_value=_d(t.get("openInterestValue"), None),
            ))
        return out

    async def instruments(self) -> list[Instrument]:
        out: list[Instrument] = []
        cursor = ""
        for _ in range(20):  # sayfalama güvenlik sınırı
            params = {"category": self.category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            res = await self._get("/v5/market/instruments-info", params)
            for i in res.get("list", []):
                lot, price = i.get("lotSizeFilter", {}), i.get("priceFilter", {})
                out.append(Instrument(
                    symbol=i["symbol"],
                    status=i.get("status", ""),
                    quote_coin=i.get("quoteCoin", ""),
                    contract_type=i.get("contractType", ""),
                    launch_ms=int(i.get("launchTime") or 0),
                    tick_size=_d(price.get("tickSize")),
                    qty_step=_d(lot.get("qtyStep") or lot.get("basePrecision")),
                    min_order_qty=_d(lot.get("minOrderQty")),
                    min_notional=_d(lot.get("minNotionalValue") or lot.get("minOrderAmt")),
                ))
            cursor = res.get("nextPageCursor") or ""
            if not cursor:
                break
        return out

    async def klines(self, symbol: str, interval: str = "15", limit: int = 200) -> list[Candle]:
        if interval not in INTERVAL_MINUTES:
            raise ValueError(f"desteklenmeyen aralık: {interval}")
        res = await self._get("/v5/market/kline", {
            "category": self.category, "symbol": symbol, "interval": interval, "limit": min(limit, 1000)})
        rows = res.get("list", [])
        candles = [Candle(int(r[0]), _d(r[1]), _d(r[2]), _d(r[3]), _d(r[4]), _d(r[5]), _d(r[6]), interval)
                   for r in rows]
        candles.sort(key=lambda c: c.start_ms)  # Bybit en yeniden eskiye döner
        return candles

    async def orderbook(self, symbol: str, limit: int = 50) -> OrderBook:
        res = await self._get("/v5/market/orderbook", {"category": self.category, "symbol": symbol, "limit": limit})
        return OrderBook(
            symbol=symbol,
            bids=[(_d(p), _d(q)) for p, q in res.get("b", [])],
            asks=[(_d(p), _d(q)) for p, q in res.get("a", [])],
            ts_ms=int(res.get("ts") or 0),
        )

    # --- Türev piyasa verileri (BOT06) ---------------------------------
    async def open_interest(self, symbol: str, interval: str = "15min", limit: int = 48) -> list[tuple[int, Decimal]]:
        """(zaman_ms, açık pozisyon) eskiden yeniye. interval: 5min,15min,30min,1h,4h,1d"""
        res = await self._get("/v5/market/open-interest", {
            "category": self.category, "symbol": symbol, "intervalTime": interval, "limit": limit})
        rows = [(int(r["timestamp"]), _d(r["openInterest"])) for r in res.get("list", [])]
        return sorted(rows)

    async def funding_history(self, symbol: str, limit: int = 30) -> list[tuple[int, Decimal]]:
        res = await self._get("/v5/market/funding/history", {
            "category": self.category, "symbol": symbol, "limit": limit})
        rows = [(int(r["fundingRateTimestamp"]), _d(r["fundingRate"])) for r in res.get("list", [])]
        return sorted(rows)

    async def account_ratio(self, symbol: str, period: str = "1h", limit: int = 24) -> list[tuple[int, Decimal]]:
        """(zaman_ms, long hesap oranı 0-1) eskiden yeniye."""
        res = await self._get("/v5/market/account-ratio", {
            "category": self.category, "symbol": symbol, "period": period, "limit": limit})
        rows = [(int(r["timestamp"]), _d(r["buyRatio"])) for r in res.get("list", [])]
        return sorted(rows)

    async def recent_trades(self, symbol: str, limit: int = 1000) -> list[tuple[int, str, Decimal, Decimal]]:
        """(zaman_ms, taker yönü 'Buy'/'Sell', fiyat, miktar) eskiden yeniye."""
        res = await self._get("/v5/market/recent-trade", {
            "category": self.category, "symbol": symbol, "limit": limit})
        rows = [(int(r["time"]), r["side"], _d(r["price"]), _d(r["size"])) for r in res.get("list", [])]
        return sorted(rows, key=lambda x: x[0])
