"""BOT05 – Haber ve piyasa duyarlılığı botu (şartname bölüm 8). Tüm piyasa için çalışır.

Ürettiği bilgiler (ana robot kullanacak):
  - Ekonomik takvim vetosu: yüksek etkili USD açıklamasından 15 dk önce / 15 dk sonra yeni işlem yok
  - Fear & Greed endeksinden -100..+100 duyarlılık skoru (yalnız bağlam, işlem sinyali değil)
Haber akışı ve sosyal medya kaynakları bağlı değil; bu eksiklik her kayıtta belirtilir.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.bots.base import MarketContext, WorkerBot
from app.domain.signals import Action, DataQuality, Signal
from app.engines.calendar_service import MISSING_SOURCES, CalendarState, fng_sentiment, veto_status

BOT_ID = "BOT05"


class NewsBot(WorkerBot):
    bot_id = BOT_ID
    timeframes = frozenset({"15m"})
    market_wide = True

    def __init__(self, before_min: int = 15, after_min: int = 15):
        self.before_min, self.after_min = before_min, after_min

    def analyze(self, ctx: MarketContext) -> list[Signal]:
        st: CalendarState | None = ctx.extra.get("calendar")
        if st is None or st.fetched_at is None:
            return []
        now = ctx.extra.get("now") or datetime.now(timezone.utc)
        v = veto_status(st.events, now, self.before_min, self.after_min)
        senti = fng_sentiment(st.fear_greed)
        quality = DataQuality.IYI if not st.errors else DataQuality.EKSIK
        warns = [f"bağlı olmayan kaynak: {', '.join(MISSING_SOURCES)}"]
        if st.errors:
            warns.append(f"veri alınamadı: {', '.join(st.errors)}")
        if v["veto_active"]:
            ev = v["veto_events"][0]
            key, reason = f"veto:{ev['time']}:{ev['title']}", f"İşlem vetosu: {ev['title']} ({ev['country']}, {ev['time']}); {v['veto_until']} sonrasına kadar yeni işlem açılmamalı"
        else:
            day = now.strftime("%Y-%m-%d")
            key = f"sentiment:{day}:{st.fear_greed}"
            nxt = v["next_event"]
            reason = (f"Veto yok. Fear & Greed {st.fear_greed} ({st.fear_greed_label}), duyarlılık {senti}. "
                      + (f"Sonraki yüksek etkili veri: {nxt['title']} ({v['minutes_to_next']} dk)" if nxt else "Bu hafta başka yüksek etkili veri yok"))
        return [Signal(
            bot_id=BOT_ID, strategy_id="news_veto" if v["veto_active"] else "sentiment",
            symbol="PIYASA", exchange=ctx.exchange, timeframe=ctx.timeframe, action=Action.BEKLE,
            confidence=0, reason=reason, data_quality=quality, risk_warnings=warns,
            valid_for_seconds=900,
            details={**v, "fear_greed": st.fear_greed, "sentiment": senti,
                     "dedup_key": f"{BOT_ID}:{key}"},
        )]
