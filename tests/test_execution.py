"""Aşama 6: BOT09 emir açma süreci, güvenlik kuralları, pozisyon izleme ve uzlaştırma."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal as D

import httpx
import pytest
from sqlalchemy import select

from app.bots.bot09_execution import ExecutionService
from app.config import Settings
from app.data.candles import Candle
from app.database import session as dbs
from app.database.models import AuditLog, Decision, Order, Position, SystemState
from app.engines.position_monitor import PositionMonitor
from app.exchange.bybit_private import sign
from app.execution.brokers import BybitBroker, SimBroker
from tests.fake_bybit import FakeBybit
from tests.fake_bybit_private import FakeBybitPrivate

PLAN = {"strategy": "nr7_breakout", "bot_id": "BOT10", "entry_low": "99.5", "entry_high": "100",
        "stop_loss": "98", "take_profit": "105", "estimated_cost_pct": "0.12", "qty": "0.5", "risk_usd": "1.06"}


@pytest.fixture
def settings(tmp_path):
    s = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/x.db", entry_order_type="market",
                 partial_tp_enabled=False)
    dbs.configure(s.sqlalchemy_url)
    dbs.init_db()
    with dbs.session_scope() as ss:
        for i in (1, 2, 3):
            ss.add(Decision(id=i, symbol="BTCUSDT", outcome="ONAYLANDI", score=80, reason="t", contributions={}))
    return s


def run(coro):
    return asyncio.run(coro)


def bybit_exec(settings, fake, price="100"):
    public = FakeBybit(kline_rows=lambda s, i, n: flat_rows(price, n)).client()
    broker = BybitBroker(fake.client(), public, "BYBIT_DEMO", D("0.055"))
    return ExecutionService(broker, settings, poll_attempts=2, poll_delay=0)


def flat_rows(price, n, start_ms=None):
    import time
    now = int(time.time() * 1000) if start_ms is None else start_ms
    step = 60_000
    base = now - now % step
    return [[str(base - k * step), price, price, price, price, "1", "0"] for k in range(n)]


def system():
    with dbs.session_scope() as s:
        st = s.get(SystemState, 1)
        return st.trading_enabled, st.safe_mode, st.safe_mode_reason


def test_signature_checked_by_server():
    import hashlib, hmac
    assert sign("s", "p") == hmac.new(b"s", b"p", hashlib.sha256).hexdigest()
    from app.exchange.bybit_private import BybitPrivate
    fake = FakeBybitPrivate()
    good = fake.client()
    bad = BybitPrivate("test-key", "yanlis", "https://api-demo.bybit.com",
                       client=httpx.AsyncClient(base_url="https://api-demo.bybit.com",
                                                transport=httpx.MockTransport(fake.handler)))
    run(good.wallet_balance())
    assert fake.bad_signatures == 0
    run(bad.wallet_balance())
    assert fake.bad_signatures == 1


def test_open_happy_path_with_exchange_side_stop(settings):
    fake = FakeBybitPrivate(fill_price="100.1")
    ex = bybit_exec(settings, fake, price="100")
    r = run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))
    assert r["status"] == "DOLDU" and fake.bad_signatures == 0
    create = next(p for path, p in fake.calls if path == "/v5/order/create")
    assert create["stopLoss"] == "98" and create["takeProfit"] == "105" and create["orderLinkId"] == "x10-1-E"
    assert create["slTriggerBy"] == "MarkPrice" and create["side"] == "Buy"
    with dbs.session_scope() as s:
        p = s.query(Position).one()
        assert p.venue == "BYBIT_DEMO" and p.entry_price == D("100.1") and p.current_stop == D("98")
        assert s.query(Order).one().status == "DOLDU"


def test_same_decision_never_sends_twice(settings):
    fake = FakeBybitPrivate()
    ex = bybit_exec(settings, fake)
    run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))
    r = run(ex.open_from_decision(1, "ETHUSDT", "LONG", PLAN))  # aynı karar kimliği
    assert r["status"] == "ATLANDI" and fake.count("/v5/order/create") == 1


def test_timeout_queries_status_instead_of_resending(settings):
    fake = FakeBybitPrivate(timeout_on_create=True)
    r = run(bybit_exec(settings, fake).open_from_decision(1, "BTCUSDT", "LONG", PLAN))
    assert fake.count("/v5/order/create") == 1          # tekrar gönderilmedi
    assert fake.count("/v5/order/realtime") >= 1         # durum sorgulandı
    assert r["status"] == "DOLDU"
    with dbs.session_scope() as s:
        assert s.query(AuditLog).filter_by(event="emir_zaman_asimi").count() == 1


def test_unverifiable_order_triggers_safe_mode(settings):
    fake = FakeBybitPrivate(timeout_on_create=True, lose_order=True)
    r = run(bybit_exec(settings, fake).open_from_decision(1, "BTCUSDT", "LONG", PLAN))
    assert r["status"] == "BILINMIYOR"
    enabled, safe, reason = system()
    assert not enabled and safe and "doğrulanamadı" in reason


def test_missing_stop_closes_position_and_safe_mode(settings):
    fake = FakeBybitPrivate(stop_supported=False, trading_stop_fails=True)
    r = run(bybit_exec(settings, fake).open_from_decision(1, "BTCUSDT", "LONG", PLAN))
    assert r["status"] == "KAPATILDI"
    assert "BTCUSDT" not in fake.positions               # reduce-only ile kapatıldı
    assert any(p.get("reduceOnly") for path, p in fake.calls if path == "/v5/order/create")
    assert system()[1] is True


def test_price_ran_away_no_order(settings):
    fake = FakeBybitPrivate()
    r = run(bybit_exec(settings, fake, price="101").open_from_decision(1, "BTCUSDT", "LONG", PLAN))
    assert r["status"] == "ATLANDI" and "kaçtı" in r["reason"] and fake.count("/v5/order/create") == 0


def test_safe_mode_blocks_new_orders(settings):
    with dbs.session_scope() as s:
        s.get(SystemState, 1).safe_mode = True
    fake = FakeBybitPrivate()
    r = run(bybit_exec(settings, fake).open_from_decision(1, "BTCUSDT", "LONG", PLAN))
    assert r["status"] == "ATLANDI" and fake.count("/v5/order/create") == 0


def test_monitor_records_exchange_stop_out_with_own_net_pnl(settings):
    fake = FakeBybitPrivate(fill_price="100")
    ex = bybit_exec(settings, fake)
    run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))
    mon = PositionMonitor(ex)
    run(mon.cycle())
    with dbs.session_scope() as s:
        assert s.query(Position).one().last_price == D("100")
    fake.stop_out("BTCUSDT", "98")
    rep = run(mon.cycle())
    assert rep["closed"] == 1
    with dbs.session_scope() as s:
        p = s.query(Position).one()
        gross = (D("98") - D("100")) * p.qty
        fees = p.qty * D("100") * D("0.00055") + p.qty * D("98") * D("0.00055")
        assert p.status == "KAPALI" and p.realized_net_pnl == pytest.approx(gross - fees, abs=D("0.000001"))


def test_reconcile_orphan_position_safe_mode(settings):
    fake = FakeBybitPrivate()
    fake.positions["SOLUSDT"] = {"symbol": "SOLUSDT", "side": "Buy", "size": "1", "avgPrice": "150",
                                 "markPrice": "150", "stopLoss": "", "takeProfit": ""}
    run(PositionMonitor(bybit_exec(settings, fake)).cycle())
    enabled, safe, reason = system()
    assert safe and "SOLUSDT" in reason


def test_manual_close(settings):
    fake = FakeBybitPrivate(fill_price="100")
    ex = bybit_exec(settings, fake)
    pid = run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))["position_id"]
    fake.fill_price = D("101")
    run(ex.close_position(pid, "elle"))
    run(PositionMonitor(ex).cycle())
    with dbs.session_scope() as s:
        p = s.get(Position, pid)
        assert p.status == "KAPALI" and p.realized_net_pnl > 0


# --- Simülasyon --------------------------------------------------------------

def sim_exec(settings, rows_fn):
    pub = FakeBybit(kline_rows=rows_fn).client()
    return ExecutionService(SimBroker(pub, D("0.055")), settings, poll_attempts=1, poll_delay=0)


def test_sim_open_uses_ask_and_fee(settings):
    ex = sim_exec(settings, lambda s, i, n: flat_rows("100", n))
    r = run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))   # sahte defterde ask 100.1
    assert r["status"] == "DOLDU" and r["price"] == "100.1"
    with dbs.session_scope() as s:
        p = s.query(Position).one()
        assert p.venue == "SIM" and p.entry_fee == p.qty * D("100.1") * D("0.00055")


def test_sim_stop_first_when_both_hit_and_gap_fill(settings):
    state = {"rows": None}
    ex = sim_exec(settings, lambda s, i, n: state["rows"] or flat_rows("100", n))
    pid = run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))["position_id"]
    import time
    t = int(time.time() * 1000) + 60_000
    # aynı mumda hem 106 (hedef) hem 97 (stop) → önce stop; açılış 99 > stop 98 → çıkış stoptan
    state["rows"] = [[str(t), "99", "106", "97", "100", "1", "0"]]
    run(PositionMonitor(ex).cycle())
    with dbs.session_scope() as s:
        p = s.get(Position, pid)
        assert p.status == "KAPALI" and p.last_price == D("98") and p.realized_net_pnl < 0


def test_sim_gap_below_stop_exits_at_open(settings):
    state = {"rows": None}
    ex = sim_exec(settings, lambda s, i, n: state["rows"] or flat_rows("100", n))
    pid = run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))["position_id"]
    import time
    t = int(time.time() * 1000) + 60_000
    state["rows"] = [[str(t), "96", "96.5", "95", "96", "1", "0"]]
    run(PositionMonitor(ex).cycle())
    with dbs.session_scope() as s:
        assert s.get(Position, pid).last_price == D("96")   # boşluk: stop değil açılış fiyatı


def test_signal_only_mode_never_executes(settings):
    from app.config import TradingMode
    settings.mode = TradingMode.SIGNAL_ONLY
    fake = FakeBybitPrivate()
    r = run(bybit_exec(settings, fake).open_from_decision(1, "BTCUSDT", "LONG", PLAN))
    assert r["status"] == "ATLANDI" and fake.count("/v5/order/create") == 0


def test_runner_executes_approved_decision_and_records_result(settings):
    from app.engines.bot_runner import BotRunner
    from app.master_robot.decision_engine import MarketInputs, decide
    from tests.test_master import ctx, sig
    fake = FakeBybitPrivate(fill_price="100")
    ex = bybit_exec(settings, fake)
    runner = BotRunner(None, settings, executor=ex)
    runner._instruments = {"XUSDT": {"qty_step": "0.001", "min_order_qty": "0.001", "min_notional": "5"}}
    d = decide(ctx("up"), [sig("BOT10", "nr7_breakout+ema", conf=80, vr=2.2), sig("BOT02", "trend_pullback", conf=85),
                           sig("BOT06", "liquidity_sweep", conf=75)], MarketInputs(sentiment=20))
    runner._risk_check(d)
    assert d.outcome == "ONAYLANDI"
    from app.engines.bot_runner import save_decision
    did = save_decision(d)
    runner.approved.append((did, d))
    [res] = run(runner._execute_approved())
    assert res["status"] == "DOLDU"
    with dbs.session_scope() as s:
        assert s.get(Decision, did).contributions["yurutme"]["status"] == "DOLDU"
        assert s.query(Position).filter_by(decision_id=did).one().symbol == "XUSDT"


# --- Aşama 7: kâr kilidinin izleyici üzerinden uygulanması -------------------------

def test_profit_lock_moves_exchange_stop_and_records_change(settings):
    from app.database.models import ProtectionChange
    fake = FakeBybitPrivate(fill_price="100")
    ex = bybit_exec(settings, fake)
    pid = run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))["position_id"]
    fake.positions["BTCUSDT"]["markPrice"] = "103.2"          # net ≈ %3 → kilit %2
    run(PositionMonitor(ex).cycle())
    stops = [p for path, p in fake.calls if path == "/v5/position/trading-stop"]
    assert stops and D(stops[-1]["stopLoss"]) > D("101.5")
    with dbs.session_scope() as s:
        p = s.get(Position, pid)
        assert p.current_stop == D(stops[-1]["stopLoss"]) and p.lock_state == 3
        ch = s.query(ProtectionChange).one()
        assert ch.old_stop == D("98") and ch.new_stop == p.current_stop
    # fiyat geri gelir → stop geri çekilmez
    fake.positions["BTCUSDT"]["markPrice"] = "102.2"
    run(PositionMonitor(ex).cycle())
    with dbs.session_scope() as s:
        assert s.get(Position, pid).current_stop == D(stops[-1]["stopLoss"])
        assert s.query(ProtectionChange).count() == 1


def test_profit_lock_exchange_failure_keeps_old_stop(settings):
    fake = FakeBybitPrivate(fill_price="100")
    ex = bybit_exec(settings, fake)
    pid = run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))["position_id"]
    fake.trading_stop_fails = True
    fake.positions["BTCUSDT"]["markPrice"] = "103.2"
    run(PositionMonitor(ex).cycle())
    with dbs.session_scope() as s:
        p = s.get(Position, pid)
        assert p.current_stop == D("98") and p.lock_state == 6   # ERROR_RECOVERY, borsadaki stop korunur
        assert s.query(AuditLog).filter_by(event="kar_kilidi_hatasi").count() == 1


def test_sim_profit_locked_then_exit_with_profit(settings):
    import time
    state = {"rows": None}
    ex = sim_exec(settings, lambda s, i, n: state["rows"] or flat_rows("100", n))
    pid = run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))["position_id"]   # giriş 100.1
    mon = PositionMonitor(ex)
    t = int(time.time() * 1000) + 60_000
    state["rows"] = [[str(t), "103", "103.5", "102.9", "103.4", "1", "0"]]   # net ≈ %3
    run(mon.cycle())
    with dbs.session_scope() as s:
        locked = s.get(Position, pid).current_stop
    assert locked > D("101")
    state["rows"] = [[str(t + 60_000), "103", "103.1", "100.5", "100.8", "1", "0"]]  # sert geri dönüş
    run(mon.cycle())
    with dbs.session_scope() as s:
        p = s.get(Position, pid)
        assert p.status == "KAPALI" and p.realized_net_pnl > 0 and p.last_price == locked and p.lock_state == 5


# --- Limit (maker) giriş --------------------------------------------------------

def limit_exec(settings, fake, bid="99.9", ask="100"):
    settings.entry_order_type = "limit"
    ob = {"bids": [[bid, "5"]], "asks": [[ask, "5"]]}
    public = FakeBybit(kline_rows=lambda s, i, n: flat_rows(ask, n), orderbook=ob).client()
    broker = BybitBroker(fake.client(), public, "BYBIT_DEMO", D("0.055"))
    return ExecutionService(broker, settings, poll_attempts=2, poll_delay=0, limit_poll_sec=0.01)


def test_limit_entry_posts_maker_at_plan_price_and_records_fill(settings):
    fake = FakeBybitPrivate()
    ex = limit_exec(settings, fake)

    async def go():
        r = await ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN, D("0.001"), D("0.1"), "15m")
        assert r["status"] == "BEKLIYOR" and D(r["price"]) == D("99.9")   # plan 100, en iyi alış 99.9 → 99.9
        create = [p for path, p in fake.calls if path == "/v5/order/create"][-1]
        assert create["orderType"] == "Limit" and create["timeInForce"] == "PostOnly"
        assert create["stopLoss"] == "98" and create["takeProfit"] == "105"
        assert ex.pending_symbols() == {"BTCUSDT"}
        # dolmadan önce aynı paritede / sınır aşan yeni emir yok
        r2 = await ex.open_from_decision(2, "BTCUSDT", "LONG", PLAN, None, None, "15m")
        assert r2["status"] == "ATLANDI"
        fake.fill_limit("x10-1-E")
        await ex.wait_pending()
    run(go())
    with dbs.session_scope() as s:
        p = s.scalars(select(Position)).one()
        assert p.entry_price == D("99.9") and p.venue == "BYBIT_DEMO"
        assert p.entry_fee < p.qty * p.entry_price * D("0.00055")                 # maker komisyonu
        o = s.scalars(select(Order)).one()
        assert o.status == "DOLDU" and o.order_type == "Limit"


def test_limit_entry_cancelled_after_timeout(settings, monkeypatch):
    import app.bots.bot09_execution as bx
    monkeypatch.setitem(bx.LIMIT_WAIT_SEC, "15m", 0.05)
    fake = FakeBybitPrivate()
    ex = limit_exec(settings, fake)

    async def go():
        r = await ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN, None, None, "15m")
        assert r["status"] == "BEKLIYOR"
        await ex.wait_pending()
    run(go())
    assert fake.count("/v5/order/cancel") == 1
    with dbs.session_scope() as s:
        assert s.scalars(select(Position)).first() is None
        assert s.scalars(select(Order)).one().status == "IPTAL"
    assert system()[1] is False


def test_limit_partial_fill_then_timeout_keeps_partial_position(settings, monkeypatch):
    import app.bots.bot09_execution as bx
    monkeypatch.setitem(bx.LIMIT_WAIT_SEC, "15m", 0.05)
    fake = FakeBybitPrivate()
    ex = limit_exec(settings, fake)

    async def go():
        await ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN, None, None, "15m")
        fake.fill_limit("x10-1-E", "0.2")
        await ex.wait_pending()
    run(go())
    with dbs.session_scope() as s:
        p = s.scalars(select(Position)).one()
        assert p.qty == D("0.2") and s.scalars(select(Order)).one().status == "KISMI"


def test_limit_entry_does_not_chase_when_price_ran(settings):
    """Fiyat plan girişinin üstüne kaçtıysa emir plan fiyatında bekler (piyasa modunda atlanırdı)."""
    fake = FakeBybitPrivate()
    ex = limit_exec(settings, fake, bid="100.6", ask="100.7")

    async def go():
        r = await ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN, None, D("0.1"), "15m")
        assert r["status"] == "BEKLIYOR" and D(r["price"]) == D("100")
        ex.pending["x10-1-E"].cancel()
        await ex.wait_pending()
    run(go())


def test_kill_switch_cancels_pending_limit(settings):
    fake = FakeBybitPrivate()
    ex = limit_exec(settings, fake)

    async def go():
        await ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN, None, None, "1h")
        with dbs.session_scope() as s:
            s.get(SystemState, 1).trading_enabled = False
        await ex.wait_pending()
    run(go())
    assert fake.count("/v5/order/cancel") == 1
    with dbs.session_scope() as s:
        assert s.scalars(select(Order)).one().status == "IPTAL"


def test_recover_pending_after_restart(settings):
    fake = FakeBybitPrivate()
    ex = limit_exec(settings, fake)
    with dbs.session_scope() as s:
        s.get(Decision, 1).plan = PLAN

    async def place():
        await ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN, None, None, "1h")
        fake.fill_limit("x10-1-E")
        ex.pending["x10-1-E"].cancel()                 # sunucu yeniden başladı: izleyici öldü
        await ex.wait_pending()
    run(place())
    ex2 = limit_exec(settings, fake)
    res = run(ex2.recover_pending())
    assert res[0]["status"] == "DOLDU"
    with dbs.session_scope() as s:
        assert s.scalars(select(Position)).one().qty == D("0.5")


def test_monitor_does_not_flag_pending_fill_as_orphan(settings):
    from app.engines.position_monitor import PositionMonitor
    fake = FakeBybitPrivate()
    ex = limit_exec(settings, fake)

    async def go():
        await ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN, None, None, "1h")
        fake.fill_limit("x10-1-E")                      # borsada pozisyon var, kayıt henüz yok
        await PositionMonitor(ex, 15)._reconcile(set())
        ex.pending["x10-1-E"].cancel()
        await ex.wait_pending()
    run(go())
    assert system()[1] is False


def test_round_tick_direction():
    from app.bots.bot09_execution import _round_tick
    assert _round_tick(D("100.07"), D("0.05"), down=True) == D("100.05")
    assert _round_tick(D("100.07"), D("0.05"), down=False) == D("100.1")
    assert _round_tick(D("100.07"), None, down=True) == D("100.07")


def test_sim_limit_fills_only_after_price_touches(settings):
    import time
    now = int(time.time() * 1000)
    minute = now - now % 60_000
    rows = {"v": [[str(minute), "100.5", "100.6", "100.4", "100.5", "1", "0"]]}
    public = FakeBybit(kline_rows=lambda s, i, n: rows["v"]).client()
    sim = SimBroker(public, D("0.055"), D("0.02"))

    async def go():
        await sim.place_limit("BTCUSDT", True, D("1"), D("100"), "c1", D("98"), D("105"))
        assert (await sim.order_status("c1")).status == "OPEN"
        # yerleştirme dakikasındaki dip sayılmaz; sonraki dakika limite değdi
        rows["v"] = [[str(minute + 60_000), "100.3", "100.4", "99.95", "100.2", "1", "0"],
                     [str(minute), "100.5", "100.6", "99.0", "100.5", "1", "0"]]
        f = await sim.order_status("c1")
        assert f.status == "FILLED" and f.avg_price == D("100") and f.fee == D("0.02")
        await sim.place_limit("BTCUSDT", False, D("1"), D("101"), "c2", D("103"), D("97"))
        await sim.cancel("BTCUSDT", "c2")
        assert (await sim.order_status("c2")).status == "REJECTED"
    run(go())


def test_closed_pnl_empty_falls_back_to_order_history(settings):
    """Bybit demo kapanış PnL kaydını boş döndürdü (25.11): emir geçmişindeki tetiklenen stoptan kapanır."""
    fake = FakeBybitPrivate(fill_price="100")
    fake.closed_pnl_empty = True
    ex = bybit_exec(settings, fake)
    run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))
    mon = PositionMonitor(ex)
    fake.stop_out("BTCUSDT", "98.5")
    assert run(mon.cycle())["closed"] == 1
    with dbs.session_scope() as s:
        p = s.query(Position).one()
        assert p.status == "KAPALI" and p.last_price == D("98.5")
    assert system()[1] is False


def test_position_gone_without_any_record_is_estimated_not_safe_mode(settings):
    import app.engines.position_monitor as pm
    fake = FakeBybitPrivate(fill_price="100")
    fake.closed_pnl_empty = True
    ex = bybit_exec(settings, fake)
    run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))
    mon = PositionMonitor(ex)
    fake.stop_out("BTCUSDT", "98", with_history=False)
    for _ in range(pm.MISSING_BEFORE_ESTIMATE - 1):
        assert run(mon.cycle())["closed"] == 0
    assert run(mon.cycle())["closed"] == 1
    with dbs.session_scope() as s:
        p = s.query(Position).one()
        assert p.status == "KAPALI" and p.last_price == D("98")
        assert s.query(AuditLog).filter(AuditLog.event == "kapanis_tahmini").count() == 1
        closed = s.query(AuditLog).filter(AuditLog.event == "pozisyon_kapandi").one()
        assert "tahmini" in closed.detail["reason"]
    assert system()[1] is False


def test_safe_mode_same_reason_written_once(settings):
    from app.bots.bot09_execution import safe_mode
    for _ in range(5):
        safe_mode("aynı neden")
    with dbs.session_scope() as s:
        assert s.query(AuditLog).filter(AuditLog.event == "guvenli_mod").count() == 1


def test_contract_needing_agreement_is_skipped_afterwards(settings):
    class Agreement(FakeBybitPrivate):
        def handler(self, req):
            if req.url.path == "/v5/order/create":
                return httpx.Response(200, json={"retCode": 110126, "retMsg": "You must sign the required agreement before trading this contract.", "result": {}})
            return super().handler(req)
    fake = Agreement()
    ex = bybit_exec(settings, fake)
    r = run(ex.open_from_decision(1, "SNDKUSDT", "LONG", PLAN))
    assert r["status"] == "REDDEDILDI" and "SNDKUSDT" in ex.blocked
    r2 = run(ex.open_from_decision(2, "SNDKUSDT", "LONG", PLAN))
    assert r2["status"] == "ATLANDI" and "sözleşme" in r2["reason"]


# --- Kısmi kâr alma -------------------------------------------------------------

def test_partial_take_profit_at_1r_halves_and_moves_stop_to_breakeven(settings):
    settings.partial_tp_enabled = True
    fake = FakeBybitPrivate(fill_price="100")
    ex = bybit_exec(settings, fake)
    run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))          # giriş 100, stop 98 → 1R = 102
    mon = PositionMonitor(ex)
    fake.fill_price = D("102")
    fake.positions["BTCUSDT"]["markPrice"] = "102"
    run(mon.cycle())
    with dbs.session_scope() as s:
        p = s.query(Position).one()
        assert p.partial_taken and p.qty == D("0.25")
        assert p.partial_realized == pytest.approx((D(2) * D("0.25")) - D("0.25") * D(102) * D("0.00055"), abs=D("1e-9"))
        assert p.current_stop > p.entry_price                                       # başabaş + komisyon
        assert s.query(Order).filter(Order.purpose == "KISMI_CIKIS").one().status == "DOLDU"
    assert D(fake.positions["BTCUSDT"]["size"]) == D("0.25")
    assert D(fake.positions["BTCUSDT"]["stopLoss"]) > D("100")
    # ikinci tur: tekrar kısmi kapatma yok
    run(mon.cycle())
    assert sum(1 for path, p in fake.calls if path == "/v5/order/create" and p.get("reduceOnly")) == 1
    # kalan yarı stoptan kapanır: toplam net = kısmi + kalan
    fake.closed["BTCUSDT"] = []
    fake.stop_out("BTCUSDT", "100.11")
    run(mon.cycle())
    with dbs.session_scope() as s:
        p = s.query(Position).one()
        assert p.status == "KAPALI" and p.realized_net_pnl > p.partial_realized - p.entry_fee - D("0.05")
        assert p.realized_net_pnl > 0


def test_partial_not_taken_below_1r(settings):
    settings.partial_tp_enabled = True
    fake = FakeBybitPrivate(fill_price="100")
    ex = bybit_exec(settings, fake)
    run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))
    fake.positions["BTCUSDT"]["markPrice"] = "101.9"
    run(PositionMonitor(ex).cycle())
    with dbs.session_scope() as s:
        assert not s.query(Position).one().partial_taken


def test_partial_skipped_when_qty_cannot_be_split(settings):
    settings.partial_tp_enabled = True
    ex = bybit_exec(settings, FakeBybitPrivate(fill_price="100"))
    run(ex.open_from_decision(1, "BTCUSDT", "LONG", {**PLAN, "qty": "0.001"}))
    r = run(ex.take_partial(1, D("0.5"), D("0.001")))
    assert r["status"] == "ATLANDI"
    with dbs.session_scope() as s:
        p = s.query(Position).one()
        assert p.partial_taken and p.qty == D("0.001")


def test_risk_threshold_is_configurable_and_clamped():
    from app.master_robot.decision_engine import outcome_for
    assert outcome_for(72, 75) == "DOGRULAMA" and outcome_for(72, 70) == "RISK_KONTROLU"
    assert outcome_for(64, 50) == "IZLEME"             # eşik 65in altına inemez
    assert outcome_for(84, 95) == "DOGRULAMA"          # eşik 85e sabitlenir
    assert outcome_for(90, 70) == "YUKSEK_ONCELIK" and outcome_for(40, 70) == "REDDEDILDI"


def test_agreement_blocked_symbols_persist_and_leave_scanner(settings):
    from app.bots.bot09_execution import agreement_blocked_symbols
    from app.engines.scanner_service import scan_config

    class Agreement(FakeBybitPrivate):
        def handler(self, req):
            if req.url.path == "/v5/order/create":
                return httpx.Response(200, json={"retCode": 110126, "retMsg": "You must sign the required agreement before trading this contract.", "result": {}})
            return super().handler(req)
    run(bybit_exec(settings, Agreement()).open_from_decision(1, "SPCXUSDT", "LONG", PLAN))
    assert agreement_blocked_symbols() == {"SPCXUSDT"}
    assert "SPCXUSDT" in bybit_exec(settings, FakeBybitPrivate()).blocked      # yeniden başlatmada hatırlanır
    assert "SPCXUSDT" in scan_config(settings).blacklist


def test_closed_queries_do_not_send_start_time_and_filter_old_rows(settings):
    """Demo hesapta startTime'lı sorgu boş dönüyordu: sorgu startTime'sız, eski kayıtlar istemcide süzülür."""
    fake = FakeBybitPrivate(fill_price="100")
    fake.closed["BTCUSDT"] = [{"symbol": "BTCUSDT", "closedSize": "9", "cumExitValue": "1", "updatedTime": "1000"}]
    ex = bybit_exec(settings, fake)
    run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))
    fake.stop_out("BTCUSDT", "98.5")
    run(PositionMonitor(ex).cycle())
    q = [p for path, p in fake.calls if path in ("/v5/position/closed-pnl", "/v5/order/history") and "orderLinkId" not in p]
    assert q and all("startTime" not in p for p in q)
    with dbs.session_scope() as s:
        p = s.query(Position).one()
        assert p.status == "KAPALI" and p.last_price == D("98.5")      # eski (9 adetlik) kayıt sayılmadı


def test_position_history_lists_closed_trades_with_reason(settings):
    from app.api.signals import position_history
    fake = FakeBybitPrivate(fill_price="100")
    fake.closed_pnl_empty = True
    ex = bybit_exec(settings, fake)
    run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))
    fake.stop_out("BTCUSDT", "98", with_history=False)
    import app.engines.position_monitor as pm
    mon = PositionMonitor(ex)
    for _ in range(pm.MISSING_BEFORE_ESTIMATE):
        run(mon.cycle())
    with dbs.session_scope() as s:
        h = position_history(100, s)
    assert h["count"] == 1 and h["losses"] == 1 and D(h["total_net"]) < 0
    t = h["trades"][0]
    assert t["symbol"] == "BTCUSDT" and t["exit_estimated"] and D(t["exit_price"]) == D("98") and t["closed_at"]


def _decision_with_reasons():
    from app.database.models import SignalRecord
    with dbs.session_scope() as s:
        d = s.get(Decision, 1)
        d.direction, d.timeframe, d.market_regime, d.score = "LONG", "15m", "GUCLU_YUKSELIS", 78
        d.plan = PLAN
        d.contributions = {"AL": 78.0, "SAT": 20.0, "rejim": {"adx": 41.2},
                           "puanlar": {"teknik": 17, "trend": 18, "hacim_likidite": 15, "rejim_uyumu": 15,
                                       "haber": 5, "akis": 4, "performans": 4},
                           "risk": {"risk_usd": "2.0", "risk_budget": "2.0", "notional": "350", "leverage": 10},
                           "notlar": ["akış/on-chain: on-chain veri yok, nötr puan"]}
        s.add(SignalRecord(bot_id="BOT10", strategy_id="nr7_breakout+ema", symbol="BTCUSDT", timeframe="15m",
                           action="AL", confidence=85, decision_id=1,
                           payload={"reason": "Yukarı NR7 kırılımı kapanışla doğrulandı; hacim 2.4x"}))
        s.add(SignalRecord(bot_id="BOT02", strategy_id="trend_pullback", symbol="BTCUSDT", timeframe="15m",
                           action="AL", confidence=70, decision_id=1, payload={"reason": "EMA20'den tepki"}))
        s.add(SignalRecord(bot_id="BOT06", strategy_id="funding", symbol="BTCUSDT", timeframe="15m",
                           action="SAT", confidence=40, decision_id=1, payload={"reason": "funding yüksek"}))


def test_trade_note_explains_why_in_plain_turkish(settings):
    from app.notifications.trade_note import build_note
    _decision_with_reasons()
    with dbs.session_scope() as s:
        note = build_note(s, 1)
    assert note.startswith("📌 Neden alındı: BTC için ALIŞ (yükseliş beklentisi)")
    assert "Piyasa güçlü bir yükseliş trendinde." in note
    assert note.index("Sıkışma botu") < note.index("Trend botu")            # güvene göre sıralı
    assert "en dar aralığından yukarı kırıldı" in note
    assert "1 bot ters yönde sinyal verdi" in note and "Robotun güveni: 78/100." in note
    assert "Zarar durdur 98 (%2,00 aşağıda), hedef 105 (%5,00 yukarıda)" in note
    assert "Riske edilen: 2,00 $ · pozisyon büyüklüğü 350,00 $ (10x kaldıraç)." in note
    for jargon in ("ADX", "NR7", "nr7_", "EMA", "inside bar", "UP"):
        assert jargon not in note


def test_tr_num_format():
    from app.notifications.trade_note import tr_num
    assert tr_num(D("58049.2")) == "58.049,2" and tr_num(D("0.05821")) == "0,05821"
    assert tr_num(D("2.5"), 2) == "2,50" and tr_num(D("1234.5"), 2) == "1.234,50"


def test_position_open_stores_note_and_telegram_shows_it(settings):
    from app.notifications.telegram import format_event
    _decision_with_reasons()
    ex = bybit_exec(settings, FakeBybitPrivate(fill_price="100"))
    run(ex.open_from_decision(1, "BTCUSDT", "LONG", PLAN))
    with dbs.session_scope() as s:
        p = s.query(Position).one()
        assert p.entry_note and "Sıkışma botu" in p.entry_note
        ev = s.query(AuditLog).filter(AuditLog.event == "pozisyon_acildi").one()
        msg = format_event(ev.event, ev.detail)
    assert "📈 İşleme girildi: BTC ALIŞ" in msg and "📌 Neden alındı: BTC için ALIŞ" in msg and "Sıkışma botu" in msg
