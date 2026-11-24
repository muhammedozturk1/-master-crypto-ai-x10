from decimal import Decimal as D

import pytest

from app.engines.profit_lock import (BREAK_EVEN, EXIT_PENDING, INITIAL, PROFIT_LOCK, TRAILING, PositionView,
                                     evaluate, lock_pct, net_pct, price_for_net_pct)

FEE = D("0.055")


def view(long=True, stop="98", cur=None, peak="0", hw=None, state=INITIAL, strat="nr7_breakout", tick=None):
    entry, qty = D(100), D("0.5")   # 50 USD pozisyon (şartname 14.7 örneği)
    return PositionView(long, qty, entry, qty * entry * FEE / 100, D(stop), D(cur or stop), D(peak),
                        D(hw) if hw else entry, state, strat, FEE, D(tick) if tick else None)


@pytest.mark.parametrize("peak,expected", [
    ("0.4", None), ("0.5", "0"), ("1", "0.5"), ("1.7", "0.8"), ("2", "1.2"), ("3", "2"), ("4.9", "3"),
    ("5", "4"), ("7", "5.6"), ("10", "8.5"), ("12", "10.2"),
])
def test_lock_table(peak, expected):
    r = lock_pct(D(peak))
    assert (r is None and expected is None) or r == D(expected)


def test_price_for_net_pct_roundtrip():
    for long in (True, False):
        p = view(long=long, stop="98" if long else "102")
        for t in ("0", "1.2", "4"):
            px = price_for_net_pct(p, D(t))
            assert net_pct(p, px) == pytest.approx(D(t), abs=D("1e-9"))
        assert (price_for_net_pct(p, D(0)) > p.entry) == long   # başabaş komisyonları kapsar


def price_at(p, pct):
    return price_for_net_pct(p, D(pct))


def test_spec_14_7_example_protected_amounts():
    p = view()
    d2 = evaluate(p, price_at(p, "2"), None)
    assert d2.state == TRAILING and d2.protected_net_usd == pytest.approx(D("0.60"))
    p5 = view(peak="5", state=TRAILING)
    assert evaluate(p5, price_at(p5, "5"), None).protected_net_usd == pytest.approx(D("2.00"))
    p10 = view(peak="10", state=TRAILING)
    assert evaluate(p10, price_at(p10, "10"), None).protected_net_usd == pytest.approx(D("4.25"))


def test_state_progression_and_break_even():
    p = view()
    d = evaluate(p, price_at(p, "0.3"), None)
    assert d.state == INITIAL and d.new_stop is None
    d = evaluate(p, price_at(p, "0.6"), None)
    assert d.state == BREAK_EVEN and d.new_stop == pytest.approx(price_at(p, "0"), abs=D("1e-6"))
    d = evaluate(p, price_at(p, "1.5"), None)
    assert d.state == PROFIT_LOCK and d.new_stop == pytest.approx(price_at(p, "0.8"), abs=D("1e-6"))  # tablo: %1,5 → %0,8


def test_lock_never_moves_back():
    p = view()
    d = evaluate(p, price_at(p, "3"), None)                    # kilit %2
    locked = d.new_stop
    p2 = view(cur=str(locked), peak=str(d.peak_net_pct), hw=str(d.high_water), state=d.state)
    back = evaluate(p2, price_at(p2, "2.5"), None)              # fiyat geri geldi
    assert back.new_stop is None and back.peak_net_pct == d.peak_net_pct


def test_initial_stop_never_widened_short_and_long():
    p = view(stop="98")
    assert evaluate(p, D("99"), D("5")).new_stop is None        # ATR geniş olsa bile stop aşağı inmez
    s = view(long=False, stop="102")
    assert evaluate(s, D("101"), D("5")).new_stop is None


def test_atr_trailing_only_after_activation_and_family():
    p = view(strat="trend_pullback")
    price = price_at(p, "6")                                      # ~106,1
    d = evaluate(p, price, D("1"))
    assert "atr_trailing" in d.components
    assert D(d.components["atr_trailing"]) == pytest.approx(price - D("2.5"))
    assert d.new_stop == pytest.approx(max(D(v) for v in d.components.values()), abs=D("1e-8"))
    early = evaluate(view(), price_at(view(), "1.5"), D("1"))    # %2 altı → trailing yok
    assert "atr_trailing" not in early.components


def test_short_mirror():
    s = view(long=False, stop="102")
    d = evaluate(s, price_for_net_pct(s, D("3")), None)
    assert d.state == TRAILING and d.new_stop < D("102") and d.new_stop == pytest.approx(price_for_net_pct(s, D("2")), abs=D("1e-6"))


def test_breached_lock_triggers_safe_exit():
    p = view(peak="5", state=TRAILING, cur="98")                 # kilit hiç borsaya işlenememiş
    d = evaluate(p, price_at(p, "3.5"), None)                     # fiyat %4 kilidinin altına inmiş
    assert d.exit_now and d.state == EXIT_PENDING and d.new_stop is None


def test_tick_rounding_is_conservative_and_not_backwards():
    p = view(tick="0.1")
    d = evaluate(p, price_at(p, "1.5"), None)
    assert d.new_stop == d.new_stop.quantize(D("0.1")) and d.new_stop <= price_at(p, "0.8")
