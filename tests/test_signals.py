from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from app.domain.signals import Action, Signal

BASE = dict(bot_id="BOT10", strategy_id="nr7_breakout", symbol="BTCUSDT", timeframe="15m", confidence=80, reason="test")


def test_valid_long():
    s = Signal(**BASE, action=Action.AL, entry_low=D("100"), entry_high=D("101"), stop_loss=D("98"), take_profit=D("105.5"))
    assert s.gross_reward_risk() == D("2.00")


def test_long_with_stop_above_entry_rejected():
    with pytest.raises(ValidationError):
        Signal(**BASE, action=Action.AL, entry_low=D("100"), entry_high=D("101"), stop_loss=D("102"), take_profit=D("110"))


def test_short_levels():
    Signal(**BASE, action=Action.SAT, entry_low=D("100"), entry_high=D("101"), stop_loss=D("103"), take_profit=D("95"))
    with pytest.raises(ValidationError):
        Signal(**BASE, action=Action.SAT, entry_low=D("100"), entry_high=D("101"), stop_loss=D("99"), take_profit=D("95"))


def test_trade_signal_requires_levels():
    with pytest.raises(ValidationError):
        Signal(**BASE, action=Action.AL)


def test_wait_needs_no_levels_and_expires():
    s = Signal(**BASE, action=Action.BEKLE, valid_for_seconds=60)
    assert not s.is_expired()
    assert s.is_expired(datetime.now(timezone.utc) + timedelta(seconds=61))


@pytest.mark.parametrize("bot_id", ["BOT00", "BOT11", "bot01", "X"])
def test_bot_id_format(bot_id):
    with pytest.raises(ValidationError):
        Signal(**{**BASE, "bot_id": bot_id}, action=Action.BEKLE)


def test_confidence_range():
    with pytest.raises(ValidationError):
        Signal(**{**BASE, "confidence": 101}, action=Action.BEKLE)
