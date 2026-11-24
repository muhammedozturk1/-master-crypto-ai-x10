import random
import time
from decimal import Decimal as D

import pytest

from app.backtesting.engine import Backtester, BacktestParams, _Pending
from app.backtesting.metrics import Trade, max_drawdown, monte_carlo, summarize
from app.data.candles import Candle

STEP = 900_000
T0 = 1_760_000_000_000 - (1_760_000_000_000 % STEP)


def walk(seed, n=700, drift=0.0):
    random.seed(seed)
    c, out = [100.0], []
    for _ in range(n - 1):
        c.append(c[-1] * (1 + drift + random.gauss(0, 0.008)))
    for k in range(n):
        o, cl = (c[k - 1] if k else c[0]), c[k]
        h = max(o, cl) * (1 + abs(random.gauss(0, 0.003)))
        l = min(o, cl) * (1 - abs(random.gauss(0, 0.003)))
        out.append(Candle(T0 + k * STEP, D(f"{o:.6f}"), D(f"{h:.6f}"), D(f"{l:.6f}"), D(f"{cl:.6f}"),
                          D(f"{random.uniform(5, 20):.3f}"), D(0), "15"))
    return out


def htf_from(cs):
    """15m → 1h birleştirme (test için)."""
    out = []
    for k in range(0, len(cs) - 3, 4):
        g = cs[k:k + 4]
        out.append(Candle(g[0].start_ms, g[0].open, max(x.high for x in g), min(x.low for x in g), g[-1].close,
                          sum((x.volume for x in g), D(0)), D(0), "60"))
    return out


@pytest.fixture(scope="module")
def data():
    d = {"AAAUSDT": walk(1, drift=0.0008), "BBBUSDT": walk(2, drift=-0.0008)}
    return d, {s: htf_from(cs) for s, cs in d.items()}, walk(3)


def test_strategy_mode_runs_and_reports(data):
    d, h, btc = data
    t0 = time.monotonic()
    r = Backtester(d, h, btc, BacktestParams()).run()
    assert time.monotonic() - t0 < 60
    s = r["summary"]
    assert s["trades"] > 0 and r["by_strategy"] and r["by_bot"]
    for k in ("net_profit", "profit_factor", "win_rate_pct", "expectancy_r", "max_drawdown_pct", "fees_paid",
              "avg_duration_min", "sharpe_daily_ann"):
        assert k in s
    assert len(r["walk_forward"]) == 4 and r["monte_carlo"]["runs"] == 500


def test_no_lookahead_truncation_invariance(data):
    """Veri kesilince, kesimden önce kapanan işlemler değişmemeli (gelecek verisi kullanılmıyor)."""
    d, h, btc = data
    full = Backtester(d, h, btc, BacktestParams()).run
    cut_ms = T0 + 550 * STEP
    dc = {s: [c for c in cs if c.start_ms < cut_ms] for s, cs in d.items()}
    hc = {s: [c for c in cs if c.start_ms < cut_ms] for s, cs in h.items()}
    bt_full, bt_cut = Backtester(d, h, btc, BacktestParams()), Backtester(dc, hc, [c for c in btc if c.start_ms < cut_ms], BacktestParams())
    bt_full.run(); bt_cut.run()
    key = lambda t: (t.symbol, t.strategy, t.entry_ms, t.exit_ms, round(t.entry, 8), round(t.exit, 8))  # noqa: E731
    before_full = sorted(key(t) for t in bt_full.trades if t.exit_ms < cut_ms - STEP and t.exit_reason != "donem_sonu")
    before_cut = sorted(key(t) for t in bt_cut.trades if t.exit_ms < cut_ms - STEP and t.exit_reason != "donem_sonu")
    assert before_full and before_full == before_cut


def bars(spec):
    return [Candle(T0 + k * STEP, D(o), D(h), D(l), D(c), D(10), D(0), "15") for k, (o, h, l, c) in enumerate(spec)]


def run_manual(spec, long=True, stop=98.0, tp=104.0, lock=False):
    cs = bars(spec)
    bt = Backtester({"X": cs}, None, None, BacktestParams(profit_lock=lock, half_spread_pct=0, slippage_pct=0))
    bt.pending.append(_Pending("X", "nr7_breakout", "BOT10", long, stop, tp, None, 1.0))
    for i, c in enumerate(cs):
        bt._fill_pending("X", c)
        bt._process_bar("X", cs, i)
    return bt


def test_entry_next_open_and_stop_first_same_bar():
    bt = run_manual([("100", "105", "97", "101")])            # hem hedef hem stop aynı mumda
    [t] = bt.trades
    assert t.entry == 100 and t.exit == 98 and t.exit_reason == "stop"


def test_gap_through_stop_exits_at_open():
    bt = run_manual([("100", "100.5", "99.5", "100"), ("96", "96.5", "95", "96")])
    assert bt.trades[0].exit == 96


def test_target_and_fees():
    bt = run_manual([("100", "101", "99.5", "100.5"), ("101", "104.5", "100.8", "104")])
    [t] = bt.trades
    assert t.exit == 104 and t.exit_reason == "hedef"
    assert t.net == pytest.approx((104 - 100) * t.qty - t.qty * 100 * 0.00055 - t.qty * 104 * 0.00055)


def test_profit_lock_applies_from_next_bar_only():
    spec = [("100", "100.5", "99.6", "100"), ("100", "103.2", "100", "103"),   # kapanışta kilit yükselir
            ("103", "103.1", "100.5", "100.6")]                               # sonraki mumda kilitten çıkış
    with_lock, without = run_manual(spec, lock=True), run_manual(spec, lock=False)
    assert with_lock.trades and with_lock.trades[0].exit_reason == "kar_kilidi" and with_lock.trades[0].net > 0
    assert not without.trades  # kilitsiz pozisyon hâlâ açık (stop 98)


