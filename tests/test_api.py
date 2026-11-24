import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app

TOKEN = "test-token-123"


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/t.db", dashboard_token=TOKEN, scanner_enabled=False, runner_enabled=False)
    get_settings.cache_clear()
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_health(client):
    r = client.get("/health").json()
    assert r["status"] == "ok" and r["database"] is True
    assert r["mode"] == "PAPER" and r["bybit_demo"] is True
    assert r["trading_enabled"] is True and r["safe_mode"] is False


def test_ten_bots_registered(client):
    bots = client.get("/bots").json()
    assert [b["bot_id"] for b in bots] == [f"BOT{i:02d}" for i in range(1, 11)]
    assert all(b["enabled"] for b in bots)
    implemented = {b["bot_id"] for b in bots if b["implemented"]}
    assert implemented == {"BOT10"}  # diğer stratejiler Aşama 3'ün devamında


def test_write_requires_token(client):
    assert client.patch("/bots/BOT03", json={"enabled": False}).status_code == 401
    r = client.patch("/bots/BOT03", json={"enabled": False}, headers={"X-Dashboard-Token": TOKEN})
    assert r.status_code == 200 and r.json()["enabled"] is False


def test_guard_bot_cannot_be_disabled(client):
    r = client.patch("/bots/BOT07", json={"enabled": False}, headers={"X-Dashboard-Token": TOKEN})
    assert r.status_code == 409


def test_weight_bounds(client):
    r = client.patch("/bots/BOT01", json={"weight": "5"}, headers={"X-Dashboard-Token": TOKEN})
    assert r.status_code == 422


def test_kill_switch_and_resume_are_audited(client):
    h = {"X-Dashboard-Token": TOKEN}
    assert client.post("/system/kill-switch", json={"reason": "test durdurma"}, headers=h).status_code == 200
    health = client.get("/health").json()
    assert health["trading_enabled"] is False and health["safe_mode"] is True
    assert client.post("/system/resume", json={"confirm": False}, headers=h).status_code == 400
    assert client.post("/system/resume", json={"confirm": True}, headers=h).status_code == 200
    events = [e["event"] for e in client.get("/audit").json()]
    assert events[:2] == ["yeniden_baslatildi", "acil_durdurma"]


def test_writes_disabled_without_token_config(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/t2.db", scanner_enabled=False, runner_enabled=False)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        r = c.post("/system/kill-switch", json={"reason": "deneme"}, headers={"X-Dashboard-Token": ""})
        assert r.status_code == 503
    app.dependency_overrides.clear()


def test_config_hides_secrets(client):
    cfg = client.get("/config").json()
    assert "dashboard_token" not in cfg and "bybit_api_secret" not in cfg
