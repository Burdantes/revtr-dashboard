-- Historical backfill for the PRE-SWITCH era (revtr data before ~May 2025, when
-- hops carried no `asn` and assumed hops were emitted as type 2 instead of being
-- split into intradomain (11) / interdomain (12)).
--
-- Every ASN-derived daily_summary column is recomputed from a RouteViews-resolved
-- per-hop ASN (cron/resolve_hop_asn.py -> revtr_dashboard.hist_ip_asn, keyed by
-- the hop's month) instead of the empty raw `asn`. A type-2 hop is interdomain
-- (12) when the nearest resolved AS BEFORE it differs from the nearest resolved
-- AS AFTER it, intradomain (11) when they match, and assumed-but-unclassified
-- when a neighbor AS is unknown. suspect_rr_atlas is rebuilt from the resolved
-- AS path only (old data has no per-hop geolocation; the live query is AS-only
-- too). Volume / hop-type-count columns match what 03_backfill.sql produced.
--
-- Run AFTER hist_ip_asn is populated for every month in [start_date, end_date]
-- (cron/load_routeviews_historical.py + cron/resolve_hop_asn.py). Idempotent per
-- day (MERGE on day). Edit the two dates before running.
DECLARE start_date DATE DEFAULT DATE('2023-09-06');
DECLARE end_date   DATE DEFAULT DATE('2025-04-30');
DECLARE target DATE;

SET target = start_date;
WHILE target <= end_date DO
  MERGE `nsf-2148275-66720.revtr_dashboard.daily_summary` T
  USING (
    WITH
    gcp_filter AS (
      SELECT NET.IP_FROM_STRING('34.0.0.0') AS lo, NET.IP_FROM_STRING('34.255.255.255') AS hi
    ),
    meas AS (
      SELECT
        FARM_FINGERPRINT(TO_JSON_STRING(r)) AS mid,
        r.raw.fail_reason AS fail_reason,
        (r.raw.stop_reason = 'REACHES') AS is_reach,
        r.raw.revtr_hops AS hops
      FROM `measurement-lab.revtr_raw.revtr1` r, gcp_filter g
      WHERE r.date = target
        AND NOT (NET.IP_FROM_STRING(r.raw.dst) BETWEEN g.lo AND g.hi)
    ),
    -- Measurement-level counts (include 0-hop measurements, unlike the hop CTEs).
    meas_agg AS (
      SELECT
        COUNT(*) AS total_measurements,
        COUNTIF(is_reach) AS reaches_count,
        COUNTIF(fail_reason IS NOT NULL AND NOT is_reach) AS failed_count,
        COUNTIF(is_reach) AS total_reaching
      FROM meas
    ),
    -- Explode hops and attach the RouteViews-resolved ASN for the hop's month.
    hres AS (
      SELECT m.mid, m.is_reach, hp.hop_number, hp.hop_type, ia.asn AS rasn
      FROM meas m, UNNEST(m.hops) hp
      LEFT JOIN `nsf-2148275-66720.revtr_dashboard.hist_ip_asn` ia
        ON ia.ym = FORMAT_DATE('%Y-%m', target) AND ia.hop_ip = hp.hop_ip
    ),
    -- Nearest resolved AS before / after each hop (within the measurement).
    hctx AS (
      SELECT *,
        LAST_VALUE(rasn IGNORE NULLS) OVER (
          PARTITION BY mid ORDER BY hop_number
          ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_asn,
        FIRST_VALUE(rasn IGNORE NULLS) OVER (
          PARTITION BY mid ORDER BY hop_number
          ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS next_asn
      FROM hres
    ),
    hclass AS (
      SELECT mid, is_reach, hop_number, hop_type, rasn,
        -- Hop-COUNT classification: literal hop_type for native 11/12, before/after for type-2.
        (hop_type = 12
         OR (hop_type = 2 AND prev_asn IS NOT NULL AND next_asn IS NOT NULL AND prev_asn != next_asn)) AS is_inter,
        (hop_type = 11
         OR (hop_type = 2 AND prev_asn IS NOT NULL AND next_asn IS NOT NULL AND prev_asn  = next_asn)) AS is_intra,
        -- Per-MEASUREMENT "crosses an AS boundary" (matches sql/02): native 11/12 compares the
        -- hop's own resolved asn to the nearest preceding resolved asn; type-2 (no own asn) uses
        -- before vs after. Used for type12_count / interdomain_count. Inert on the pure type-2 era
        -- (no 11/12 hops there) so the already-rolled type-2 days are unchanged.
        ((hop_type = 12 AND rasn IS NOT NULL AND rasn != prev_asn)
         OR (hop_type = 2 AND prev_asn IS NOT NULL AND next_asn IS NOT NULL AND prev_asn != next_asn)) AS is_inter_cross,
        ((hop_type IN (11, 12) AND rasn IS NOT NULL AND rasn != prev_asn)
         OR (hop_type = 2 AND prev_asn IS NOT NULL AND next_asn IS NOT NULL AND prev_asn != next_asn)) AS is_interdom_cross
      FROM hctx
    ),
    per_meas AS (
      SELECT mid, ANY_VALUE(is_reach) AS is_reach,
        COUNT(*) AS n_hops,
        COUNTIF(hop_type IN (3,4,5,6)) AS n_measured,
        COUNTIF(hop_type IN (2,11,12)) AS n_assumed,
        COUNTIF(is_intra) AS n_intra,
        COUNTIF(is_inter) AS n_inter,
        COUNTIF(hop_type = 4) AS n_type4,
        COUNTIF(hop_type = 1) AS n_type1,
        COUNTIF(hop_type = 3) AS n_type3,
        COUNTIF(hop_type = 5) AS n_type5,
        COUNTIF(hop_type = 6) AS n_type6,
        LOGICAL_OR(is_interdom_cross) AS has_interdomain,
        LOGICAL_OR(is_inter_cross) AS has_type12,
        -- IFNULL to an empty array: ARRAY_AGG(... IGNORE NULLS) returns NULL when a
        -- measurement has no resolved ASN, and the has_loop UDF dereferences .length.
        IFNULL(ARRAY_AGG(CAST(rasn AS STRING) IGNORE NULLS ORDER BY hop_number), ARRAY<STRING>[]) AS as_all,
        IFNULL(ARRAY_AGG(IF(hop_type != 4, CAST(rasn AS STRING), NULL) IGNORE NULLS ORDER BY hop_number), ARRAY<STRING>[]) AS as_no4
      FROM hclass GROUP BY mid
    ),
    per_meas2 AS (
      SELECT *,
        (n_type4 > 0
         AND `nsf-2148275-66720.revtr_dashboard.has_loop`(as_all)
         AND NOT `nsf-2148275-66720.revtr_dashboard.has_loop`(as_no4)) AS is_suspect
      FROM per_meas
    ),
    hop_agg AS (
      SELECT
        COUNTIF(is_reach AND has_interdomain) AS interdomain_count,
        COUNTIF(is_reach AND has_type12) AS type12_count,
        COUNTIF(is_reach AND is_suspect) AS suspect_rr_atlas_count,
        SUM(IF(is_reach, n_hops, 0)) AS total_hops,
        SUM(IF(is_reach, n_measured, 0)) AS measured_hops,
        SUM(IF(is_reach, n_assumed, 0)) AS assumed_hops,
        SUM(IF(is_reach, n_intra, 0)) AS intradomain_assumed_hops,
        SUM(IF(is_reach, n_inter, 0)) AS interdomain_assumed_hops,
        SUM(IF(is_reach AND is_suspect, n_type4, 0)) AS suspect_rr_atlas_hops,
        SUM(IF(is_reach, n_type1, 0)) AS type1_hops,
        SUM(IF(is_reach, n_type3, 0)) AS type3_hops,
        SUM(IF(is_reach, n_type4, 0)) AS type4_hops,
        SUM(IF(is_reach, n_type5, 0)) AS type5_hops,
        SUM(IF(is_reach, n_type6, 0)) AS type6_hops
      FROM per_meas2
    ),
    fr AS (
      SELECT ARRAY_AGG(STRUCT(fail_reason AS reason, cnt) ORDER BY cnt DESC) AS fail_reasons
      FROM (
        SELECT fail_reason, COUNT(*) AS cnt
        FROM meas
        WHERE fail_reason IS NOT NULL AND NOT is_reach
        GROUP BY fail_reason
      )
    ),
    rr AS (
      SELECT
        COUNT(DISTINCT p.raw.dst) AS rr_probed_targets,
        COUNT(DISTINCT IF(ARRAY_LENGTH(p.raw.record_route) > 0, p.raw.dst, NULL)) AS rr_responsive_targets
      FROM `measurement-lab.revtr_raw.ping1` p, gcp_filter g
      WHERE p.date = target
        AND IFNULL(p.raw.spoofed, 0) = 0
        AND NOT (NET.IP_FROM_STRING(p.raw.dst) BETWEEN g.lo AND g.hi)
    )
    SELECT
      target AS day,
      m.total_measurements, m.reaches_count, m.failed_count,
      fr.fail_reasons, m.total_reaching, h.interdomain_count, h.type12_count,
      h.suspect_rr_atlas_count, h.total_hops, h.measured_hops, h.assumed_hops,
      h.intradomain_assumed_hops, h.interdomain_assumed_hops, h.suspect_rr_atlas_hops,
      rr.rr_probed_targets, rr.rr_responsive_targets,
      CURRENT_TIMESTAMP() AS updated_at,
      h.type1_hops, h.type3_hops, h.type4_hops, h.type5_hops, h.type6_hops
    FROM meas_agg m CROSS JOIN hop_agg h CROSS JOIN fr CROSS JOIN rr
  ) S
  ON T.day = S.day
  WHEN MATCHED THEN UPDATE SET
    total_measurements = S.total_measurements, reaches_count = S.reaches_count,
    failed_count = S.failed_count, fail_reasons = S.fail_reasons,
    total_reaching = S.total_reaching, interdomain_count = S.interdomain_count,
    type12_count = S.type12_count, suspect_rr_atlas_count = S.suspect_rr_atlas_count,
    total_hops = S.total_hops, measured_hops = S.measured_hops, assumed_hops = S.assumed_hops,
    intradomain_assumed_hops = S.intradomain_assumed_hops,
    interdomain_assumed_hops = S.interdomain_assumed_hops,
    suspect_rr_atlas_hops = S.suspect_rr_atlas_hops,
    rr_probed_targets = S.rr_probed_targets, rr_responsive_targets = S.rr_responsive_targets,
    updated_at = S.updated_at,
    type1_hops = S.type1_hops, type3_hops = S.type3_hops, type4_hops = S.type4_hops,
    type5_hops = S.type5_hops, type6_hops = S.type6_hops
  WHEN NOT MATCHED THEN INSERT ROW;

  SET target = DATE_ADD(target, INTERVAL 1 DAY);
END WHILE;