def test_system_mode_uses_risk_layer(data):
    d, h, btc = data
    r = Backtester(d, h, btc, BacktestParams(mode="sistem")).run()
    assert r["decisions"] is not None
    assert r["summary"]["max_drawdown_pct"] <= 100
    # Sistem modunda aynı anda en fazla 2 pozisyon ve kasa 200 USD'den başlar
    assert r["params"]["mode"] == "sistem"


def test_metrics_helpers():
    assert max_drawdown([100, 120, 90, 130])[1] == pytest.approx(25.0)
    tr = [Trade("X", "s", "B", True, 0, 60_000, 1, 1, 1, 0.01, x, 1.0, max(x, 0), "stop") for x in [1, -1, 2, -0.5] * 3]
    s = summarize(tr, [(0, 200.0), (86_400_000, 201.0), (172_800_000, 205.5)], 200)
    assert s["trades"] == 12 and s["win_rate_pct"] == 50 and s["profit_factor"] == pytest.approx(2.0)
    assert monte_carlo(tr, 200)["max_dd_pct_p95"] >= monte_carlo(tr, 200)["max_dd_pct_p50"]


# --- Servis, geçmiş veri ve uçlar -------------------------------------------

def test_klines_range_paginates_without_gaps():
    import asyncio
    from tests.fake_bybit import FakeBybit
    fake = FakeBybit()
    async def go():
        c = fake.client()
        end = int(time.time() * 1000)
        r = await c.klines_range("BTCUSDT", "15", end - 30 * 86_400_000, end)   # 2880 mum → 3 sayfa
        await c.close()
        return r
    cs = asyncio.run(go())
    assert len(cs) >= 2880 and all(b.start_ms - a.start_ms == STEP for a, b in zip(cs, cs[1:]))
    assert fake.calls.count("/v5/market/kline") == 3


def test_suggest_weights_needs_enough_trades():
    from app.backtesting.service import suggest_weights
    w = suggest_weights({"BOT10": {"trades": 12, "expectancy_r": 0.8},
                         "BOT02": {"trades": 40, "expectancy_r": 0.4}, "BOT01": {"trades": 50, "expectancy_r": -0.5}})
    assert w["BOT10"]["weight"] == 1.0 and "yetersiz" in w["BOT10"]["note"]
    assert 1.0 < w["BOT02"]["weight"] <= 1.5 and 0.5 <= w["BOT01"]["weight"] < 1.0


def test_backtest_api_end_to_end(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import Settings, get_settings
    from app.engines.scanner_service import scan_once
    from app.main import app
    from tests.fake_bybit import FakeBybit
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/b.db", dashboard_token="tok",
                        scanner_enabled=False, runner_enabled=False, calendar_enabled=False,
                        arbitrage_enabled=False, execution_enabled=False)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.market = FakeBybit().client()
    h = {"X-Dashboard-Token": "tok"}
    with TestClient(app, headers=h) as c:
        c.portal.call(scan_once, app.state.market, settings)
        assert c.post("/backtest", json={"days": 3}, headers={"X-Dashboard-Token": "x"}).status_code == 401
        r = c.post("/backtest", json={"days": 3, "symbols": ["BTCUSDT", "ETHUSDT"]}, headers=h).json()
        rid = r["id"]
        c.portal.call(lambda: app.state.backtest_task)
        for _ in range(100):
            res = c.get(f"/backtest/{rid}").json()
            if res["status"] != "CALISIYOR":
                break
            time.sleep(0.1)
        assert res["status"] == "BITTI", res.get("error")
        out = res["result"]
        assert "profit_lock_comparison" in out and "suggested_weights" in out and out["data"]["BTCUSDT"] > 0
        assert c.post(f"/backtest/{rid}/apply-weights", json={"confirm": False}, headers=h).status_code == 400
        assert c.post(f"/backtest/{rid}/apply-weights", json={"confirm": True}, headers=h).status_code == 200
        assert c.get("/performance").json()["summary"]["trades"] == 0
    app.dependency_overrides.clear()


def test_scalping_auto_passive_on_negative_expectancy(tmp_path):
    from datetime import datetime, timedelta, timezone
    from app.database import session as dbs
    from app.database.models import BotState, Decision, Position
    from app.engines.performance import learning_check
    dbs.configure(f"sqlite:///{tmp_path}/l.db")
    dbs.init_db()
    now = datetime.now(timezone.utc)
    with dbs.session_scope() as s:
        s.add(Decision(id=1, symbol="X", outcome="ONAYLANDI", score=80, reason="t", contributions={},
                       plan={"bot_id": "BOT03"}))
        for k in range(20):
            s.add(Position(decision_id=1, mode="PAPER", venue="SIM", symbol="XUSDT", side="LONG", strategy_id="ema9_21_scalp",
                           qty=D(1), entry_price=D(100), initial_stop=D(99), current_stop=D(99), status="KAPALI",
                           realized_net_pnl=D("-0.2") if k % 3 else D("0.25"), last_price=D(100),
                           opened_at=now - timedelta(hours=k + 1), closed_at=now - timedelta(hours=k)))
    with dbs.session_scope() as s:
        acts = learning_check(s, "PAPER")
    assert acts and "BOT03 pasife" in acts[0]
    with dbs.session_scope() as s:
        assert s.get(BotState, "BOT03").enabled is False
