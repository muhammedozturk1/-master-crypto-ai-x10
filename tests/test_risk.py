from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from app.bots.bot07_risk import RiskManager, TradeRequest
from app.bots.bot08_portfolio import account_state
from app.config import RiskLimits
from app.database import session as dbs
from app.database.models import Position

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    from app.database.models import Decision
    dbs.configure(f"sqlite:///{tmp_path}/r.db")
    dbs.init_db()
    with dbs.session_scope() as s:
        s.add(Decision(id=1, symbol="X", outcome="RISK_KONTROLU", score=80, reason="test", contributions={}))
    return dbs


def pos(symbol="ETHUSDT", status="KAPALI", pnl="0", closed=None, entry="100", stop="98", qty="1", lev=2, unreal=None):
    return Position(decision_id=1, mode="PAPER", symbol=symbol, side="LONG", strategy_id="x", qty=D(qty),
                    leverage=lev, entry_price=D(entry), initial_stop=D(stop), current_stop=D(stop),
                    status=status, realized_net_pnl=D(pnl) if status == "KAPALI" else None,
                    unrealized_net_pnl=D(unreal) if unreal is not None else None,
                    closed_at=closed, entry_fee=D("0.05"), exit_fee=D("0.05") if status == "KAPALI" else D(0))


def acc(db, *positions, now=NOW):
    with db.session_scope() as s:
        for p in positions:
            s.add(p)
    with db.session_scope() as s:
        return account_state(s, D(200), now=now)


def req(entry="100", stop="98", target="105", cost="0.12", **kw):
    return TradeRequest("BTCUSDT", True, D(entry), D(stop), D(target), D(cost), **kw)


RM = RiskManager(RiskLimits())


def test_fresh_account_and_spec_example(db):
    a = acc(db)
    assert a.equity == 200 and a.drawdown_pct == 0 and a.open_positions == 0
    # Şartname 10.3: 1 USD risk, %2 stop → komisyon hariç 50 USD; maliyetlerle biraz altı
    v = RM.evaluate(req(cost="0"), a, NOW)
    assert v.approved and v.risk_budget == D(1)
    assert v.notional == pytest.approx(D(50)) and v.margin == pytest.approx(D(25))
    v2 = RM.evaluate(req(cost="0.12"), a, NOW)
    assert v2.approved and v2.notional < v.notional and v2.risk_usd <= D(1)


def test_margin_caps_tight_stop(db):
    v = RM.evaluate(req(entry="100", stop="99.8", target="101", cost="0"), acc(db), NOW)
    assert v.approved and v.margin <= D(50)  # tek pozisyon %25 teminat sınırı
    assert v.risk_usd < D(1)


def test_min_qty_never_exceeds_risk(db):
    v = RM.evaluate(req(qty_step=D("1"), min_qty=D("1")), acc(db), NOW)
    assert not v.approved and any("borsa minimumu" in r for r in v.reasons)


def test_rr_below_two_rejected(db):
    v = RM.evaluate(req(target="103"), acc(db), NOW)
    assert not v.approved and any("ödül/risk" in r for r in v.reasons)


def test_high_volatility_halves_budget(db):
    v = RM.evaluate(req(regime="YUKSEK_VOLATILITE"), acc(db), NOW)
    assert v.risk_budget == D("0.5")


def test_daily_loss_limit(db):
    a = acc(db, pos(pnl="-4.5", closed=NOW - timedelta(hours=2)))
    v = RM.evaluate(req(), a, NOW)
    assert not v.approved and any("günlük zarar" in r for r in v.reasons)


def test_consecutive_losses_cooldown_and_halt(db):
    losses = [pos(pnl="-0.5", closed=NOW - timedelta(minutes=10 + k)) for k in range(3)]
    v = RM.evaluate(req(), acc(db, *losses), NOW)
    assert not v.approved and any("bekleme" in r for r in v.reasons) and v.halt is None
    v_later = RM.evaluate(req(), acc(db), NOW + timedelta(minutes=40))
    assert not any("bekleme" in r for r in v_later.reasons)


