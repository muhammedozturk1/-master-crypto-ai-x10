import asyncio
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.data.candles import Candle, DataQualityError, closed_only, now_ms, validate_candles
from app.engines.market_scanner import ScanConfig, run_scan
from app.exchange.bybit_public import BybitError
from tests.fake_bybit import FakeBybit

STEP = 15 * 60_000


def candles(n, start=None, gap_at=None):
    t = now_ms()
    first = (start if start is not None else t - (t % STEP) - n * STEP)
    out, s = [], first
    for k in range(n):
        if gap_at == k:
            s += STEP
        out.append(Candle(s, D(100), D(101), D(99), D(100), D(1), D(100), "15"))
        s += STEP
    return out


def run(coro):
    return asyncio.run(coro)


# --- Mum verisi -----------------------------------------------------------

def test_forming_candle_is_dropped():
    fake = FakeBybit()
    async def go():
        c = fake.client()
        raw = await c.klines("BTCUSDT", "15", 50)
        await c.close()
        return raw
    raw = run(go())
    assert [x.start_ms for x in raw] == sorted(x.start_ms for x in raw)  # eskiden yeniye
    closed = closed_only(raw)
    assert len(closed) == len(raw) - 1
    validate_candles(closed)


def test_gap_detected():
    with pytest.raises(DataQualityError, match="boşluğu"):
        validate_candles(candles(10, gap_at=5))


def test_stale_detected():
    old_start = now_ms() - (now_ms() % STEP) - 40 * STEP
    with pytest.raises(DataQualityError, match="bayat"):
        validate_candles(candles(10, start=old_start))


def test_bad_ohlc_detected():
    bad = candles(3)
    bad[1] = Candle(bad[1].start_ms, D(100), D(99), D(101), D(100), D(1), D(1), "15")
    with pytest.raises(DataQualityError, match="OHLC"):
        validate_candles(bad)


# --- Bybit istemcisi ------------------------------------------------------

def test_retry_then_success():
    fake = FakeBybit(fail_first=2)
    async def go():
        c = fake.client()
        r = await c.klines("BTCUSDT", "15", 5)
        await c.close()
        return r
    assert len(run(go())) == 5
    assert len(fake.calls) == 3


def test_ret_code_error_raises():
    fake = FakeBybit(ret_code=10001)
    async def go():
        c = fake.client()
        try:
            await c.tickers()
        finally:
            await c.close()
    with pytest.raises(BybitError, match="10001"):
        run(go())


def test_orderbook_depth():
    fake = FakeBybit()
    async def go():
        c = fake.client()
        ob = await c.orderbook("BTCUSDT")
        await c.close()
        return ob
    ob = run(go())
    bid_usd, ask_usd = ob.depth_usd(D("0.5"))
    assert bid_usd == D("999.0")   # yalnızca 99.9 bandın içinde
    assert ask_usd == D("800.8")   # yalnızca 100.1 bandın içinde


# --- Coin tarayıcı --------------------------------------------------------

def test_scanner_filters_and_ranks():
    fake = FakeBybit()
    async def go():
        c = fake.client()
        r = await run_scan(c, ScanConfig())
        await c.close()
        return r
    r = run(go())
    assert [x.symbol for x in r.candidates] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert r.rejected == {"dusuk_hacim": 1, "yuksek_spread": 1, "yeni_listeleme": 1, "islem_kapali": 1}


def test_scanner_top_n_and_blacklist():
    fake = FakeBybit()
    async def go():
        c = fake.client()
        r = await run_scan(c, ScanConfig(top_n=1, blacklist=("BTCUSDT",)))
        await c.close()
        return r
    r = run(go())
    assert [x.symbol for x in r.candidates] == ["ETHUSDT"]
    assert r.rejected["kara_liste"] == 1 and r.rejected["ilk_n_disi"] == 1


# --- API -----------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    from app.main import app
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/m.db", scanner_enabled=False, runner_enabled=False)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.market = FakeBybit().client()
    with TestClient(app) as c:
        yield c, app, settings
    app.dependency_overrides.clear()


def test_candles_endpoint_closed_only(api):
    c, _, _ = api
    r = c.get("/market/candles/btcusdt", params={"timeframe": "15m", "limit": 20}).json()
    assert r["symbol"] == "BTCUSDT" and r["data_quality"] == "IYI"
    assert r["dropped_forming"] == 1 and len(r["candles"]) == 20
    assert c.get("/market/candles/BTCUSDT", params={"timeframe": "7m"}).status_code == 400


def test_scan_endpoint(api):
    from app.engines.scanner_service import scan_once
    c, app, settings = api
    assert c.get("/market/scan").json() == {"status": "henuz_tarama_yok"}
    c.portal.call(scan_once, app.state.market, settings)
    r = c.get("/market/scan").json()
    assert r["last_run_ok"] is True and r["selected"] == 3
    assert r["candidates"][0]["symbol"] == "BTCUSDT"
