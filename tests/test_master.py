"""Aşama 4: piyasa rejimi, strateji ağırlıkları, sinyal birleştirme ve karar motoru."""

import asyncio
from decimal import Decimal as D

import pytest

from app.bots.base import MarketContext
from app.data.candles import Candle
from app.domain.signals import Action, DataQuality, Signal
from app.master_robot.decision_engine import CAPS, MarketInputs, decide, score_direction
from app.master_robot.market_regime import Regime, RegimeResult, detect_regime
from app.master_robot.strategy_selector import strategy_weight

STEP = 900_000


def series(kind: str, n: int = 260):
    cs = []
    for k in range(n):
        if kind == "up":        # istikrarlı güçlü yükseliş
            o = 100 + k * 0.4; c = o + 0.35; h = c + 0.1; l = o - 0.1
        elif kind == "down":
            o = 200 - k * 0.4; c = o - 0.35; h = o + 0.1; l = c - 0.1
        elif kind == "flat":    # salınım, trend yok
            o = 100 + (1 if k % 2 else -1) * 0.5; c = 200 - o; h = max(o, c) + 0.3; l = min(o, c) - 0.3
        cs.append(Candle(k * STEP, D(str(round(o, 4))), D(str(round(h, 4))), D(str(round(l, 4))), D(str(round(c, 4))),
                         D(10), D(0), "15"))
    return cs


def ctx(kind, spread=0.01):
    return MarketContext("XUSDT", "15m", series(kind), spread_pct=spread)


def sig(bot, strat, action=Action.AL, conf=70, vr=1.6, quality=DataQuality.IYI, sym="XUSDT"):
    long = action == Action.AL
    return Signal(bot_id=bot, strategy_id=strat, symbol=sym, timeframe="15m", action=action, confidence=conf,
                  entry_low=D(100), entry_high=D(100), stop_loss=D(99) if long else D(101),
                  take_profit=D(102.5) if long else D(97.5), expected_net_rr=D(2), estimated_cost_pct=D("0.12"),
                  reason="t", data_quality=quality,
                  details={"volume_ratio": vr, "dedup_key": f"{bot}:{strat}:{action.value}:{conf}"})


# --- Rejim ----------------------------------------------------------------

def test_regimes():
    assert detect_regime(ctx("up")).regime == Regime.GUCLU_YUKSELIS
    assert detect_regime(ctx("down")).regime == Regime.GUCLU_DUSUS
    assert detect_regime(ctx("flat")).regime == Regime.YATAY
    assert detect_regime(ctx("up"), news_veto=True).regime == Regime.HABER_BELIRSIZLIGI
    assert detect_regime(ctx("up", spread=0.3)).regime == Regime.DUSUK_LIKIDITE


def test_abnormal_move():
    cs = series("flat")
    last = cs[-1]
    cs[-1] = Candle(last.start_ms, last.open, D(112), last.low, D(111), D(10), D(0), "15")
    assert detect_regime(MarketContext("X", "15m", cs)).regime == Regime.ANORMAL_HAREKET


def test_strategy_weights():
    assert strategy_weight(Regime.GUCLU_YUKSELIS, "trend_pullback+adx25", True) == 1.3
    assert strategy_weight(Regime.GUCLU_YUKSELIS, "trend_pullback", False) == 0.0   # güçlü trende karşı yok
    assert strategy_weight(Regime.YATAY, "bollinger_mean_reversion", True) == 1.3
    assert strategy_weight(Regime.VOLATILITE_SIKISMASI, "nr7_breakout+ema", True) == 1.3
    assert strategy_weight(Regime.HABER_BELIRSIZLIGI, "nr7_breakout", True) == 0.0


# --- Birleştirme ----------------------------------------------------------

def test_categories_take_max_not_sum():
    reg = RegimeResult(Regime.GUCLU_YUKSELIS, 1, {})
    one, c1, _ = score_direction(True, [sig("BOT01", "ema_rsi_trend", conf=70)], reg, 0.01, MarketInputs())
    two, c2, _ = score_direction(True, [sig("BOT01", "ema_rsi_trend", conf=70), sig("BOT10", "nr7_breakout", conf=60)],
                                 reg, 0.01, MarketInputs())
    assert c1["teknik"] == c2["teknik"] == 14.0     # aynı kategoride ikinci sinyal eklenmez
    assert sum(CAPS.values()) == 100


def test_single_signal_is_not_enough_confluence_is():
    c = ctx("up")
    lone = decide(c, [sig("BOT10", "nr7_breakout", conf=60)], MarketInputs())
    assert lone.outcome in ("IZLEME", "DOGRULAMA") and lone.score < 75
    many = decide(c, [sig("BOT10", "nr7_breakout+ema", conf=80, vr=2.2), sig("BOT02", "trend_pullback", conf=85),
                      sig("BOT06", "liquidity_sweep", conf=75)], MarketInputs(sentiment=20))
    assert many.outcome in ("RISK_KONTROLU", "YUKSEK_ONCELIK") and many.score >= 75
    assert many.direction == "LONG" and many.plan["stop_loss"] == "99"
    assert {x["bot"] for x in many.contributions["destekleyenler"]} == {"BOT10", "BOT02", "BOT06"}


