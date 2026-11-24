"""Çalışan botların ürettiği standart sinyal formatı (şartname bölüm 15)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class Action(str, Enum):
    AL = "AL"
    SAT = "SAT"
    BEKLE = "BEKLE"


class DataQuality(str, Enum):
    IYI = "IYI"
    GECIKMELI = "GECIKMELI"
    EKSIK = "EKSIK"


class Signal(BaseModel):
    bot_id: str = Field(pattern=r"^BOT(0[1-9]|10)$")
    strategy_id: str
    symbol: str
    exchange: str = "bybit"
    timeframe: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    action: Action
    confidence: int = Field(ge=0, le=100)
    entry_low: Decimal | None = None
    entry_high: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    expected_net_rr: Decimal | None = None
    estimated_cost_pct: Decimal | None = None
    market_regime: str | None = None
    valid_for_seconds: int = Field(default=900, gt=0)
    reason: str
    data_quality: DataQuality = DataQuality.IYI
    risk_warnings: list[str] = []

    @model_validator(mode="after")
    def check_levels(self) -> "Signal":
        if self.action == Action.BEKLE:
            return self
        missing = [n for n in ("entry_low", "entry_high", "stop_loss", "take_profit") if getattr(self, n) is None]
        if missing:
            raise ValueError(f"AL/SAT sinyalinde zorunlu alanlar eksik: {', '.join(missing)}")
        if self.entry_low > self.entry_high:
            raise ValueError("entry_low entry_high'dan büyük olamaz")
        if self.action == Action.AL and not (self.stop_loss < self.entry_low and self.take_profit > self.entry_high):
            raise ValueError("AL sinyalinde stop girişin altında, hedef üstünde olmalı")
        if self.action == Action.SAT and not (self.stop_loss > self.entry_high and self.take_profit < self.entry_low):
            raise ValueError("SAT sinyalinde stop girişin üstünde, hedef altında olmalı")
        return self

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(seconds=self.valid_for_seconds)

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= self.expires_at

    def gross_reward_risk(self) -> Decimal | None:
        """Maliyetler hariç ödül/risk oranı (giriş bölgesi ortası üzerinden)."""
        if self.action == Action.BEKLE:
            return None
        mid = (self.entry_low + self.entry_high) / 2
        risk = abs(mid - self.stop_loss)
        reward = abs(self.take_profit - mid)
        return (reward / risk).quantize(Decimal("0.01")) if risk else None
