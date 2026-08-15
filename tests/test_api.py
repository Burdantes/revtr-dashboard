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
    import numpy as np
    # Mirror real BigQuery output: repeated struct -> numpy.ndarray of dicts,
    # and a zero-failure day -> NULL array (None/NaN).
    # Raw revtr reason strings get bucketed into primary categories:
    # GAPLIMIT->"Gap limit reached", timed out->"Timed out", anything else
    # (e.g. no-socket)->"Probe/system error".
    df = pd.DataFrame([
        {"day": date(2026, 6, 9), "failed_count": 0, "fail_reasons": None},
        {"day": date(2026, 6, 10), "failed_count": 30,
         "fail_reasons": np.array([
             {"reason": "Traceroute didn't reach destination  GAPLIMIT", "cnt": 20},
             {"reason": "Traceroute timed out ", "cnt": 7},
             {"reason": " No socket found ", "cnt": 3}], dtype=object)},
    ])
    monkeypatch.setattr(appmod, "fetch_summary", lambda s, e: df)
    resp = client.get("/api/failures?range=7d")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["totals"]["Gap limit reached"] == 20
    assert data["totals"]["Timed out"] == 7
    assert data["totals"]["Probe/system error"] == 3
    assert data["granularity"] == "day"
    assert data["series"][-1]["day"] == "2026-06-10"
    assert data["series"][-1]["reasons"]["Gap limit reached"] == 20
    assert data["series"][0]["reasons"] == {}  # zero-failure day


def test_hops_endpoint(client, monkeypatch):
    df = pd.DataFrame([
        {"day": date(2026, 6, 10), "total_hops": 100, "measured_hops": 70,
         "assumed_hops": 30, "intradomain_assumed_hops": 18,
         "interdomain_assumed_hops": 12, "suspect_rr_atlas_count": 5,
         "total_reaching": 90,
         "type1_hops": 9, "type3_hops": 20, "type4_hops": 10,
         "type5_hops": 25, "type6_hops": 15},
    ])
    monkeypatch.setattr(appmod, "fetch_summary", lambda s, e: df)
    resp = client.get("/api/hops?range=7d")
    assert resp.status_code == 200
    body = resp.get_json()
    pt = body["series"][0]
    assert pt["frac_measured"] == 0.7
    assert pt["frac_assumed"] == 0.3
    assert pt["frac_intradomain_assumed"] == 0.18
    assert pt["frac_interdomain_assumed"] == 0.12
    # per-type fractions present and correct
    assert pt["frac_type5"] == 0.25
    assert pt["frac_type1"] == 0.09
    # interpretation labels exposed for the chart
    assert "type5" in body["labels"]
    assert "Record-Route" in body["labels"]["type5"]


def test_hops_endpoint_tolerates_null_pertype(client, monkeypatch):
    # A day not yet re-rolled: per-type columns are SQL NULL. BigQuery's
    # nullable INT64 dtype surfaces these as pandas NA (NAType), so build the
    # per-type columns as Int64 with pd.NA to match production exactly.
    df = pd.DataFrame([
        {"day": date(2026, 6, 10), "total_hops": 100, "measured_hops": 70,
         "assumed_hops": 30, "intradomain_assumed_hops": 18,
         "interdomain_assumed_hops": 12, "suspect_rr_atlas_count": 5,
         "total_reaching": 90},
    ])
    for col in ("type1_hops", "type3_hops", "type4_hops", "type5_hops", "type6_hops"):
        df[col] = pd.array([pd.NA], dtype="Int64")
    monkeypatch.setattr(appmod, "fetch_summary", lambda s, e: df)
    resp = client.get("/api/hops?range=7d")
    assert resp.status_code == 200
    assert resp.get_json()["series"][0]["frac_type5"] == 0.0


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


