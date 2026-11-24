"""Coin tarama ve seçim motoru (şartname bölüm 16.1).

Uygun USDT paritelerini filtreler ve en likit N tanesini sıralar. Bu motor
yalnızca aday listesi üretir; hiçbir coini otomatik satın almaz.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.data.candles import now_ms
from app.exchange.bybit_public import BybitPublic, Instrument, Ticker

DAY_MS = 86_400_000


@dataclass
class ScanConfig:
    min_turnover_usdt: Decimal = Decimal("10000000")
    max_spread_pct: Decimal = Decimal("0.10")
    top_n: int = 30
    min_listing_days: int = 30
    whitelist: tuple[str, ...] = ()
    blacklist: tuple[str, ...] = ()


@dataclass
class Candidate:
    symbol: str
    last_price: Decimal
    turnover_24h: Decimal
    spread_pct: Decimal
    change_24h_pct: Decimal
    funding_rate: Decimal | None
    listing_days: int
    min_order_qty: Decimal
    min_notional: Decimal

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "last_price": str(self.last_price),
            "turnover_24h": str(self.turnover_24h.quantize(Decimal("1"))),
            "spread_pct": str(self.spread_pct.quantize(Decimal("0.0001"))),
            "change_24h_pct": str(self.change_24h_pct.quantize(Decimal("0.01"))),
            "funding_rate": str(self.funding_rate) if self.funding_rate is not None else None,
            "listing_days": self.listing_days,
            "min_order_qty": str(self.min_order_qty),
            "min_notional": str(self.min_notional),
        }


@dataclass
class ScanResult:
    candidates: list[Candidate]
    total_symbols: int
    rejected: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "total_symbols": self.total_symbols,
            "selected": len(self.candidates),
            "rejected": self.rejected,
            "candidates": [c.to_dict() for c in self.candidates],
        }


def select_candidates(tickers: list[Ticker], instruments: list[Instrument], cfg: ScanConfig,
                      at_ms: int | None = None) -> ScanResult:
    t_now = now_ms() if at_ms is None else at_ms
    inst = {i.symbol: i for i in instruments}
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    out: list[Candidate] = []
    for t in tickers:
        i = inst.get(t.symbol)
        if i is None:
            reject("bilgi_yok"); continue
        if t.symbol in cfg.blacklist:
            reject("kara_liste"); continue
        if cfg.whitelist and t.symbol not in cfg.whitelist:
            reject("beyaz_liste_disi"); continue
        if i.status != "Trading":
            reject("islem_kapali"); continue
        if i.quote_coin != "USDT" or (i.contract_type and i.contract_type != "LinearPerpetual"):
            reject("usdt_perp_degil"); continue
        days = (t_now - i.launch_ms) // DAY_MS if i.launch_ms else 9999
        if days < cfg.min_listing_days:
            reject("yeni_listeleme"); continue
        if t.turnover_24h < cfg.min_turnover_usdt:
            reject("dusuk_hacim"); continue
        spread = t.spread_pct
        if spread is None:
            reject("fiyat_yok"); continue
        if spread > cfg.max_spread_pct:
            reject("yuksek_spread"); continue
        out.append(Candidate(t.symbol, t.last_price, t.turnover_24h, spread, t.change_24h_pct,
                             t.funding_rate, int(days), i.min_order_qty, i.min_notional))

    out.sort(key=lambda c: c.turnover_24h, reverse=True)
    if len(out) > cfg.top_n:
        rejected["ilk_n_disi"] = len(out) - cfg.top_n
        out = out[: cfg.top_n]
    return ScanResult(out, total_symbols=len(tickers), rejected=rejected)


async def run_scan(client: BybitPublic, cfg: ScanConfig) -> ScanResult:
    tickers = await client.tickers()
    instruments = await client.instruments()
    return select_candidates(tickers, instruments, cfg)
