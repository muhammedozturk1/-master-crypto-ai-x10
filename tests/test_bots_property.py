"""Rastgele fiyat serilerinde tüm sinyal botları: çökme yok, her işlem sinyali ortak
kurallara uyuyor ve her strateji en az bir kez tetikleniyor (kapsam kontrolü)."""

import random
from collections import Counter
from decimal import Decimal as D

import pytest

from app.bots.base import MarketContext
from app.bots.registry import IMPLEMENTATIONS
from app.data.candles import Candle
from app.domain.signals import Action


def random_series(seed: int, n: int = 300):
    random.seed(seed)
    c = [100.0]
    drift = random.choice([-0.002, 0.0, 0.002])
    for _ in range(n - 1):
        c.append(c[-1] * (1 + drift + random.gauss(0, 0.008)))
    out = []
    for k in range(n):
        o, cl = (c[k - 1] if k else c[0]), c[k]
        h = max(o, cl) * (1 + abs(random.gauss(0, 0.003)))
        l = min(o, cl) * (1 - abs(random.gauss(0, 0.003)))
        out.append(Candle(k * 900_000, D(f"{o:.6f}"), D(f"{h:.6f}"), D(f"{l:.6f}"), D(f"{cl:.6f}"),
                          D(f"{random.uniform(5, 20):.3f}"), D(0), "15"))
    return out, c


@pytest.fixture(scope="module")
def all_signals():
    sigs = []
    for seed in range(30):
        cs, closes = random_series(seed)
        for end in range(220, len(cs), 2):
            ctx = MarketContext(f"S{seed}USDT", "15m", cs[:end + 1], extra={"btc_closes": closes[:end + 1]})
            for cls in IMPLEMENTATIONS.values():
                sigs.extend(cls().analyze(ctx))
    return [s for s in sigs if s.action != Action.BEKLE]


def test_every_trade_signal_obeys_common_rules(all_signals):
    assert all_signals
    for s in all_signals:
        entry = float(s.entry_high if s.action == Action.AL else s.entry_low)
        stop, tp = float(s.stop_loss), float(s.take_profit)
        assert float(s.expected_net_rr) >= 1.999, s
        assert abs(entry - stop) / entry * 100 <= 4.0 + 1e-9, s
        assert (stop < entry < tp) if s.action == Action.AL else (tp < entry < stop), s
        assert 0 <= s.confidence <= 100
        assert s.details.get("dedup_key")
        assert s.reason


def test_dedup_keys_unique_per_bar(all_signals):
    keys = Counter(s.details["dedup_key"] for s in all_signals)
    assert max(keys.values()) == 1


def test_strategy_coverage(all_signals):
    seen = {s.strategy_id.split("+")[0] for s in all_signals}
    for strat in ("nr7_breakout", "nr7_retest", "trend_pullback", "donchian_breakout",
                  "ema_rsi_trend", "sr_breakout", "bollinger_mean_reversion"):
        assert strat in seen, strat


def test_signal_rate_is_selective(all_signals):
    """Her strateji değerlendirilen mumların küçük bir kısmında sinyal vermeli (aşırı işlem önleme)."""
    evaluated = 30 * len(range(220, 300, 2))
    per = Counter(s.strategy_id.split("+")[0] for s in all_signals)
    for strat, n in per.items():
        assert n / evaluated < 0.06, (strat, n / evaluated)


def test_no_shorts_when_not_allowed():
    for seed in range(10):
        cs, closes = random_series(seed)
        for end in range(220, len(cs), 3):
            ctx = MarketContext("XUSDT", "15m", cs[:end + 1], allow_short=False)
            for cls in IMPLEMENTATIONS.values():
                assert all(s.action != Action.SAT for s in cls().analyze(ctx))


def test_too_few_bars_returns_nothing():
    cs, _ = random_series(1, 150)
    ctx = MarketContext("XUSDT", "15m", cs)
    for bid in ("BOT01", "BOT02"):
        assert IMPLEMENTATIONS[bid]().analyze(ctx) == []