def test_hop_quality_uses_renamed_keys(client, monkeypatch):
    df = pd.DataFrame([
        {"day": date(2026, 6, 10), "total_reaching": 100, "interdomain_count": 10,
         "type12_count": 8, "suspect_rr_atlas_count": 5,
         "frac_interdomain": 0.1, "frac_type12": 0.08, "frac_suspect_rr_atlas": 0.05},
    ])
    monkeypatch.setattr(appmod, "fetch_hop_quality", lambda d, b: df)
    resp = client.get("/api/hop_quality")
    assert resp.status_code == 200
    row0 = resp.get_json()["daily"][0]
    assert "frac_suspect_rr_atlas" in row0
    assert "frac_fishy_type4" not in row0
    assert row0["suspect_rr_atlas_count"] == 5


def test_categorize_failure_buckets():
    # Real revtr reason strings collapse into 5 primary categories.
    cat = appmod._categorize_failure
    assert cat("Traceroute didn't reach destination  GAPLIMIT") == "Gap limit reached"
    assert cat("Traceroute didn't reach destination  GAPLIMIT. Could not find "
               "responsive addresses in the destination AS") == "Gap limit reached"
    assert cat(" Traceroute timed out ") == "Timed out"
    assert cat("Traceroute didn't reach destination  UNREACH") == "Unreachable"
    assert cat("Traceroute didn't reach destination  LOOP") == "Routing loop"
    assert cat(" No socket found ") == "Probe/system error"
    assert cat("rpc error: code = Unavailable") == "Probe/system error"


def test_failures_weekly_granularity(client, monkeypatch):
    import numpy as np
    # Daily rows spanning two ISO weeks; range=1y -> weekly buckets.
    fr = np.array([{"reason": "Traceroute timed out ", "cnt": 5}], dtype=object)
    df = pd.DataFrame([
        {"day": date(2026, 6, 2), "failed_count": 5, "fail_reasons": fr},
        {"day": date(2026, 6, 3), "failed_count": 5, "fail_reasons": fr},
        {"day": date(2026, 6, 16), "failed_count": 5, "fail_reasons": fr},
    ])
    monkeypatch.setattr(appmod, "fetch_summary", lambda s, e: df)
    data = client.get("/api/failures?range=1y").get_json()
    assert data["granularity"] == "week"
    # Two distinct weeks -> two buckets; the first week summed two days (10).
    assert len(data["series"]) == 2
    assert data["series"][0]["failed_count"] == 10
    assert data["series"][0]["reasons"]["Timed out"] == 10
    assert data["totals"]["Timed out"] == 15  # 5+5+5 across both weeks


def test_as_distribution_endpoint(client, monkeypatch):
    # Per-destination-AS rows (as fetch_as_test_distribution returns them: asn is
    # a string, "unmapped" for dst IPs with no prefix match). The endpoint derives
    # frac_reached (reaches/tests) and frac_interdomain (interdomain/reaching).
    appmod._as_dist_cache.clear()  # endpoint caches the snapshot; keep test hermetic
    # One row per (AS, day): the precomputed as_distribution_7d shape.
    df = pd.DataFrame([
        {"asn": "7018", "as_name": "AT&T", "is_gcp": False, "day": "2026-06-12",
         "unique_ips": 42, "tests": 1000, "reaches": 100, "interdomain_count": 50,
         "computed_at": "2026-06-14T13:30:00Z"},
        {"asn": "gcp", "as_name": "GCP clients (34.0.0.0/8)", "is_gcp": True,
         "day": "2026-06-12", "unique_ips": 7, "tests": 500, "reaches": 480,
         "interdomain_count": 20, "computed_at": "2026-06-14T13:30:00Z"},
        {"asn": "unmapped", "as_name": None, "is_gcp": False, "day": "2026-06-11",
         "unique_ips": 9, "tests": 200, "reaches": 80, "interdomain_count": 10,
         "computed_at": "2026-06-14T13:30:00Z"},
    ])
    monkeypatch.setattr(appmod, "fetch_as_distribution", lambda: df)
    data = client.get("/api/as_distribution").get_json()
    assert data["row_count"] == 3
    assert data["window"]["days"] == 2                   # two distinct days
    assert data["window"]["start"] == "2026-06-11"
    assert data["window"]["end"] == "2026-06-12"
    assert data["computed_at"] == "2026-06-14T13:30:00Z"
    assert data["cached"] is False
    rows = {(r["asn"], r["day"]): r for r in data["rows"]}
    assert rows[("7018", "2026-06-12")]["as_name"] == "AT&T"
    assert rows[("7018", "2026-06-12")]["unique_ips"] == 42
    assert rows[("7018", "2026-06-12")]["frac_reached"] == 0.1      # 100 / 1000
    assert rows[("7018", "2026-06-12")]["frac_interdomain"] == 0.5  # 50 / 100 reaching
    assert rows[("gcp", "2026-06-12")]["is_gcp"] is True            # GCP shown as its own row
    assert rows[("unmapped", "2026-06-11")]["as_name"] is None      # NULL ASName -> null
    assert rows[("unmapped", "2026-06-11")]["frac_reached"] == 0.4  # 80 / 200


