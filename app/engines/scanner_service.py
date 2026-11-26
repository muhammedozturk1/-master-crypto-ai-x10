"""Coin tarayıcıyı arka planda düzenli çalıştırır ve sonucu kaydeder."""

from __future__ import annotations

import asyncio
import logging
import time

from app.config import Settings
from app.database.models import ScanSnapshot
from app.database.session import session_scope
from app.engines.market_scanner import ScanConfig, run_scan
from app.exchange.bybit_public import BybitPublic

log = logging.getLogger("x10.scanner")


def scan_config(settings: Settings) -> ScanConfig:
    return ScanConfig(
        min_turnover_usdt=settings.min_24h_volume_usdt,
        max_spread_pct=settings.max_spread_pct,
        top_n=settings.scan_top_n,
        min_listing_days=settings.min_listing_days,
        whitelist=tuple(settings.symbol_whitelist),
        blacklist=tuple(settings.symbol_blacklist) + tuple(sorted(_blocked())),
    )


def _blocked() -> set[str]:
    """Emir kabul etmeyen (sözleşme imzası isteyen) pariteler taramaya alınmaz: aday yerini
    işlem açılabilecek paritelere bırakır."""
    try:
        from app.bots.bot09_execution import agreement_blocked_symbols
        return agreement_blocked_symbols()
    except Exception:  # noqa: BLE001
        return set()


async def scan_once(client: BybitPublic, settings: Settings) -> ScanSnapshot:
    t0 = time.monotonic()
    try:
        result = await run_scan(client, scan_config(settings))
        snap = ScanSnapshot(ok=True, selected=len(result.candidates), total_symbols=result.total_symbols,
                            result=result.to_dict(), duration_ms=int((time.monotonic() - t0) * 1000))
        log.info("Tarama: %d/%d aday", snap.selected, snap.total_symbols)
    except Exception as exc:  # noqa: BLE001 – hata kaydedilir, döngü durmaz
        snap = ScanSnapshot(ok=False, error=str(exc)[:500], duration_ms=int((time.monotonic() - t0) * 1000))
        log.warning("Tarama başarısız: %s", exc)
    with session_scope() as s:
        s.add(snap)
    return snap


async def scanner_loop(client: BybitPublic, settings: Settings) -> None:
    while True:
        await scan_once(client, settings)
        await asyncio.sleep(settings.scanner_interval_sec)
