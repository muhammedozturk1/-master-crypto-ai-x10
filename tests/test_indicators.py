import random

import pytest

from app.indicators import core as I


def series(n=400, seed=7):
    random.seed(seed)
    c = [100.0]
    for _ in range(n - 1):
        c.append(c[-1] * (1 + random.gauss(0, 0.01)))
    h = [x * (1 + abs(random.gauss(0, 0.004))) for x in c]
    l = [x * (1 - abs(random.gauss(0, 0.004))) for x in c]
    v = [random.uniform(1, 10) for _ in c]
    return h, l, c, v


def test_warmup_is_none_not_fake():
    c = [float(i) for i in range(1, 30)]
    assert I.sma(c, 5)[:4] == [None] * 4 and I.sma(c, 5)[4] == 3.0
    assert I.ema(c, 10)[8] is None and I.ema(c, 10)[9] == 5.5
    assert I.ema([1.0, 2.0], 5) == [None, None]


def test_rsi_extremes():
    assert I.rsi([float(i) for i in range(30)], 14)[-1] == 100.0
    assert I.rsi([float(30 - i) for i in range(30)], 14)[-1] == 0.0
    assert I.rsi([5.0] * 30, 14)[-1] == 50.0


def test_atr_constant_range():
    h, l, c = [11.0] * 40, [9.0] * 40, [10.0] * 40
    assert I.atr(h, l, c, 14)[-1] == pytest.approx(2.0)


def test_donchian_excludes_current_bar():
    highs = [1, 2, 3, 4, 100.0]
    up, _ = I.donchian(highs, highs, 4)
    assert up[4] == 4  # 100'lük güncel mum dahil değil


def test_percentile_rank():
    vals = [float(i) for i in range(100)] + [-1.0]
    assert I.percentile_rank(vals, 100, 100) == 0.0
    assert I.percentile_rank(vals, 99, 50) == 100.0


@pytest.mark.parametrize("name", ["sma", "ema", "rsi", "atr", "adx", "pdi", "bb_up", "macd", "macd_sig"])
def test_matches_reference_library(name):
    pd = pytest.importorskip("pandas")
    ta = pytest.importorskip("ta")
    h, l, c, v = series()
    H, L, C = pd.Series(h), pd.Series(l), pd.Series(c)
    mine, ref = {
        "sma": (I.sma(c, 20), ta.trend.sma_indicator(C, 20)),
        "ema": (I.ema(c, 50), ta.trend.ema_indicator(C, 50)),
        "rsi": (I.rsi(c, 14), ta.momentum.rsi(C, 14)),
        "atr": (I.atr(h, l, c, 14), ta.volatility.average_true_range(H, L, C, 14)),
        "adx": (I.adx(h, l, c, 14)[0], ta.trend.adx(H, L, C, 14)),
        "pdi": (I.adx(h, l, c, 14)[1], ta.trend.adx_pos(H, L, C, 14)),
        "bb_up": (I.bollinger(c, 20, 2)[1], ta.volatility.bollinger_hband(C, 20, 2)),
        "macd": (I.macd(c)[0], ta.trend.macd(C)),
        "macd_sig": (I.macd(c)[1], ta.trend.macd_signal(C)),
    }[name]
    ref = ref.tolist()
    for i in range(len(c) - 50, len(c)):
        assert mine[i] == pytest.approx(ref[i], rel=1e-6, abs=1e-6)
