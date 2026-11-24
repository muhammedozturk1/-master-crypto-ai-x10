"""Footprint stratejisi (BOT01-B), hacim profili ve websocket footprint toplayıcısı."""

import math
from decimal import Decimal as D

from app.bots.base import MarketContext
from app.bots.bot01_technical import STRATEGY, TechnicalBot
from app.data.candles import Candle
from app.domain.signals import Action
from app.engines.footprint import FootprintCollector, nice_step
from app.indicators.core import volume_profile

T0 = 1_700_000_000_000
STEP = 900_000


def bar(i, o, h, l, c, v):
    return Candle(T0 + i * STEP, D(f"{o:.6f}"), D(f"{h:.6f}"), D(f"{l:.6f}"), D(f"{c:.6f}"), D(f"{v:.4f}"), D("0"), "15")


def ranging(n=239):
    """Yatay piyasa: 100 etrafında salınım, hacim POC çevresinde yoğun."""
    out = []
    for k in range(n):
        c = 100 + 1.2 * math.sin(k / 3.0) + 0.3 * math.sin(k / 1.3)
        o = 100 + 1.2 * math.sin((k - 1) / 3.0) + 0.3 * math.sin((k - 1) / 1.3)
        out.append(bar(k, o, max(o, c) + 0.15, min(o, c) - 0.15, c, 100))
    return out


def ctx_with(last, extra=None):
    cs = ranging() + [last]
    return MarketContext(symbol="TESTUSDT", timeframe="15m", candles=cs, spread_pct=0.01, extra=extra or {})


def test_volume_profile_poc_and_value_area():
    # 100'de yoğun hacim, kenarlarda ince
    H = [100.5] * 30 + [104.0] * 3 + [97.5] * 3
    L = [99.5] * 30 + [103.0] * 3 + [96.5] * 3
    V = [100.0] * 30 + [5.0] * 6
    poc, vah, val = volume_profile(H, L, V, 0, len(H))
    assert 99.5 <= poc <= 100.5
    assert val < poc < vah
    assert vah <= 101 and val >= 99            # ince kenarlar değer alanının dışında
    assert volume_profile(H, L, V, 0, 5) is None


def test_volume_profile_ignores_current_bar():
    cs = ranging()
    H = [float(c.high) for c in cs]; L = [float(c.low) for c in cs]; V = [float(c.volume) for c in cs]
    a = volume_profile(H, L, V, len(H) - 96, len(H) - 1)
    H2, L2, V2 = H[:-1] + [500.0], L[:-1] + [1.0], V[:-1] + [1e9]
    assert volume_profile(H2, L2, V2, len(H) - 96, len(H) - 1) == a


def _profile():
    cs = ranging()
    H = [float(c.high) for c in cs]; L = [float(c.low) for c in cs]; V = [float(c.volume) for c in cs]
    return volume_profile(H, L, V, len(cs) - 96, len(cs))


def test_estimated_absorption_at_val_gives_long_to_poc():
    poc, vah, val = _profile()
    low = val - 0.15
    last = bar(239, val + 0.05, val + 0.20, low, val + 0.12, 400)   # hacimli, uzun alt fitil, VAL üstü kapanış
    sigs = [s for s in TechnicalBot().analyze(ctx_with(last)) if s.strategy_id.startswith(STRATEGY)]
    assert len(sigs) == 1
    s = sigs[0]
    assert s.action == Action.AL
    assert s.details["footprint_source"] == "tahmini"
    assert s.details["absorption"] is True
    assert float(s.stop_loss) < low
    assert any("footprint tahmini" in w for w in s.risk_warnings)
    assert abs(s.details["val"] - val) < 1e-9 and abs(s.details["poc"] - poc) < 1e-9


def test_real_footprint_absorption_and_exhaustion():
    poc, vah, val = _profile()
    low, high = val - 0.15, val + 0.20
    levels = [(low, 1.0, 1.0),                    # en uç seviye: neredeyse hiç işlem yok → tükenme
              (low + 0.05, 50.0, 400.0),          # dip bölgesinde agresif satış emiliyor
              (low + 0.10, 60.0, 300.0),
              (low + 0.20, 300.0, 100.0), (low + 0.25, 250.0, 90.0), (high, 200.0, 80.0)]
    buy = sum(x[1] for x in levels); sell = sum(x[2] for x in levels)
    fp = {"source": "gercek", "levels": levels, "buy": buy, "sell": sell, "delta": buy - sell, "volume": buy + sell}
    last = bar(239, val + 0.05, high, low, val + 0.12, 120)          # hacim artışı yok: tahmini emilim tetiklenmez
    s = [x for x in TechnicalBot().analyze(ctx_with(last, {"footprint": fp})) if x.strategy_id.startswith(STRATEGY)]
    assert len(s) == 1
    d = s[0].details
    assert d["footprint_source"] == "gercek" and d["absorption"] and d["exhaustion"]
    assert "+emilim" in s[0].strategy_id and "+tukenme" in s[0].strategy_id
    assert not any("footprint tahmini" in w for w in s[0].risk_warnings)


