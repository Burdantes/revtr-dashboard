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


def test_failures_endpoint(client, monkeypatch):
    df = pd.DataFrame([
        {"day": date(2026, 6, 10), "failed_count": 30,
         "fail_reasons": [{"reason": "NO_RESP", "cnt": 20}, {"reason": "TIMEOUT", "cnt": 10}]},
    ])
    monkeypatch.setattr(appmod, "fetch_summary", lambda s, e: df)
    resp = client.get("/api/failures?range=7d")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["totals"]["NO_RESP"] == 20
    assert data["totals"]["TIMEOUT"] == 10
    assert data["series"][0]["day"] == "2026-06-10"
    assert data["series"][0]["reasons"]["NO_RESP"] == 20


def test_hops_endpoint(client, monkeypatch):
    df = pd.DataFrame([
        {"day": date(2026, 6, 10), "total_hops": 100, "measured_hops": 70,
         "assumed_hops": 30, "intradomain_assumed_hops": 18,
         "interdomain_assumed_hops": 12, "suspect_rr_atlas_count": 5,
         "total_reaching": 90},
    ])
    monkeypatch.setattr(appmod, "fetch_summary", lambda s, e: df)
    resp = client.get("/api/hops?range=7d")
    assert resp.status_code == 200
    pt = resp.get_json()["series"][0]
    assert pt["frac_measured"] == 0.7
    assert pt["frac_assumed"] == 0.3
    assert pt["frac_intradomain_assumed"] == 0.18


def test_rr_responsiveness_endpoint(client, monkeypatch):
    df = pd.DataFrame([
        {"day": date(2026, 6, 10), "rr_probed_targets": 200, "rr_responsive_targets": 50},
    ])
    monkeypatch.setattr(appmod, "fetch_summary", lambda s, e: df)
    resp = client.get("/api/rr_responsiveness?range=7d")
    assert resp.status_code == 200
    pt = resp.get_json()["series"][0]
    assert pt["frac_responsive"] == 0.25
    assert pt["probed"] == 200
