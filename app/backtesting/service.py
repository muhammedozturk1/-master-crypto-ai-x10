"""Backtest servisi: gerçek Bybit geçmiş verisini çeker, motoru arka planda çalıştırır,
sonucu kaydeder ve bot ağırlığı önerisi üretir. Ağırlıklar OTOMATİK değişmez; kullanıcı
onayıyla uygulanır (şartname 18.3: yeterli örnek olmadan aşırı değişiklik yok)."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone

from app.backtesting.engine import Backtester, BacktestParams
from app.config import Settings
from app.data.candles import INTERVAL_MINUTES, TIMEFRAME_TO_INTERVAL
from app.database.models import BacktestRun
from app.database.session import session_scope
from app.exchange.bybit_public import BybitPublic

log = logging.getLogger("x10.backtest")
HTF = {"5m": "15m", "15m": "1h", "1h": "4h"}
MIN_TRADES_FOR_WEIGHT = 30
MAX_SYMBOLS, MAX_DAYS = 10, 90
DEFAULT_BOTS = {"5m": ("BOT03", "BOT10"), "15m": ("BOT01", "BOT02", "BOT10"), "1h": ("BOT01", "BOT02", "BOT10")}


def suggest_weights(by_bot: dict) -> dict:
    """Beklenen R'ye göre 0,5-1,5 arası ağırlık önerisi; 30 işlemden azsa 1,0 (yetersiz örnek)."""
    out = {}
    for bot, m in by_bot.items():
        if m["trades"] < MIN_TRADES_FOR_WEIGHT:
            out[bot] = {"weight": 1.0, "note": f"yetersiz örnek ({m['trades']} < {MIN_TRADES_FOR_WEIGHT})"}
            continue
        w = 1 + 0.5 * math.tanh(m["expectancy_r"] * 2)
        out[bot] = {"weight": round(max(0.5, min(1.5, round(w / 0.05) * 0.05)), 2),
                    "note": f"{m['trades']} işlem, beklenen {m['expectancy_r']}R"}
    return out


async def fetch(market: BybitPublic, symbol: str, tf: str, start_ms: int, end_ms: int):
    cs = await market.klines_range(symbol, TIMEFRAME_TO_INTERVAL[tf], start_ms, end_ms)
    step = INTERVAL_MINUTES[TIMEFRAME_TO_INTERVAL[tf]] * 60_000
    return [c for c in cs if c.start_ms + step <= end_ms]   # yalnız kapanmış mumlar


async def run_job(run_id: int, req: dict, market: BybitPublic, settings: Settings) -> None:
    t0 = time.monotonic()
    try:
        tf, days = req["timeframe"], req["days"]
        end = int(time.time() * 1000)
        warm = 260 * INTERVAL_MINUTES[TIMEFRAME_TO_INTERVAL[tf]] * 60_000
        start = end - days * 86_400_000 - warm
        data, htf, problems = {}, {}, {}
        for sym in req["symbols"]:
            try:
                data[sym] = await fetch(market, sym, tf, start, end)
                if HTF.get(tf):
                    htf[sym] = await fetch(market, sym, HTF[tf], start - 120 * 4 * 3_600_000, end)
            except Exception as exc:  # noqa: BLE001
                problems[sym] = str(exc)[:200]
        btc = await fetch(market, "BTCUSDT", tf, start, end)
        params = BacktestParams(timeframe=tf, mode=req["mode"], profit_lock=True, bots=tuple(req["bots"]),
                                fee_pct=settings.taker_fee_pct, limits=settings.risk,
                                start_capital=float(settings.risk.starting_capital_usd),
                                allow_short=settings.market_type.value == "linear")
        result = await asyncio.to_thread(Backtester(data, htf, btc, params).run)
        if req.get("compare_profit_lock"):
            params_off = BacktestParams(**{**params.__dict__, "profit_lock": False})
            off = await asyncio.to_thread(Backtester(data, htf, btc, params_off).run)
            result["profit_lock_comparison"] = {"with_lock": result["summary"], "without_lock": off["summary"]}
        result["suggested_weights"] = suggest_weights(result["by_bot"])
        result["data"] = {s: len(c) for s, c in data.items()} | {"problems": problems}
        result["duration_sec"] = round(time.monotonic() - t0, 1)
        with session_scope() as s:
            r = s.get(BacktestRun, run_id)
            r.status, r.result, r.finished_at = "BITTI", _jsonable(result), datetime.now(timezone.utc)
        log.info("Backtest #%s bitti: %s işlem, net %s, %ss", run_id, result["summary"]["trades"],
                 result["summary"]["net_profit"], result["duration_sec"])
    except Exception as exc:  # noqa: BLE001
        log.exception("Backtest #%s başarısız", run_id)
        with session_scope() as s:
            r = s.get(BacktestRun, run_id)
            r.status, r.error, r.finished_at = "HATA", str(exc)[:1000], datetime.now(timezone.utc)


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if hasattr(x, "is_finite"):  # Decimal
        return str(x)
    return x