def test_real_footprint_without_absorption_gives_no_signal():
    poc, vah, val = _profile()
    low, high = val - 0.15, val + 0.20
    # Dipte alıcılar agresif (emilim değil), uç seviyede bol hacim (tükenme değil)
    levels = [(low, 300.0, 100.0), (low + 0.05, 300.0, 100.0), (low + 0.2, 200.0, 200.0), (high, 100.0, 100.0)]
    fp = {"source": "gercek", "levels": levels, "buy": 900.0, "sell": 500.0, "delta": 400.0, "volume": 1400.0}
    last = bar(239, val + 0.05, high, low, val + 0.12, 400)
    s = [x for x in TechnicalBot().analyze(ctx_with(last, {"footprint": fp})) if x.strategy_id.startswith(STRATEGY)]
    assert s == []


def test_no_signal_in_middle_of_value_area():
    poc, vah, val = _profile()
    last = bar(239, poc - 0.1, poc + 0.2, poc - 0.6, poc + 0.1, 400)
    assert not [s for s in TechnicalBot().analyze(ctx_with(last)) if s.strategy_id.startswith(STRATEGY)]


# --- Toplayıcı ---------------------------------------------------------------

def msg(sym, trades):
    return {"topic": f"publicTrade.{sym}", "data": [{"T": t, "s": sym, "S": side, "p": str(p), "v": str(v)} for t, side, p, v in trades]}


def test_nice_step():
    assert nice_step(100_000) == 50
    assert nice_step(2.0) == 0.001
    assert math.isclose(nice_step(0.5), 0.0002)


def test_collector_aggregates_bar_only_when_fully_covered():
    fc = FootprintCollector()
    start = 1_700_000_100_000 - 1_700_000_100_000 % 900_000
    # dinleme mum başlamadan önce başladı
    fc.on_message(msg("BTCUSDT", [(start - 30_000, "Buy", 100_000, 0.1)]), now_ms=start - 30_000)
    fc.on_message(msg("BTCUSDT", [(start + 1_000, "Sell", 100_010, 0.5), (start + 61_000, "Buy", 100_010, 0.2),
                                  (start + 120_000, "Buy", 100_120, 1.0)]), now_ms=start + 120_000)
    b = fc.bar("BTCUSDT", start, start + 900_000)
    assert b["source"] == "gercek"
    assert math.isclose(b["sell"], 0.5 * 100_010) and math.isclose(b["buy"], 0.2 * 100_010 + 100_120)
    assert [round(x[0]) for x in b["levels"]] == [100_000, 100_100]      # 50'lik seviyeler
    # mumun öncesindeki işlem dahil değil
    assert b["volume"] < 0.5 * 100_010 + 0.2 * 100_010 + 100_120 + 1
    # bağlantı koptu → kapsama kayboldu
    fc.reset_coverage()
    assert fc.bar("BTCUSDT", start, start + 900_000) is None


def test_collector_rejects_partially_heard_bar():
    fc = FootprintCollector()
    start = 1_700_000_000_000 - 1_700_000_000_000 % 900_000
    fc.on_message(msg("ETHUSDT", [(start + 300_000, "Buy", 3000, 1)]), now_ms=start + 300_000)
    assert fc.bar("ETHUSDT", start, start + 900_000) is None              # dinleme mum ortasında başladı
    assert fc.bar("ETHUSDT", start + 900_000, start + 1_800_000) is None  # veri yok
    fc.on_message({"topic": "tickers.X"}, now_ms=start)                   # ilgisiz mesaj yok sayılır
    fc.on_message(msg("ETHUSDT", [(start + 301_000, "Buy", "bozuk", 1)]), now_ms=start + 301_000)
    assert fc.trades_seen == 1


def test_collector_prunes_old_minutes():
    fc = FootprintCollector()
    t = 1_700_000_000_000
    fc.on_message(msg("SOLUSDT", [(t, "Buy", 150, 1)]), now_ms=t)
    fc.on_message(msg("SOLUSDT", [(t + 300 * 60_000, "Buy", 150, 1)]), now_ms=t + 300 * 60_000)
    assert min(fc.minutes["SOLUSDT"]) >= t + 100 * 60_000
