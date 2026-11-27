"""Sistem ayarları.

Ayarlar ortam değişkenlerinden (.env) okunur. Risk sınırlarının kodda kilitli
tavanları vardır: .env ile bu tavanların üstüne çıkılamaz, uygulama açılmaz.
"""

from __future__ import annotations

from typing import Literal

from decimal import Decimal
from enum import Enum
from functools import lru_cache

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"  # Bybit demo hesabı veya sanal kasa
    SIGNAL_ONLY = "SIGNAL_ONLY"
    MANUAL_APPROVAL = "MANUAL_APPROVAL"
    AUTONOMOUS_LIVE = "AUTONOMOUS_LIVE"


class MarketType(str, Enum):
    SPOT = "spot"
    LINEAR = "linear"  # Bybit USDT vadeli (perpetual)


# --- Kodda kilitli tavanlar: .env bunları aşamaz -------------------------
HARD_CAPS = {
    # Şartname %0,5; kullanıcı kararıyla (27.11, demo testi) %1'e çıkarıldı. Diğer tavanlar aynı.
    "risk_per_trade_pct": Decimal("1"),
    "daily_loss_pct": Decimal("2"),
    "weekly_loss_pct": Decimal("5"),
    "max_drawdown_pct": Decimal("10"),
    "max_open_positions": 2,
    "max_position_capital_pct": Decimal("25"),
    "max_total_capital_pct": Decimal("50"),
    "max_leverage": 3,
}
MIN_REWARD_RISK = Decimal("2")


class RiskLimits(BaseModel):
    """Şartname bölüm 10.1 başlangıç risk parametreleri."""

    starting_capital_usd: Decimal = Decimal("200")
    risk_per_trade_pct: Decimal = Decimal("1")
    daily_loss_pct: Decimal = Decimal("2")
    weekly_loss_pct: Decimal = Decimal("5")
    max_drawdown_pct: Decimal = Decimal("10")
    max_open_positions: int = 2
    max_position_capital_pct: Decimal = Decimal("25")
    max_total_capital_pct: Decimal = Decimal("50")
    min_reward_risk: Decimal = Decimal("2")
    leverage: int = 2
    consecutive_loss_cooldown: int = 3
    cooldown_minutes: int = 30
    consecutive_loss_halt: int = 5
    # Yasaklar: değiştirilemez, sadece görünürlük için alan olarak tutulur.
    martingale_allowed: bool = False
    uncontrolled_dca_allowed: bool = False

    @model_validator(mode="after")
    def enforce_hard_caps(self) -> "RiskLimits":
        errors = []
        for key, cap in HARD_CAPS.items():
            if key == "max_leverage":
                if not 1 <= self.leverage <= cap:
                    errors.append(f"leverage 1 ile {cap} arasında olmalı (verilen {self.leverage})")
                continue
            value = getattr(self, key)
            if value <= 0:
                errors.append(f"{key} sıfırdan büyük olmalı")
            elif value > cap:
                errors.append(f"{key} kilitli tavanı aşıyor: {value} > {cap}")
        if self.min_reward_risk < MIN_REWARD_RISK:
            errors.append(f"min_reward_risk en az {MIN_REWARD_RISK} olmalı")
        if self.max_position_capital_pct > self.max_total_capital_pct:
            errors.append("tek pozisyon sınırı toplam sınırdan büyük olamaz")
        if self.martingale_allowed or self.uncontrolled_dca_allowed:
            errors.append("martingale ve kontrolsüz DCA yasaktır")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    @property
    def risk_budget_usd(self) -> Decimal:
        return self.starting_capital_usd * self.risk_per_trade_pct / 100

    @property
    def daily_loss_limit_usd(self) -> Decimal:
        return self.starting_capital_usd * self.daily_loss_pct / 100


class ProfitLockStep(BaseModel):
    peak_pct: Decimal
    lock_pct: Decimal


