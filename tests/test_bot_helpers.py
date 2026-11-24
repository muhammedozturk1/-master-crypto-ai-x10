from decimal import Decimal as D

import pytest

from app.bots.base import MarketContext
from app.bots.common import Plan, candle_pattern, cluster_levels, make_signal, net_rr, pivots, target_for
from app.data.candles import Candle
from app.indicators import core as I


def ctx(**kw):
    cs = [Candle(k * 900_000, D(100), D(101), D(99), D(100), D(1), D(0), "15") for k in range(5)]
    return MarketContext("XUSDT", "15m", cs, **kw)


def test_target_gives_exact_net_rr():
    cost = 0.2
    tp = target_for(100, 98, cost, True)
    assert net_rr(100, 98, tp, cost) == pytest.approx(2.0)
    tp_s = target_for(100, 102, cost, False)
    assert tp_s < 100 and net_rr(100, 102, tp_s, cost) == pytest.approx(2.0)


def test_make_signal_rejections():
    c = ctx()
    assert make_signal(c, "BOT01", "x", Plan(True, 100, 100, 101), 60, "r") is None      # stop yanlış tarafta
    assert make_signal(c, "BOT01", "x", Plan(True, 100, 100, 95), 60, "r") is None       # stop %5
    assert make_signal(c, "BOT01", "x", Plan(True, 100, 100, 99, target=100.5), 60, "r") is None  # net RR < 2
    assert make_signal(ctx(allow_short=False), "BOT01", "x", Plan(False, 100, 100, 101), 60, "r") is None
    s = make_signal(c, "BOT01", "x+tag", Plan(True, 100, 100, 99), 150, "r")
    assert s.confidence == 100 and float(s.expected_net_rr) == pytest.approx(2.0)
    assert s.details["dedup_key"].startswith("BOT01:XUSDT:15m:x:")


def test_pivots_ignore_unconfirmed_right_wing():
    highs = [1, 2, 5, 2, 1, 2, 3, 9, 3]
    lows = [h - 0.5 for h in highs]
    tops, _ = pivots(highs, lows, end=len(highs), lookback=20, wing=2)
    assert (2, 5) in tops
    assert all(k != 7 for k, _ in tops)  # sağında 2 mum yok → onaylanmamış


def test_cluster_levels():
    lv = cluster_levels([(1, 100.0), (5, 100.2), (9, 105.0)], tol_pct=0.3)
    assert lv[0][1] == 2 and lv[0][0] == pytest.approx(100.1) and lv[1] == (105.0, 1)


@pytest.mark.parametrize("o,h,l,c,expected", [
    ([10, 10, 9.5], [10.2, 10.2, 10.6], [9.4, 9.4, 9.3], [9.5, 9.5, 10.5], ("bullish_engulfing", 1)),
    ([10, 10, 10.5], [10.6, 10.6, 10.6], [9.9, 9.9, 9.4], [10.5, 10.5, 9.5], ("bearish_engulfing", -1)),
    ([10, 10, 10], [10.25, 10.25, 10.25], [9.95, 9.95, 9.4], [10.2, 10.2, 10.2], ("hammer", 1)),
    ([10, 10, 10.2], [10.25, 10.25, 10.85], [9.95, 9.95, 9.95], [10.2, 10.2, 10.0], ("shooting_star", -1)),
])
def test_candle_patterns(o, h, l, c, expected):
    assert candle_pattern(o, h, l, c, 2) == expected


def test_supertrend_and_stoch_rsi():
    up = [100 + i for i in range(60)]
    line, d = I.supertrend([x + 1 for x in up], [x - 1 for x in up], up, 10, 3)
    assert d[-1] == 1 and line[-1] < up[-1]
    dn = [160 - i for i in range(60)]
    line, d = I.supertrend([x + 1 for x in dn], [x - 1 for x in dn], dn, 10, 3)
    assert d[-1] == -1 and line[-1] > dn[-1]


def test_stoch_rsi_matches_reference():
    pd = pytest.importorskip("pandas")
    ta = pytest.importorskip("ta")
    import random
    random.seed(3)
    c = [100.0]
    for _ in range(300):
        c.append(c[-1] * (1 + random.gauss(0, 0.01)))
    ref = ta.momentum.stochrsi_k(pd.Series(c), 14, 3, 3).tolist()
    mine = I.stoch_rsi(c, 14, 3)
    for i in range(250, 301):
        assert mine[i] == pytest.approx(ref[i] * 100, abs=1e-6)
