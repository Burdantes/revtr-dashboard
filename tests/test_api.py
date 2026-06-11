from datetime import date

import pandas as pd

import app as appmod


def _fake_summary():
    return pd.DataFrame([
        {"day": date(2026, 6, 9), "total_measurements": 100, "reaches_count": 80,
         "failed_count": 20, "total_reaching": 80},
        {"day": date(2026, 6, 10), "total_measurements": 120, "reaches_count": 90,
         "failed_count": 30, "total_reaching": 90},
    ])


def _stub_live(monkeypatch):
    """Stub the live (BQ-hitting) parts of /api/health so tests stay hermetic."""
    monkeypatch.setattr(appmod, "fetch_daily_health", lambda d, b: pd.DataFrame())
    monkeypatch.setattr(appmod, "fetch_hourly_health", lambda d, b, h: pd.DataFrame())
    monkeypatch.setattr(appmod, "fetch_hop_quality", lambda d, b: pd.DataFrame())
    monkeypatch.setattr(appmod, "evaluate_health", lambda *a, **k: {"triggered": False})


def test_health_range_reads_summary(client, monkeypatch):
    _stub_live(monkeypatch)
    monkeypatch.setattr(appmod, "fetch_summary", lambda s, e: _fake_summary())
    resp = client.get("/api/health?range=30d")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "daily" in data
    assert len(data["daily"]) == 2
    assert data["daily"][-1]["total_measurements"] == 120
    assert data["range"] == "30d"