# Şartname bölüm 14.3 – başlangıç konfigürasyonu, backtest ile doğrulanacak.
DEFAULT_PROFIT_LOCK_TABLE = [
    ProfitLockStep(peak_pct=Decimal(p), lock_pct=Decimal(l))
    for p, l in [
        ("0.5", "0"), ("1", "0.5"), ("1.5", "0.8"), ("2", "1.2"), ("3", "2"),
        ("4", "3"), ("5", "4"), ("7", "5.6"), ("10", "8.5"),
    ]
]
PROFIT_LOCK_ABOVE_10_RATIO = Decimal("0.85")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__", extra="ignore")

    app_name: str = "MASTER CRYPTO AI X10"
    mode: TradingMode = TradingMode.PAPER

    # Borsa
    exchange: str = "bybit"
    market_type: MarketType = MarketType.LINEAR
    bybit_demo: bool = True
    bybit_api_key: str = Field(default="", repr=False)
    bybit_api_secret: str = Field(default="", repr=False)

    # Canlı işlem için iki ayrı açık onay gerekir.
    live_trading_confirmed: bool = False

    # Veritabanı (Render PostgreSQL veya yerelde SQLite)
    database_url: str = "sqlite:///./data/x10.db"

    # Panel yazma işlemleri için erişim anahtarı; boşsa yazma kapalıdır.
    dashboard_token: str = Field(default="", repr=False)

    # Telegram bildirimleri (boşsa kapalı)
    telegram_bot_token: str = Field(default="", repr=False)
    telegram_chat_id: str = Field(default="", repr=False)

    # Haber vetosu (şartname 8.3): açıklamadan önce/sonra dakika
    news_blackout_before_min: int = 15
    news_blackout_after_min: int = 15

    # Coin tarama başlangıç filtreleri (şartname 16.1)
    min_24h_volume_usdt: Decimal = Decimal("10000000")
    max_spread_pct: Decimal = Decimal("0.10")
    scan_top_n: int = 30
    min_listing_days: int = 30
    scanner_enabled: bool = True
    scanner_interval_sec: int = Field(default=300, ge=60)
    symbol_whitelist: list[str] = []
    symbol_blacklist: list[str] = []

    # Bot çalıştırıcı
    runner_enabled: bool = True
    runner_timeframes: list[str] = ["5m", "15m", "1h"]
    calendar_enabled: bool = True
    execution_enabled: bool = True     # onaylanan kararları uygula (anahtar yoksa simülasyon)
    monitor_interval_sec: int = Field(default=15, ge=5)
    arbitrage_enabled: bool = True
    footprint_enabled: bool = True   # websocket işlem akışı (BOT01 footprint stratejisi)
    runner_concurrency: int = Field(default=4, ge=1, le=10)
    # Giriş emri: "limit" = plan fiyatına PostOnly limit (maker komisyonu, fiyat kovalanmaz;
    # süre dolunca iptal) | "market" = piyasa emri (taker)
    entry_order_type: Literal["limit", "market"] = "limit"
    # Ana robotun risk değerlendirmesine gönderme eşiği (şartname 75; demo testinde kullanıcı
    # kararıyla 70). Kodda 65-85 aralığına sabitlenir.
    risk_score_threshold: int = 70
    # Kısmi kâr alma: fiyat 1R kâra ulaşınca pozisyonun yarısı kapatılır, stop başabaşa çekilir,
    # kalan yarı hedefe/kâr kilidine kadar taşınır.
    partial_tp_enabled: bool = True
    partial_tp_r: float = 1.0
    partial_tp_fraction: float = 0.5
    maker_fee_pct: float = 0.02     # Bybit USDT perpetual maker komisyonu (tek yön, %)
    taker_fee_pct: float = 0.055  # Bybit USDT perpetual taker komisyonu (tek yön, %)

    risk: RiskLimits = RiskLimits()

    @model_validator(mode="after")
    def guard_live_mode(self) -> "Settings":
        if self.mode == TradingMode.AUTONOMOUS_LIVE:
            if not self.live_trading_confirmed:
                raise ValueError("Canlı mod için LIVE_TRADING_CONFIRMED=true gerekir")
            if self.bybit_demo:
                raise ValueError("Canlı mod demo hesapla çalıştırılamaz (BYBIT_DEMO=false gerekir)")
        if self.market_type == MarketType.SPOT and self.risk.leverage != 1:
            raise ValueError("Spot piyasada kaldıraç 1 olmalıdır")
        return self

    @property
    def sqlalchemy_url(self) -> str:
        """Render 'postgres://' verir; SQLAlchemy psycopg sürücüsü ister."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url

    @property
    def bybit_rest_url(self) -> str:
        return "https://api-demo.bybit.com" if self.bybit_demo else "https://api.bybit.com"

    def public_summary(self) -> dict:
        """Panelde gösterilebilecek, gizli bilgi içermeyen özet."""
        return {
            "app_name": self.app_name,
            "mode": self.mode.value,
            "exchange": self.exchange,
            "market_type": self.market_type.value,
            "bybit_demo": self.bybit_demo,
            "api_key_configured": bool(self.bybit_api_key),
            "telegram_configured": bool(self.telegram_bot_token and self.telegram_chat_id),
            "risk": {k: str(v) if isinstance(v, Decimal) else v for k, v in self.risk.model_dump().items()},
            "news_blackout_min": [self.news_blackout_before_min, self.news_blackout_after_min],
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
