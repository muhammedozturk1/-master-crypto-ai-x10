from decimal import Decimal as D

import pytest

from app.bots.base import MarketContext
from app.bots.bot10_nr7 import Nr7Bot, is_nr7
from app.data.candles import Candle
from app.domain.signals import Action

STEP = 15 * 60_000


def bar(i, o, h, l, c, v=10.0):
    return Candle(1_700_000_000_000 + i * STEP, D(str(o)), D(str(h)), D(str(l)), D(str(c)), D(str(v)), D("0"), "15")


def flat(n):
    """Aralığı 2 olan sakin mumlar (NR7 oluşturmaz)."""
    return [bar(i, 100, 101, 99, 100) for i in range(n)]


def nr7_bar(i):
    return bar(i, 100, 100.25, 99.75, 100.0)  # aralık 0.5 < 2


def ctx(candles, **kw):
    return MarketContext(symbol="BTCUSDT", timeframe="15m", candles=candles, **kw)


def run(candles, **kw):
    return Nr7Bot().analyze(ctx(candles, **kw))


def test_is_nr7_strict():
    assert is_nr7([2, 2, 2, 2, 2, 2, 1], 6)
    assert not is_nr7([2, 2, 2, 2, 2, 1, 1], 6)  # eşit aralık NR7 sayılmaz
    assert not is_nr7([1, 1, 1], 2)  # 6 önceki mum yok


def test_nr7_on_last_bar_waits():
    cs = flat(79) + [nr7_bar(79)]
    sigs = run(cs)
    assert len(sigs) == 1 and sigs[0].action == Action.BEKLE
    assert sigs[0].details["status"] == "KIRILIM_BEKLENIYOR"
    assert sigs[0].details["nr7_high"] == 100.25


def test_closed_breakout_gives_long_with_net_rr_2():
    cs = flat(78) + [nr7_bar(78), bar(79, 100.1, 100.7, 100.0, 100.6, v=20)]
    [s] = run(cs)
    assert s.action == Action.AL and s.strategy_id.startswith("nr7_breakout")
    assert s.stop_loss < D("99.75")              # NR7 Low altında tamponlu stop
    assert s.entry_low == D("100.25") and s.entry_high == D("100.6")
    assert float(s.expected_net_rr) == pytest.approx(2.0, abs=1e-6)
    assert s.gross_reward_risk() > 2              # maliyetler yüzünden brüt oran 2'nin üstünde
    assert s.details["volume_ratio"] == 2.0


def test_wick_only_is_not_breakout():
    cs = flat(78) + [nr7_bar(78), bar(79, 100.0, 101.0, 99.9, 100.2, v=30)]  # fitil üstte, kapanış içeride
    [s] = run(cs)
    assert s.action == Action.BEKLE


def test_low_volume_breakout_rejected():
    cs = flat(78) + [nr7_bar(78), bar(79, 100.1, 100.7, 100.0, 100.6, v=5)]
    assert run(cs) == []


def test_overextended_breakout_rejected():
    cs = flat(78) + [nr7_bar(78), bar(79, 100.1, 103.2, 100.0, 103.0, v=30)]
    assert run(cs) == []


def test_signal_expires_after_three_bars():
    cs = flat(75) + [nr7_bar(75)] + [bar(76 + j, 100, 100.2, 99.8, 100.0) for j in range(4)]
    assert run(cs) == []


def test_breakdown_short_only_when_allowed():
    cs = flat(78) + [nr7_bar(78), bar(79, 99.9, 100.0, 99.3, 99.4, v=20)]
    assert run(cs, allow_short=False) == []
    [s] = run(cs, allow_short=True)
    assert s.action == Action.SAT and s.stop_loss > D("100.25") and s.take_profit < D("99.4")


def test_retest_signal():
    cs = flat(76) + [nr7_bar(76), bar(77, 100.1, 100.7, 100.0, 100.6, v=20),
                     bar(78, 100.4, 100.8, 100.3, 100.7, v=12)]
    [s] = run(cs)
    assert s.action == Action.AL and s.strategy_id.startswith("nr7_retest")
    assert s.details["status"] == "RETEST"


def test_htf_against_reduces_confidence():
    cs = flat(78) + [nr7_bar(78), bar(79, 100.1, 100.7, 100.0, 100.6, v=20)]
    base = run(cs)[0].confidence
    down = [bar(i, 200 - i, 201 - i, 199 - i, 199.5 - i) for i in range(80)]
    [s] = run(cs, htf_candles=down)
    assert s.confidence == base - 15 and "üst zaman dilimi ters yönde" in s.risk_warnings


def test_too_few_bars():
    assert run(flat(40) + [nr7_bar(40)]) == []


def test_abnormal_spread_rejected():
    cs = flat(78) + [nr7_bar(78), bar(79, 100.1, 100.7, 100.0, 100.6, v=20)]
    assert run(cs, spread_pct=0.5) == []


def test_dedup_key_stable():
    cs = flat(78) + [nr7_bar(78), bar(79, 100.1, 100.7, 100.0, 100.6, v=20)]
    assert run(cs)[0].details["dedup_key"] == run(cs)[0].details["dedup_key"]
