"""BOT03, BOT04, BOT05, BOT06 ve ilgili servislerin testleri."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import httpx
import pytest

from app.bots.base import MarketContext
from app.bots.bot03_scalping import ScalpingBot
from app.bots.bot04_arbitrage import ArbitrageBot
from app.bots.bot05_news import NewsBot
from app.bots.bot06_flow import FlowBot, crowding_adjustment, flow_metrics
from app.data.candles import Candle
from app.domain.signals import Action
from app.engines.arbitrage_service import ArbState, ArbitrageService, cross_exchange, triangular
from app.engines.calendar_service import CalendarService, CalendarState, EconEvent, fng_sentiment, parse_events, veto_status
from app.exchange.bybit_public import OrderBook
from tests.fake_bybit import FakeBybit

T0 = 1_700_000_000_000


def bar(i, o, h, l, c, v=10.0, step=900_000, interval="15"):
    return Candle(T0 + i * step, D(str(o)), D(str(h)), D(str(l)), D(str(c)), D(str(v)), D("0"), interval)


# --- BOT06 ----------------------------------------------------------------

def test_flow_metrics_and_missing_data():
    assert flow_metrics({}, [1.0])["funding"] is None
    extra = {
        "funding": [(1, D("0.0006"))],
        "open_interest": [(k, D(1000 + 10 * k)) for k in range(20)],
        "account_ratio": [(1, D("0.72"))],
        "trades": [(1, "Buy", D(100), D(3)), (2, "Sell", D(100), D(1))],
        "orderbook": OrderBook("X", [(D("99.9"), D(30))], [(D("100.1"), D(10))], 0),
    }
    m = flow_metrics(extra, [1.0])
    assert m["funding"] == 0.0006 and m["long_ratio"] == 0.72
    assert m["taker_buy_ratio"] == pytest.approx(0.75) and m["cvd"] == pytest.approx(200)
    assert m["oi_change_pct"] == pytest.approx((1190 - 1030) / 1030 * 100)
    assert m["book_imbalance"] > 1.5


def test_crowded_longs_penalize_long_signal():
    m = {"funding": 0.0006, "long_ratio": 0.75, "oi_change_pct": None, "taker_buy_ratio": None, "book_imbalance": None}
    tags, warns = [], []
    assert crowding_adjustment(True, m, 1.0, tags, warns) == -15
    assert any("funding yüksek" in w for w in warns)
    assert crowding_adjustment(False, m, -1.0, [], []) == 10


def flat_bars(n, base=100.0):
    return [bar(k, base, base + 0.5, base - 0.5, base) for k in range(n)]


def test_liquidity_sweep_long():
    cs = flat_bars(130)
    cs[115] = bar(115, 100, 100.5, 98.0, 100)   # belirgin dip (pivot)
    # son mum: dibi fitille aşıp yukarı kapanış, uzun alt fitil, yüksek hacim
    cs.append(bar(130, 99.9, 100.2, 97.6, 100.1, v=30))
    [s] = [x for x in FlowBot().analyze(MarketContext("XUSDT", "15m", cs)) if x.strategy_id.startswith("liquidity_sweep")]
    assert s.action == Action.AL and s.stop_loss < D("97.6")
    assert "on-chain veri yok" in s.risk_warnings


def test_manipulation_filter():
    cs = flat_bars(130)
    cs.append(bar(130, 100, 106, 99.9, 105.5, v=200))
    [s] = FlowBot().analyze(MarketContext("XUSDT", "15m", cs))
    assert s.action == Action.BEKLE and s.strategy_id == "manipulation_filter" and s.details["suspicious"]


# --- BOT03 ----------------------------------------------------------------

def scalp_ctx(cs, spread=0.01, htf_up=True):
    htf = [bar(k, 100 + k * 0.5, 101 + k * 0.5, 99 + k * 0.5, 100.4 + k * 0.5) for k in range(80)]
    if not htf_up:
        htf = [bar(k, 200 - k * 0.5, 201 - k * 0.5, 199 - k * 0.5, 199.6 - k * 0.5) for k in range(80)]
    return MarketContext("XUSDT", "5m", cs, htf_candles=htf, spread_pct=spread, taker_fee_pct=0.055)


def micro_setup():
    cs = [bar(k, 100, 101.5, 98.5, 100, v=10, step=300_000, interval="5") for k in range(120)]
    for k in range(108, 120):  # dar aralık, hacim azalarak
        cs[k] = bar(k, 100, 100.6, 99.6, 100.1, v=12 if k < 114 else 6, step=300_000, interval="5")
    cs.append(bar(120, 100.2, 101.4, 100.1, 101.3, v=25, step=300_000, interval="5"))
    return cs


def test_scalp_micro_breakout_requires_15m_direction():
    sigs = ScalpingBot().analyze(scalp_ctx(micro_setup()))
    assert "micro_breakout" in {s.strategy_id for s in sigs}
    assert all(s.action == Action.AL for s in sigs)
    assert ScalpingBot().analyze(scalp_ctx(micro_setup(), htf_up=False)) == []


def test_scalp_spread_and_cost_filters():
    assert ScalpingBot().analyze(scalp_ctx(micro_setup(), spread=0.08)) == []
    ctx = scalp_ctx(micro_setup())
    ctx.taker_fee_pct = 0.6  # maliyet beklenen hareketi aşıyor
    assert ScalpingBot().analyze(ctx) == []


# --- Takvim ve BOT05 -------------------------------------------------------

NOW = datetime(2026, 11, 25, 13, 20, tzinfo=timezone.utc)
FEED = [
    {"title": "Core PCE", "country": "USD", "date": "2026-11-25T08:30:00-05:00", "impact": "High"},
    {"title": "German Ifo", "country": "EUR", "date": "2026-11-25T09:00:00+00:00", "impact": "High"},
    {"title": "Jobless Claims", "country": "USD", "date": "2026-11-26T08:30:00-05:00", "impact": "Medium"},
    {"title": "bozuk", "country": "USD", "date": "yok", "impact": "High"},
]


def test_parse_and_veto_window():
    ev = parse_events(FEED)
    assert len(ev) == 3 and ev[1].time == datetime(2026, 11, 25, 13, 30, tzinfo=timezone.utc)
    assert veto_status(ev, NOW, 15, 15)["veto_active"] is True            # 10 dk önce
    assert veto_status(ev, NOW - timedelta(minutes=6), 15, 15)["veto_active"] is False  # 16 dk önce
    assert veto_status(ev, NOW + timedelta(minutes=24), 15, 15)["veto_active"] is True  # 14 dk sonra
    v = veto_status(ev, NOW + timedelta(minutes=26), 15, 15)
    assert v["veto_active"] is False and v["next_event"] is None  # orta etkili ve EUR olaylar sayılmaz


def test_news_bot_veto_and_sentiment():
    st = CalendarState(parse_events(FEED), 22, "Extreme Fear", NOW, {})
    cs = flat_bars(5)
    [s] = NewsBot().analyze(MarketContext("PIYASA", "15m", cs, extra={"calendar": st, "now": NOW}))
    assert s.strategy_id == "news_veto" and s.details["veto_active"] and s.symbol == "PIYASA"
    [s] = NewsBot().analyze(MarketContext("PIYASA", "15m", cs, extra={"calendar": st, "now": NOW - timedelta(hours=2)}))
    assert s.strategy_id == "sentiment" and s.details["sentiment"] == -56 and s.details["minutes_to_next"] == 130
    assert fng_sentiment(None) is None and fng_sentiment(100) == 100


def test_calendar_service_keeps_old_data_on_error():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        if calls["n"] > 2:
            return httpx.Response(500)
        if "fng" in str(req.url):
            return httpx.Response(200, json={"data": [{"value": "40", "value_classification": "Fear"}]})
        return httpx.Response(200, json=FEED)
    svc = CalendarService(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    async def go():
        a = await svc.refresh()
        b = await svc.refresh()
        await svc.close()
        return a, b
    a, b = asyncio.run(go())
    assert len(a.events) == 3 and a.fear_greed == 40 and not a.errors
    assert len(b.events) == 3 and b.fear_greed == 40 and set(b.errors) == {"takvim", "fear_greed"}


# --- Arbitraj ve BOT04 -----------------------------------------------------

def test_triangular_math():
    book = {"BTCUSDT": (100.0, 100.0), "ETHBTC": (0.05, 0.05), "ETHUSDT": (5.1, 5.1)}
    paths = {p["path"]: p["net_pct"] for p in triangular(book, ["ETH"], fee_pct=0.0)}
    assert paths["USDT→BTC→ETH→USDT"] == pytest.approx(2.0)     # 1/100/0.05*5.1 = 1.02
    assert paths["USDT→ETH→BTC→USDT"] == pytest.approx((1 / 5.1 * 0.05 * 100 - 1) * 100, abs=1e-4)
    with_fee = triangular(book, ["ETH"], fee_pct=0.1)[0]["net_pct"]
    assert with_fee == pytest.approx((1.02 * 0.999 ** 3 - 1) * 100, abs=1e-4)


def test_cross_exchange():
    books = {"bybit": {"BTCUSDT": (100.0, 100.1)}, "binance": {"BTCUSDT": (100.5, 100.6)}, "okx": {}}
    [r] = cross_exchange(books, ["BTCUSDT", "ETHUSDT"])
    assert r["buy"] == "bybit" and r["sell"] == "binance"
    assert r["gross_pct"] == pytest.approx((100.5 / 100.1 - 1) * 100, abs=1e-4)
    assert r["net_pct"] == pytest.approx(r["gross_pct"] - 0.2, abs=1e-4)


def test_arbitrage_service_with_partial_failures():
    def handler(req):
        if "binance" in str(req.url):
            return httpx.Response(200, json=[{"symbol": "BTCUSDT", "bidPrice": "60100", "askPrice": "60101"}])
        return httpx.Response(503)  # OKX erişilemiyor
    svc = ArbitrageService(bybit_spot=FakeBybit().client(), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                           symbols_provider=lambda: ["BTCUSDT", "ETHUSDT"])
    async def go():
        st = await svc.refresh()
        await svc.close()
        return st
    st = asyncio.run(go())
    assert "okx" in st.errors and "bybit" not in st.errors
    assert st.cross and st.cross[0]["symbol"] == "BTCUSDT" and st.cross[0]["sell"] == "binance"


def test_arbitrage_bot_observation_only():
    now = datetime.now(timezone.utc)
    cs = flat_bars(5)
    low = ArbState(triangular=[{"path": "x", "net_pct": -0.2}], checked_at=now)
    assert ArbitrageBot().analyze(MarketContext("PIYASA", "15m", cs, extra={"arbitrage": low})) == []
    high = ArbState(cross=[{"symbol": "BTCUSDT", "buy": "bybit", "sell": "binance", "net_pct": 0.3}], checked_at=now)
    [s] = ArbitrageBot().analyze(MarketContext("PIYASA", "15m", cs, extra={"arbitrage": high}))
    assert s.action == Action.BEKLE and "gözlem" in s.risk_warnings[0]


def test_calendar_and_arbitrage_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import Settings, get_settings
    from app.main import app
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/c.db", scanner_enabled=False,
                        runner_enabled=False, calendar_enabled=False, arbitrage_enabled=False)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.market = FakeBybit().client()
    with TestClient(app) as c:
        assert c.get("/calendar").json() == {"enabled": False}
        now = datetime.now(timezone.utc)
        cal = CalendarService(httpx.AsyncClient())
        cal.state = CalendarState([EconEvent("CPI", "USD", now + timedelta(minutes=5), "High")], 30, "Fear", now, {})
        app.state.calendar = cal
        r = c.get("/calendar").json()
        assert r["veto_active"] is True and r["sentiment"] == -40 and "haber akışı" in r["not_connected_sources"]
        arb = ArbitrageService(bybit_spot=FakeBybit().client(), http=httpx.AsyncClient())
        arb.state = ArbState(triangular=[{"path": "USDT→BTC→ETH→USDT", "net_pct": -0.31}], checked_at=now)
        app.state.arbitrage = arb
        r = c.get("/arbitrage").json()
        assert r["mode"] == "gozlem" and r["best"]["net_pct"] == -0.31
        app.state.calendar = app.state.arbitrage = None
    app.dependency_overrides.clear()