def test_conflict_rejected():
    c = ctx("flat")
    # İki yön eşit güçte → çelişki, işlem yok
    d = decide(c, [sig("BOT01", "bollinger_mean_reversion", Action.AL, 80, vr=2.2),
                   sig("BOT02", "donchian_breakout", Action.AL, 80, vr=2.2),
                   sig("BOT01", "bollinger_mean_reversion", Action.SAT, 80, vr=2.2),
                   sig("BOT02", "donchian_breakout", Action.SAT, 80, vr=2.2)], MarketInputs())
    assert d.outcome == "CELISKI"
    # Bir taraf belirgin güçlüyse çelişki değil, zayıf taraf kadar ceza
    d = decide(c, [sig("BOT01", "bollinger_mean_reversion", Action.AL, 80, vr=2.2),
                   sig("BOT02", "donchian_breakout", Action.SAT, 80, vr=2.2),
                   sig("BOT01", "bollinger_mean_reversion", Action.SAT, 80, vr=2.2)], MarketInputs())
    assert d.outcome != "CELISKI" and d.direction == "SHORT" and d.contributions["puanlar"]["celiski_cezasi"] < 0


def test_veto_and_system_stop():
    c = ctx("up")
    sigs = [sig("BOT10", "nr7_breakout", conf=80), sig("BOT02", "trend_pullback", conf=85)]
    assert decide(c, sigs, MarketInputs(news_veto=True, veto_reason="CPI")).outcome == "VETO"
    assert decide(c, sigs, MarketInputs(trading_enabled=False)).outcome == "SISTEM_DURDU"


def test_counter_trend_rejected_in_strong_trend():
    d = decide(ctx("up"), [sig("BOT02", "donchian_breakout", Action.SAT, 90)], MarketInputs())
    assert d.outcome == "REDDEDILDI" and "rejiminde bu yönde kullanılmaz" in d.reason


def test_degraded_data_and_bot_weight_lower_score():
    c = ctx("up")
    base = decide(c, [sig("BOT02", "trend_pullback", conf=80)], MarketInputs()).score
    assert decide(c, [sig("BOT02", "trend_pullback", conf=80, quality=DataQuality.EKSIK)], MarketInputs()).score < base
    assert decide(c, [sig("BOT02", "trend_pullback", conf=80)], MarketInputs(bot_weights={"BOT02": 0.5})).score < base


def test_no_trade_signals_no_decision():
    wait = Signal(bot_id="BOT10", strategy_id="nr7_watch", symbol="XUSDT", timeframe="15m", action=Action.BEKLE,
                  confidence=0, reason="w")
    assert decide(ctx("up"), [wait], MarketInputs()) is None


# --- Döngü ve kayıt --------------------------------------------------------

def test_runner_records_decision_and_links_signals(tmp_path):
    from app.config import Settings
    from app.database import session as dbs
    from app.database.models import Decision, SignalRecord
    from app.engines.bot_runner import BotRunner
    from app.engines.scanner_service import scan_once
    from tests.fake_bybit import FakeBybit
    from tests.test_runner import nr7_breakout_rows
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/d.db", scanner_enabled=False,
                        runner_enabled=False, calendar_enabled=False, arbitrage_enabled=False, execution_enabled=False, runner_timeframes=["15m"])
    dbs.configure(settings.sqlalchemy_url)
    dbs.init_db()
    async def go():
        m = FakeBybit(kline_rows=nr7_breakout_rows).client()
        await scan_once(m, settings)
        r = BotRunner(m, settings)
        a = await r.run_timeframe("15m")
        b = await r.run_timeframe("15m")
        await m.close()
        return a, b
    a, b = asyncio.run(go())
    assert sum(a.decisions.values()) == 1 and b.decisions == {}
    with dbs.session_scope() as s:
        [d] = s.query(Decision).all()
        assert d.symbol == "BTCUSDT" and d.direction == "LONG" and d.plan["bot_id"] == "BOT10"
        assert d.decision_key.startswith("BTCUSDT:15m:")
        sr = s.query(SignalRecord).filter_by(symbol="BTCUSDT", action="AL").one()
        assert sr.decision_id == d.id


def test_decisions_and_master_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import Settings, get_settings
    from app.engines.bot_runner import BotRunner
    from app.engines.scanner_service import scan_once
    from app.main import app
    from tests.fake_bybit import FakeBybit
    from tests.test_runner import nr7_breakout_rows
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/e.db", scanner_enabled=False,
                        runner_enabled=False, calendar_enabled=False, arbitrage_enabled=False, execution_enabled=False, runner_timeframes=["15m"])
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.market = FakeBybit(kline_rows=nr7_breakout_rows).client()
    with TestClient(app) as c:
        c.portal.call(scan_once, app.state.market, settings)
        c.portal.call(BotRunner(app.state.market, settings).run_timeframe, "15m")
        [d] = c.get("/decisions").json()
        assert d["symbol"] == "BTCUSDT" and d["plan"]["stop_loss"]
        assert c.get("/nr7").json()[0]["main_robot_decision"]["outcome"] == d["outcome"]
        m = c.get("/master").json()
        assert m["last_decision"]["id"] == d["id"] and "Emir gönderimi yok" in m["execution"]
    app.dependency_overrides.clear()
