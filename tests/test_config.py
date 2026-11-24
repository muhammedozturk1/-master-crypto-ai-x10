from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import RiskLimits, Settings, TradingMode, MarketType


def test_defaults_match_spec():
    r = RiskLimits()
    assert r.risk_budget_usd == Decimal("1")  # 200 × %0,5
    assert r.daily_loss_limit_usd == Decimal("4")
    assert r.max_open_positions == 2
    assert r.leverage == 2


@pytest.mark.parametrize("field,value", [
    ("risk_per_trade_pct", Decimal("1")),
    ("daily_loss_pct", Decimal("3")),
    ("max_drawdown_pct", Decimal("15")),
    ("max_open_positions", 3),
    ("leverage", 5),
    ("min_reward_risk", Decimal("1.5")),
    ("martingale_allowed", True),
])
def test_hard_caps_reject(field, value):
    with pytest.raises(ValidationError):
        RiskLimits(**{field: value})


def test_live_mode_requires_confirmation():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mode=TradingMode.AUTONOMOUS_LIVE)
    with pytest.raises(ValidationError):  # onay var ama demo hesap
        Settings(_env_file=None, mode=TradingMode.AUTONOMOUS_LIVE, live_trading_confirmed=True)
    s = Settings(_env_file=None, mode=TradingMode.AUTONOMOUS_LIVE, live_trading_confirmed=True, bybit_demo=False)
    assert s.bybit_rest_url == "https://api.bybit.com"


def test_spot_forbids_leverage():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, market_type=MarketType.SPOT)  # varsayılan kaldıraç 2
    s = Settings(_env_file=None, market_type=MarketType.SPOT, risk=RiskLimits(leverage=1))
    assert s.risk.leverage == 1


def test_default_is_paper_demo():
    s = Settings(_env_file=None)
    assert s.mode == TradingMode.PAPER
    assert s.bybit_rest_url == "https://api-demo.bybit.com"


def test_render_postgres_url_converted():
    s = Settings(_env_file=None, database_url="postgres://u:p@h:5432/db")
    assert s.sqlalchemy_url == "postgresql+psycopg://u:p@h:5432/db"


def test_secrets_not_in_public_summary():
    s = Settings(_env_file=None, bybit_api_key="KEY123", bybit_api_secret="SECRET", dashboard_token="TOK")
    dumped = str(s.public_summary())
    assert "KEY123" not in dumped and "SECRET" not in dumped and "TOK" not in dumped
    assert s.public_summary()["api_key_configured"] is True
