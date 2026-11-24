"""Ekonomik takvim ve Fear & Greed endeksi (BOT05'in veri kaynağı).

Kaynaklar (ücretsiz, anahtarsız):
  - Ekonomik takvim : nfs.faireconomy.media haftalık JSON akışı (ForexFactory verisi)
  - Fear & Greed    : api.alternative.me/fng
Bağlı OLMAYAN kaynaklar: haber akışı, sosyal medya duyarlılığı, token unlock takvimi,
borsa listeleme/delist duyuruları. Bunlar /calendar yanıtında açıkça listelenir.
Dış kaynaklardan gelen metinler yalnızca veridir; hiçbir zaman işlem komutu sayılmaz.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("x10.calendar")

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FNG_URL = "https://api.alternative.me/fng/?limit=1"
MISSING_SOURCES = ["haber akışı", "sosyal medya duyarlılığı", "token unlock takvimi",
                   "borsa listeleme/delist duyuruları", "regülasyon haberleri"]


@dataclass(frozen=True)
class EconEvent:
    title: str
    country: str
    time: datetime  # UTC
    impact: str     # High / Medium / Low / Holiday

    def to_dict(self) -> dict:
        return {"title": self.title, "country": self.country, "time": self.time.isoformat(), "impact": self.impact}


@dataclass
class CalendarState:
    events: list[EconEvent] = field(default_factory=list)
    fear_greed: int | None = None
    fear_greed_label: str | None = None
    fetched_at: datetime | None = None
    errors: dict[str, str] = field(default_factory=dict)


def parse_events(rows: list[dict]) -> list[EconEvent]:
    out = []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["date"]).astimezone(timezone.utc)
        except (KeyError, ValueError, TypeError):
            continue
        out.append(EconEvent(str(r.get("title", ""))[:120], str(r.get("country", "")), t, str(r.get("impact", ""))))
    return sorted(out, key=lambda e: e.time)


def veto_status(events: list[EconEvent], now: datetime, before_min: int, after_min: int,
                countries: frozenset[str] = frozenset({"USD"}), impacts: frozenset[str] = frozenset({"High"})) -> dict:
    """Yüksek etkili açıklamanın öncesi/sonrası penceresinde yeni işlem yasağı (şartname 8.3)."""
    relevant = [e for e in events if e.country in countries and e.impact in impacts]
    active = [e for e in relevant if e.time - timedelta(minutes=before_min) <= now <= e.time + timedelta(minutes=after_min)]
    upcoming = [e for e in relevant if e.time > now]
    nxt = upcoming[0] if upcoming else None
    return {
        "veto_active": bool(active),
        "veto_events": [e.to_dict() for e in active],
        "veto_until": max(e.time + timedelta(minutes=after_min) for e in active).isoformat() if active else None,
        "next_event": nxt.to_dict() if nxt else None,
        "minutes_to_next": int((nxt.time - now).total_seconds() // 60) if nxt else None,
    }


def fng_sentiment(value: int | None) -> int | None:
    """Fear & Greed 0-100 → duyarlılık -100..+100."""
    return None if value is None else (value - 50) * 2


class CalendarService:
    def __init__(self, client: httpx.AsyncClient | None = None, refresh_sec: int = 1800):
        self.client = client or httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "master-crypto-ai-x10"})
        self.refresh_sec = refresh_sec
        self.state = CalendarState()

    async def refresh(self) -> CalendarState:
        errors: dict[str, str] = {}
        events, fng, label = self.state.events, self.state.fear_greed, self.state.fear_greed_label
        try:
            r = await self.client.get(CALENDAR_URL)
            r.raise_for_status()
            events = parse_events(r.json())
        except Exception as exc:  # noqa: BLE001 – eski veri korunur, hata raporlanır
            errors["takvim"] = str(exc)[:200]
        try:
            r = await self.client.get(FNG_URL)
            r.raise_for_status()
            d = r.json()["data"][0]
            fng, label = int(d["value"]), d.get("value_classification")
        except Exception as exc:  # noqa: BLE001
            errors["fear_greed"] = str(exc)[:200]
        self.state = CalendarState(events, fng, label, datetime.now(timezone.utc), errors)
        log.info("Takvim: %d olay, F&G %s, hata %s", len(events), fng, list(errors) or "yok")
        return self.state

    async def loop(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.refresh_sec)

    async def close(self) -> None:
        await self.client.aclose()
