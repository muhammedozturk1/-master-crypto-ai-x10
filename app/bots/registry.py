"""10 çalışan botun kimlikleri ve rolleri (şartname bölüm 2).

Botlar analiz yapar, sinyal üretir; hiçbiri kendi başına işlem açamaz.
Strateji kodu yazılan botlar IMPLEMENTATIONS sözlüğüne kaydedilir; kayıtlı
olmayan botlar `/bots` ekranında `implemented=False` görünür ve sinyal üretmez.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.bots.base import WorkerBot
from app.bots.bot01_technical import TechnicalBot
from app.bots.bot02_trend import TrendBot
from app.bots.bot03_scalping import ScalpingBot
from app.bots.bot04_arbitrage import ArbitrageBot
from app.bots.bot05_news import NewsBot
from app.bots.bot06_flow import FlowBot
from app.bots.bot10_nr7 import Nr7Bot


@dataclass(frozen=True)
class BotInfo:
    bot_id: str
    name: str
    role: str
    kind: str  # "signal" sinyal üretir, "guard" zorunlu kontrol, "service" uygulama katmanı
    stage: int  # hangi geliştirme aşamasında gerçekleştirilecek


BOTS: tuple[BotInfo, ...] = (
    BotInfo("BOT01", "Teknik analiz ve formasyon", "EMA/RSI trend devamı, Bollinger mean reversion, destek-direnç kırılımı, mum formasyonları", "signal", 3),
    BotInfo("BOT02", "Trend ve momentum", "Trend pullback, Donchian breakout, Supertrend momentum, göreceli güç", "signal", 3),
    BotInfo("BOT03", "Scalping", "EMA 9/21, VWAP reclaim, mikro kırılım; maliyet sonrası beklenen değer yoksa pasif", "signal", 3),
    BotInfo("BOT04", "Arbitraj", "Borsalar arası ve üçgen arbitraj; 200 USD'de varsayılan gözlem modu", "signal", 3),
    BotInfo("BOT05", "Haber ve piyasa duyarlılığı", "Ekonomik takvim vetosu, haber skoru -100..+100", "signal", 3),
    BotInfo("BOT06", "Balina, on-chain ve likidite", "Funding, open interest, likidasyon, CVD, emir defteri; manipülasyon filtresi", "signal", 3),
    BotInfo("BOT07", "Risk ve sermaye koruma", "Zorunlu risk katmanı; ana robot bu kontrolleri aşamaz", "guard", 5),
    BotInfo("BOT08", "Portföy ve sermaye optimizasyonu", "Ortak kasa takibi, bileşik sermaye, strateji performans raporu", "guard", 5),
    BotInfo("BOT09", "Emir ve pozisyon yönetimi", "Borsa iletişimi; çift emir koruması, uzlaştırma, borsa tarafı stop", "service", 6),
    BotInfo("BOT10", "NR7 volatilite sıkışması", "NR7 breakout, NR7+EMA, NR7+Inside Bar, NR7+Bollinger squeeze, retest", "signal", 3),
)

BOTS_BY_ID = {b.bot_id: b for b in BOTS}

IMPLEMENTATIONS: dict[str, type[WorkerBot]] = {
    "BOT01": TechnicalBot,
    "BOT02": TrendBot,
    "BOT03": ScalpingBot,
    "BOT04": ArbitrageBot,
    "BOT05": NewsBot,
    "BOT06": FlowBot,
    "BOT10": Nr7Bot,
}
