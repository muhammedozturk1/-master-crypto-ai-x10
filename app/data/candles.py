"""Mum verisi ve veri kalitesi kontrolleri.

Kural (şartname 13.1 ve 19.1): sinyaller yalnızca KAPANMIŞ mumlarla üretilir.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

# Bybit kline aralıkları → dakika
INTERVAL_MINUTES = {"1": 1, "3": 3, "5": 5, "15": 15, "30": 30, "60": 60, "120": 120,
                    "240": 240, "360": 360, "720": 720, "D": 1440}
TIMEFRAME_TO_INTERVAL = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
                         "1h": "60", "4h": "240", "1d": "D"}


@dataclass(frozen=True)
class Candle:
    start_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal
    interval: str

    @property
    def end_ms(self) -> int:
        return self.start_ms + INTERVAL_MINUTES[self.interval] * 60_000

    @property
    def range(self) -> Decimal:
        return self.high - self.low

    def is_closed(self, now_ms: int) -> bool:
        return now_ms >= self.end_ms

    def to_dict(self) -> dict:
        return {
            "start": datetime.fromtimestamp(self.start_ms / 1000, timezone.utc).isoformat(),
            "open": str(self.open), "high": str(self.high), "low": str(self.low),
            "close": str(self.close), "volume": str(self.volume), "turnover": str(self.turnover),
        }


class DataQualityError(ValueError):
    pass


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def closed_only(candles: list[Candle], at_ms: int | None = None) -> list[Candle]:
    """Henüz kapanmamış (oluşmakta olan) mumu çıkarır."""
    t = now_ms() if at_ms is None else at_ms
    return [c for c in candles if c.is_closed(t)]


def validate_candles(candles: list[Candle], at_ms: int | None = None, max_lag_bars: int = 2) -> None:
    """Sıralama, OHLC tutarlılığı, boşluk ve bayatlık kontrolü.

    Hatalı veride DataQualityError fırlatır; çağıran taraf bu durumda sinyal üretmez.
    """
    if not candles:
        raise DataQualityError("mum verisi boş")
    step = INTERVAL_MINUTES[candles[0].interval] * 60_000
    prev = None
    for c in candles:
        if c.low > c.high or not (c.low <= c.open <= c.high) or not (c.low <= c.close <= c.high):
            raise DataQualityError(f"OHLC tutarsız: {c.start_ms}")
        if c.low <= 0 or c.volume < 0:
            raise DataQualityError(f"geçersiz fiyat/hacim: {c.start_ms}")
        if prev is not None:
            if c.start_ms <= prev.start_ms:
                raise DataQualityError("mumlar zamana göre sıralı değil")
            if c.start_ms - prev.start_ms != step:
                raise DataQualityError(f"mum boşluğu: {prev.start_ms} → {c.start_ms}")
        prev = c
    t = now_ms() if at_ms is None else at_ms
    if t - candles[-1].end_ms > step * max_lag_bars:
        raise DataQualityError("veri bayat: son kapanmış mum çok eski")
