CREATE TABLE IF NOT EXISTS `nsf-2148275-66720.revtr_dashboard.daily_summary`
(
  day DATE,
  total_measurements INT64,
  reaches_count INT64,
  failed_count INT64,
  fail_reasons ARRAY<STRUCT<reason STRING, cnt INT64>>,
  total_reaching INT64,
  interdomain_count INT64,
  type12_count INT64,
  suspect_rr_atlas_count INT64,
  total_hops INT64,
  measured_hops INT64,
  assumed_hops INT64,
  intradomain_assumed_hops INT64,
  interdomain_assumed_hops INT64,
  suspect_rr_atlas_hops INT64,
  rr_probed_targets INT64,
  rr_responsive_targets INT64,
  updated_at TIMESTAMP
)
PARTITION BY day;