def test_five_losses_stop_trading(db):
    losses = [pos(pnl="-0.3", closed=NOW - timedelta(days=1, minutes=k)) for k in range(5)]
    v = RM.evaluate(req(), acc(db, *losses), NOW)
    assert not v.approved and v.halt == "STOP_TRADING"


def test_drawdown_safe_mode_includes_unrealized(db):
    a = acc(db, pos(pnl="10", closed=NOW - timedelta(days=3)), pos(symbol="SOLUSDT", status="ACIK", unreal="-12"),
            pos(pnl="-10", closed=NOW - timedelta(days=2)))
    # zirve 210, şimdi 200 + 10 - 10 - 12 = 188 → %10,5 düşüş
    assert a.peak_equity == 210 and a.drawdown_pct > 10
    v = RM.evaluate(req(), a, NOW)
    assert not v.approved and v.halt == "SAFE_MODE"


def test_position_limits(db):
    a = acc(db, pos(symbol="BTCUSDT", status="ACIK"), pos(symbol="SOLUSDT", status="ACIK"))
    v = RM.evaluate(req(), a, NOW)
    assert any("en fazla 2" in r for r in v.reasons) and any("zaten açık" in r for r in v.reasons)


def test_compounding_risk_budget(db):
    a = acc(db, pos(pnl="5", closed=NOW - timedelta(days=1)))
    assert a.equity == 205 and RM.evaluate(req(), a, NOW).risk_budget == D("1.025")  # şartname 11.3


def test_wrong_side_stop_rejected(db):
    assert not RM.evaluate(req(stop="101"), acc(db), NOW).approved


def test_decision_goes_through_risk_layer(tmp_path):
    """Yüksek puanlı karar BOT07'den geçer: miktar ve teminat plana eklenir; sınır doluysa RISK_RED."""
    from app.config import Settings
    from app.engines.bot_runner import BotRunner
    from app.master_robot.decision_engine import MarketInputs, decide
    from tests.test_master import ctx, sig
    from app.domain.signals import Action
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/x.db")
    dbs.configure(settings.sqlalchemy_url)
    dbs.init_db()
    runner = BotRunner(None, settings)
    runner._instruments = {"XUSDT": {"qty_step": "0.001", "min_order_qty": "0.001", "min_notional": "5"}}
    sigs = [sig("BOT10", "nr7_breakout+ema", conf=80, vr=2.2), sig("BOT02", "trend_pullback", conf=85),
            sig("BOT06", "liquidity_sweep", conf=75)]
    d = decide(ctx("up"), sigs, MarketInputs(sentiment=20))
    runner._risk_check(d)
    assert d.outcome == "ONAYLANDI" and D(d.plan["qty"]) > 0 and D(d.plan["risk_usd"]) <= D(1)
    assert d.contributions["risk"]["approved"] is True
    # Haftalık zarar sınırı dolu hesap → red ve sistem durdurulur
    from app.database.models import Decision, SystemState
    with dbs.session_scope() as s:
        s.add(Decision(id=1, symbol="X", outcome="ONAYLANDI", score=80, reason="t", contributions={}))
    with dbs.session_scope() as s:
        s.add(pos(pnl="-11", closed=datetime.now(timezone.utc) - timedelta(minutes=5)))
    d2 = decide(ctx("up"), sigs, MarketInputs(sentiment=20))
    runner._risk_check(d2)
    assert d2.outcome == "RISK_RED" and "haftalık zarar" in d2.reason
    with dbs.session_scope() as s:
        assert s.get(SystemState, 1).trading_enabled is False


def test_portfolio_and_risk_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import Settings, get_settings
    from app.main import app
    from tests.fake_bybit import FakeBybit
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/p.db", scanner_enabled=False,
                        runner_enabled=False, calendar_enabled=False, arbitrage_enabled=False)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.market = FakeBybit().client()
    with TestClient(app) as c:
        p = c.get("/portfolio").json()
        assert p["equity"] == "200.0000" and p["open_positions"] == 0 and p["mode"] == "PAPER"
        r = c.get("/risk").json()
        assert r["usage"]["risk_budget_usd"] == "1.0000" and r["usage"]["daily_limit"] == "4.00"
        assert r["new_trades_blocked_by"] == [] and r["hard_caps"]["max_leverage"] == "3"
    app.dependency_overrides.clear()