def test_as_distribution_caches(client, monkeypatch):
    # Second call within TTL must be served from cache (cached=True) without
    # re-invoking the (expensive) fetch.
    appmod._as_dist_cache.clear()
    calls = {"n": 0}

    def _fake():
        calls["n"] += 1
        return pd.DataFrame([{"asn": "1", "as_name": "Example", "is_gcp": False,
                              "day": "2026-06-12", "unique_ips": 3, "tests": 10,
                              "reaches": 5, "interdomain_count": 2,
                              "computed_at": "2026-06-14T13:30:00Z"}])

    monkeypatch.setattr(appmod, "fetch_as_distribution", _fake)
    first = client.get("/api/as_distribution").get_json()
    second = client.get("/api/as_distribution").get_json()
    assert first["cached"] is False
    assert second["cached"] is True
    assert calls["n"] == 1  # fetch ran only once


def test_hops_skips_no_data_periods(client, monkeypatch):
    # A revtr-down day (total_hops 0/NULL) must be omitted, not shown as 100% "Other".
    df = pd.DataFrame([
        {"day": date(2026, 6, 9), "total_hops": 0, "measured_hops": 0, "assumed_hops": 0,
         "intradomain_assumed_hops": 0, "interdomain_assumed_hops": 0,
         "suspect_rr_atlas_count": 0, "total_reaching": 0,
         "type1_hops": 0, "type3_hops": 0, "type4_hops": 0, "type5_hops": 0, "type6_hops": 0},
        {"day": date(2026, 6, 10), "total_hops": 100, "measured_hops": 70, "assumed_hops": 30,
         "intradomain_assumed_hops": 18, "interdomain_assumed_hops": 12,
         "suspect_rr_atlas_count": 5, "total_reaching": 90,
         "type1_hops": 10, "type3_hops": 20, "type4_hops": 10, "type5_hops": 25, "type6_hops": 15},
    ])
    monkeypatch.setattr(appmod, "fetch_summary", lambda s, e: df)
    body = client.get("/api/hops?range=7d").get_json()
    days = [p["day"] for p in body["series"]]
    assert "2026-06-09" not in days   # no-data day omitted
    assert "2026-06-10" in days


# ---------------------------------------------------------------------------
# Severity ladder: hard failures must outrank flag count (see alerting.classify)
# ---------------------------------------------------------------------------


def _health_frame(day, total, reaches, failed, baseline_days=7):
    """Target day plus a flat baseline window before it."""
    rows = []
    for i in range(baseline_days, 0, -1):
        d = date.fromordinal(day.toordinal() - i)
        rows.append({"day": d, "total_measurements": 40000,
                     "reaches_count": 30000, "failed_count": 4000})
    rows.append({"day": day, "total_measurements": total,
                 "reaches_count": reaches, "failed_count": failed})
    df = pd.DataFrame(rows)
    df["reach_rate"] = df["reaches_count"] / df["total_measurements"].clip(lower=1)
    df["fail_rate"] = df["failed_count"] / df["total_measurements"].clip(lower=1)
    return df


