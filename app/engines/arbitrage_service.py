"""Arbitraj gözlemi (BOT04'ün veri kaynağı) – yalnızca gözlem ve raporlama.

200 USD'lik kasada borsalar arası arbitraj; transfer süresi ve ücreti, sermayenin bölünmesi
ve bacak riski yüzünden ekonomik değildir (şartname 7.3). Bu servis emir göndermez.

  Üçgen arbitraj   : Bybit spot içinde USDT → X → Y → USDT ve ters yön, gerçek alış/satış fiyatları
  Borsalar arası   : aynı paritenin Bybit, Binance ve OKX spot fiyat farkı
Net getiri = brüt fark − her bacağın taker komisyonu. Transfer ücreti ve süresi hesaba
KATILMAZ (çekim ücretleri bağlı değil); bu yüzden borsalar arası net değer iyimserdir.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.exchange.bybit_public import BybitPublic

log = logging.getLogger("x10.arbitrage")

BINANCE_BOOK = "https://api.binance.com/api/v3/ticker/bookTicker"
OKX_TICKERS = "https://www.okx.com/api/v5/market/tickers?instType=SPOT"
SPOT_TAKER_FEE_PCT = 0.1
BRIDGES = ("BTC", "ETH")


@dataclass
class ArbState:
    triangular: list[dict] = field(default_factory=list)
    cross: list[dict] = field(default_factory=list)
    checked_at: datetime | None = None
    errors: dict[str, str] = field(default_factory=dict)

    def best(self) -> dict | None:
        allx = self.triangular + self.cross
        return max(allx, key=lambda x: x["net_pct"]) if allx else None


def triangular(book: dict[str, tuple[float, float]], coins: list[str], fee_pct: float = SPOT_TAKER_FEE_PCT) -> list[dict]:
    """book: sembol → (bid, ask). Her döngü için 1 USDT'nin net getirisi (%)."""
    f = 1 - fee_pct / 100
    out = []
    for bridge in BRIDGES:
        bu = book.get(f"{bridge}USDT")
        if not bu:
            continue
        for y in coins:
            if y in (bridge, "USDT"):
                continue
            yb, yu = book.get(f"{y}{bridge}"), book.get(f"{y}USDT")
            if not yb or not yu or min(*bu, *yb, *yu) <= 0:
                continue
            # USDT → bridge → Y → USDT
            a = (1 / bu[1]) * f            # bridge al (ask)
            a = (a / yb[1]) * f            # Y al, bridge ile (ask)
            a = a * yu[0] * f              # Y sat, USDT (bid)
            out.append({"path": f"USDT→{bridge}→{y}→USDT", "net_pct": round((a - 1) * 100, 4)})
            # USDT → Y → bridge → USDT
            b = (1 / yu[1]) * f
            b = b * yb[0] * f
            b = b * bu[0] * f
            out.append({"path": f"USDT→{y}→{bridge}→USDT", "net_pct": round((b - 1) * 100, 4)})
    return sorted(out, key=lambda x: -x["net_pct"])


def cross_exchange(books: dict[str, dict[str, tuple[float, float]]], symbols: list[str],
                   fee_pct: float = SPOT_TAKER_FEE_PCT) -> list[dict]:
    """books: borsa → sembol → (bid, ask). En ucuz alış ile en pahalı satış arasındaki fark."""
    out = []
    for s in symbols:
        quotes = {ex: b[s] for ex, b in books.items() if s in b and b[s][0] > 0 and b[s][1] > 0}
        if len(quotes) < 2:
            continue
        buy_ex = min(quotes, key=lambda e: quotes[e][1])
        sell_ex = max(quotes, key=lambda e: quotes[e][0])
        if buy_ex == sell_ex:
            continue
        gross = (quotes[sell_ex][0] / quotes[buy_ex][1] - 1) * 100
        out.append({"symbol": s, "buy": buy_ex, "sell": sell_ex, "gross_pct": round(gross, 4),
                    "net_pct": round(gross - 2 * fee_pct, 4), "transfer_cost": "hesaba katılmadı"})
    return sorted(out, key=lambda x: -x["net_pct"])


class ArbitrageService:
    def __init__(self, bybit_spot: BybitPublic | None = None, http: httpx.AsyncClient | None = None,
                 refresh_sec: int = 300, symbols_provider=None):
        self.spot = bybit_spot or BybitPublic(category="spot")
        self.http = http or httpx.AsyncClient(timeout=10.0)
        self.refresh_sec = refresh_sec
        self.symbols_provider = symbols_provider or (lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"])
        self.state = ArbState()

    async def _binance(self) -> dict[str, tuple[float, float]]:
        r = await self.http.get(BINANCE_BOOK)
        r.raise_for_status()
        return {d["symbol"]: (float(d["bidPrice"]), float(d["askPrice"])) for d in r.json()}

    async def _okx(self) -> dict[str, tuple[float, float]]:
        r = await self.http.get(OKX_TICKERS)
        r.raise_for_status()
        out = {}
        for d in r.json().get("data", []):
            if d.get("bidPx") and d.get("askPx"):
                out[d["instId"].replace("-", "")] = (float(d["bidPx"]), float(d["askPx"]))
        return out

    async def refresh(self) -> ArbState:
        errors: dict[str, str] = {}
        books: dict[str, dict[str, tuple[float, float]]] = {}
        try:
            books["bybit"] = {t.symbol: (float(t.bid), float(t.ask)) for t in await self.spot.tickers()}
        except Exception as exc:  # noqa: BLE001
            errors["bybit"] = str(exc)[:200]
        for name, fn in (("binance", self._binance), ("okx", self._okx)):
            try:
                books[name] = await fn()
            except Exception as exc:  # noqa: BLE001
                errors[name] = str(exc)[:200]
        symbols = self.symbols_provider()
        coins = [s[:-4] for s in symbols if s.endswith("USDT")]
        tri = triangular(books.get("bybit", {}), coins)[:10]
        cross = cross_exchange(books, symbols)[:10]
        self.state = ArbState(tri, cross, datetime.now(timezone.utc), errors)
        best = self.state.best()
        log.info("Arbitraj gözlemi: üçgen %d, borsalar arası %d, en iyi net %s, hata %s",
                 len(tri), len(cross), best["net_pct"] if best else None, list(errors) or "yok")
        return self.state

    async def loop(self) -> None:
        await asyncio.sleep(30)
        while True:
            await self.refresh()
            await asyncio.sleep(self.refresh_sec)

    async def close(self) -> None:
        await self.http.aclose()
        await self.spot.close()
