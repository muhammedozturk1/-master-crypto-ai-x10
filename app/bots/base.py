"""Botların ortak girdi yapısı ve yardımcıları."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal

from app.data.candles import Candle
from app.domain.signals import Signal
from app.indicators import core as I


@dataclass
class MarketContext:
    """Bir botun tek bir sembol ve zaman dilimi için ihtiyaç duyduğu veri.

    `candles` yalnızca kapanmış ve kalite kontrolünden geçmiş mumları içerir.
    """

    symbol: str
    timeframe: str
    candles: list[Candle]
    htf_candles: list[Candle] | None = None  # bir üst zaman dilimi (trend filtresi)
    spread_pct: float | None = None
    taker_fee_pct: float = 0.055  # Bybit USDT perpetual taker (tek yön)
    allow_short: bool = True
    exchange: str = "bybit"
    extra: dict = field(default_factory=dict)

    @property
    def closes(self) -> list[float]:
        return [float(c.close) for c in self.candles]

    @property
    def highs(self) -> list[float]:
        return [float(c.high) for c in self.candles]

    @property
    def lows(self) -> list[float]:
        return [float(c.low) for c in self.candles]

    @property
    def volumes(self) -> list[float]:
        return [float(c.volume) for c in self.candles]

    def cost_per_unit(self, price: float) -> float:
        """Giriş + çıkış komisyonu ve yarım spread × 2 (fiyat birimi cinsinden)."""
        spread = (self.spread_pct or 0.0) / 100 * price
        return price * (2 * self.taker_fee_pct / 100) + spread


def htf_trend(candles: list[Candle] | None) -> str | None:
    """Üst zaman dilimi yönü: UP / DOWN / FLAT (EMA50 eğimi ve fiyatın konumu)."""
    if not candles or len(candles) < 60:
        return None
    closes = [float(c.close) for c in candles]
    e50 = I.ema(closes, 50)
    s = I.slope(e50, len(closes) - 1, 5)
    if s is None:
        return None
    if closes[-1] > e50[-1] and s > 0:
        return "UP"
    if closes[-1] < e50[-1] and s < 0:
        return "DOWN"
    return "FLAT"


def px(value: float) -> Decimal:
    """Float fiyatı Signal için Decimal'e çevirir (10 anlamlı basamak).
    Borsa fiyat adımına yuvarlama emir katmanında (Aşama 6) yapılır."""
    return Decimal(f"{value:.10g}")


class WorkerBot(ABC):
    bot_id: str

    @abstractmethod
    def analyze(self, ctx: MarketContext) -> list[Signal]:
        """Kapanmış mumlarla analiz yapıp sinyal listesi döndürür (boş olabilir)."""
