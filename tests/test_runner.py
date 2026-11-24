import asyncio
import time

import pytest
from sqlalchemy import create_engine, inspect, text

from app.config import Settings
from app.database import session as dbs
from app.engines.bot_runner import BotRunner
from app.engines.scanner_service import scan_once
from tests.fake_bybit import FakeBybit

STEP15 = 15 * 60_000


def nr7_breakout_rows(symbol, interval, limit):
    """BTCUSDT 15m: son kapanmış mumda yukarı NR7 kırılımı. Diğerleri: varsayılan sahte veri."""
    if symbol != "BTCUSDT" or interval != "15":
        return None
    now = int(time.time() * 1000)
    forming = now - (now % STEP15)
    n = limit - 1
    bars = []
    for k in range(n):  # k=0 en eski kapanmış
        start = forming - (n - k) * STEP15
        if k == n - 2:
            o, h, l, c, v = 100, 100.25, 99.75, 100.0, 10
        elif k == n - 1:
            o, h, l, c, v = 100.1, 100.7, 100.0, 100.6, 20
        else:
            o, h, l, c, v = 100, 101, 99, 100, 10
        bars.append([str(start), str(o), str(h), str(l), str(c), str(v), "0"])
    bars.append([str(forming), "100.6", "100.7", "100.5", "100.6", "1", "0"])  # oluşan mum
    return list(reversed(bars))


@pytest.fixture
def env(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/r.db",
                        scanner_enabled=False, runner_enabled=False, runner_timeframes=["15m"])
    dbs.configure(settings.sqlalchemy_url)
    dbs.init_db()
    return settings


def test_runner_records_nr7_signal_once(env):
    async def go():
        market = FakeBybit(kline_rows=nr7_breakout_rows).client()
        await scan_once(market, env)
        runner = BotRunner(market, env)
        r1 = await runner.run_timeframe("15m")
        r2 = await runner.run_timeframe("15m")  # aynı mum tekrar işlenirse
        await market.close()
        return r1, r2
    r1, r2 = asyncio.run(go())
    assert r1.symbols == 3 and r1.analyzed == 3 and not r1.data_problems
    assert r1.signals_trade == 1 and r1.signals_new == 1
    assert r2.signals_new == 0  # tekrar kaydedilmedi
    from app.database.models import BotState, SignalRecord
    with dbs.session_scope() as s:
        recs = s.query(SignalRecord).all()
        assert len(recs) == 1 and recs[0].symbol == "BTCUSDT" and recs[0].action == "AL"
        st = s.get(BotState, "BOT10")
        assert st.signals_total == 1 and st.status == "calisiyor" and st.last_analysis_at is not None


def test_disabled_bot_does_not_run(env):
    from app.database.models import BotState
    with dbs.session_scope() as s:
        s.get(BotState, "BOT10").enabled = False
    async def go():
        market = FakeBybit(kline_rows=nr7_breakout_rows).client()
        await scan_once(market, env)
        r = await BotRunner(market, env).run_timeframe("15m")
        await market.close()
        return r
    assert asyncio.run(go()).signals_new == 0


def test_due_timeframes_waits_for_close_delay(env):
    runner = BotRunner(None, env)
    boundary = 1_700_000_100_000 - (1_700_000_100_000 % STEP15)
    assert runner.due_timeframes(boundary + 2_000) == []          # mum yeni kapandı, bekle
    assert runner.due_timeframes(boundary + 9_000) == ["15m"]     # gecikme doldu
    assert runner.due_timeframes(boundary + 60_000) == []         # aynı mum ikinci kez işlenmez


def test_migration_adds_dedup_column(tmp_path):
    url = f"sqlite:///{tmp_path}/old.db"
    eng = create_engine(url)
    with eng.begin() as c:  # Aşama 1-2'deki eski tablo yapısı
        c.execute(text("CREATE TABLE signals (id INTEGER PRIMARY KEY, bot_id VARCHAR(8))"))
    dbs.configure(url)
    dbs.init_db()
    cols = {c["name"] for c in inspect(dbs.get_engine()).get_columns("signals")}
    assert "dedup_key" in cols


def test_signals_and_nr7_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.main import app
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/e.db", scanner_enabled=False,
                        runner_enabled=False, runner_timeframes=["15m"])
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.market = FakeBybit(kline_rows=nr7_breakout_rows).client()
    with TestClient(app) as c:
        c.portal.call(scan_once, app.state.market, settings)
        runner = BotRunner(app.state.market, settings)
        c.portal.call(runner.run_timeframe, "15m")
        sigs = c.get("/signals").json()
        assert len(sigs) == 1 and sigs[0]["bot_id"] == "BOT10" and sigs[0]["action"] == "AL"
        nr7 = c.get("/nr7").json()
        assert nr7[0]["symbol"] == "BTCUSDT" and nr7[0]["status"] == "KIRILIM" and nr7[0]["nr7_high"] == 100.25
        bots = {b["bot_id"]: b for b in c.get("/bots").json()}
        assert bots["BOT10"]["implemented"] is True and bots["BOT01"]["implemented"] is False
    app.dependency_overrides.clear()