def test_missing_day_is_critical_not_warning():
    """A total outage raises one condition; it must not rank below 3 wobbles."""
    day = date(2026, 8, 14)
    result = appmod.evaluate_health(_health_frame(day, 1, 1, 0).iloc[:-1], day)
    assert result["severity"] == "critical"
    assert result["hard_failures"] == [f"No data for {day}"]


def test_volume_collapse_is_a_hard_failure():
    day = date(2026, 8, 14)
    # 1% of baseline volume: the system is down, not degraded.
    df = _health_frame(day, 400, 300, 40)
    result = appmod.evaluate_health(df, day)
    assert result["severity"] == "critical"
    assert any("Volume drop" in r for r in result["hard_failures"])


def test_moderate_volume_drop_is_only_a_warning():
    day = date(2026, 8, 14)
    # 40% of baseline: below the 50% alert line but above the 20% collapse line.
    df = _health_frame(day, 16000, 12000, 1600)
    result = appmod.evaluate_health(df, day)
    assert result["hard_failures"] == []
    assert result["severity"] == "warning"


def test_healthy_day_is_ok():
    day = date(2026, 8, 14)
    df = _health_frame(day, 40000, 30000, 4000)
    result = appmod.evaluate_health(df, day)
    assert result["severity"] == "ok"
    assert result["triggered"] is False


def _hourly_frame(day, today_total, baseline_days=2, baseline_total=490000, include_today=True):
    rows = []
    for i in range(baseline_days, 0, -1):
        d = date.fromordinal(day.toordinal() - i)
        rows.append({"day": d, "total_measurements": baseline_total,
                     "reaches_count": 200000, "failed_count": 250000})
    if include_today:
        rows.append({"day": day, "total_measurements": today_total,
                     "reaches_count": 1, "failed_count": 1})
    return pd.DataFrame(rows)


def test_zero_volume_this_hour_is_a_hard_failure_not_a_skipped_check():
    """The hour-aware check must not vanish when today's row is absent.

    A missing today-row means zero measurements so far, which is the most
    severe case -- exactly when the check is most needed. Observed live on
    2026-08-15, where hours 00-03 UTC had no data and the check silently
    no-opped instead of firing.
    """
    day = date(2026, 8, 15)
    daily = _health_frame(day, 40000, 30000, 4000)
    hourly = _hourly_frame(day, 0, include_today=False)
    result = appmod.evaluate_health(daily, day, hourly_df=hourly)
    assert result["hourly_volume"]["today"] == 0
    assert any("Volume drop" in r for r in result["hard_failures"])
    assert result["severity"] == "critical"


def test_volume_check_falls_back_to_daily_when_hourly_baseline_missing():
    """Never skip the volume check outright: fall back rather than no-op."""
    day = date(2026, 8, 15)
    daily = _health_frame(day, 400, 300, 40)  # 1% of baseline
    hourly = _hourly_frame(day, 100, baseline_days=0)  # today only, no baseline
    result = appmod.evaluate_health(daily, day, hourly_df=hourly)
    assert any("Volume drop" in r for r in result["reasons"])
    assert result["severity"] == "critical"


def test_baseline_days_reports_days_actually_present_not_the_window():
    """revtr_raw had a 6-day gap (2026-08-07..12).

    Reporting 'prior 7 days' when only 2 are present overstates the baseline.
    """
    day = date(2026, 8, 15)
    daily = _health_frame(day, 40000, 30000, 4000, baseline_days=2)
    result = appmod.evaluate_health(daily, day)
    assert result["baseline_days"] == 2
