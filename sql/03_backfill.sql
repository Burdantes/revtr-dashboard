-- Backfill daily_summary over [start_date, end_date]. Idempotent per day.
-- GENERATED from 02_rollup_daily.sql by gen_rollups.py (do not edit by hand).
-- Edit the two dates below before running. Each day scans ~0.9 GB.
DECLARE start_date DATE DEFAULT DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY);
DECLARE end_date   DATE DEFAULT DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY);
DECLARE target DATE;

SET target = start_date;
WHILE target <= end_date DO
  MERGE `nsf-2148275-66720.revtr_dashboard.daily_summary` T
  USING (
    WITH gcp_filter AS (
      SELECT NET.IP_FROM_STRING('34.0.0.0') AS lo, NET.IP_FROM_STRING('34.255.255.255') AS hi
    ),
    -- ---- revtr1 side: per-measurement classification ----
    meas AS (
      SELECT
        r.raw.stop_reason AS stop_reason,
        r.raw.fail_reason AS fail_reason,
        -- AS path (all hops) / (excluding type-4)
        ARRAY(SELECT CAST(h.asn AS STRING) FROM UNNEST(r.raw.revtr_hops) h
              WHERE h.asn IS NOT NULL ORDER BY h.hop_number) AS as_all,
        ARRAY(SELECT CAST(h.asn AS STRING) FROM UNNEST(r.raw.revtr_hops) h
              WHERE h.asn IS NOT NULL AND h.hop_type != 4 ORDER BY h.hop_number) AS as_no4,
        -- geographic path key (all hops) / (excluding type-4)
        ARRAY(SELECT CONCAT(CAST(ROUND(h.geolocation_ipinfo.lat,1) AS STRING), ',',
                            CAST(ROUND(h.geolocation_ipinfo.lng,1) AS STRING))
              FROM UNNEST(r.raw.revtr_hops) h
              WHERE h.geolocation_ipinfo.lat IS NOT NULL AND h.geolocation_ipinfo.lng IS NOT NULL
              ORDER BY h.hop_number) AS geo_all,
        ARRAY(SELECT CONCAT(CAST(ROUND(h.geolocation_ipinfo.lat,1) AS STRING), ',',
                            CAST(ROUND(h.geolocation_ipinfo.lng,1) AS STRING))
              FROM UNNEST(r.raw.revtr_hops) h
              WHERE h.geolocation_ipinfo.lat IS NOT NULL AND h.geolocation_ipinfo.lng IS NOT NULL
                AND h.hop_type != 4 ORDER BY h.hop_number) AS geo_no4,
        -- hop-type level counts (this measurement). Type-2 hops are the OLD
        -- (pre-~May-2025) "symmetry assumption" hops, which carry no asn/geo of
        -- their own; the pipeline later split these into intradomain (11) /
        -- interdomain (12). We reconstruct that split from the nearest known-AS
        -- hop BEFORE vs AFTER each type-2 hop: before-AS != after-AS -> 12;
        -- before-AS = after-AS -> 11; a neighbor AS missing (path end) -> assumed
        -- but unclassified (counted in n_assumed only, neither 11 nor 12). The
        -- (!=)/(=) is NULL-safe: a NULL neighbor yields NULL -> not counted.
        -- These branches are inert on post-switch data (no hop_type=2 present).
        (SELECT COUNT(*) FROM UNNEST(r.raw.revtr_hops) h) AS n_hops,
        (SELECT COUNTIF(h.hop_type IN (3,4,5,6)) FROM UNNEST(r.raw.revtr_hops) h) AS n_measured,
        (SELECT COUNTIF(h.hop_type IN (11,12) OR h.hop_type = 2) FROM UNNEST(r.raw.revtr_hops) h) AS n_assumed,
        (SELECT COUNTIF(
           h.hop_type = 11
           OR (h.hop_type = 2 AND
               (SELECT b.asn FROM UNNEST(r.raw.revtr_hops) b
                WHERE b.hop_number < h.hop_number AND b.asn IS NOT NULL
                ORDER BY b.hop_number DESC LIMIT 1)
             = (SELECT a.asn FROM UNNEST(r.raw.revtr_hops) a
                WHERE a.hop_number > h.hop_number AND a.asn IS NOT NULL
                ORDER BY a.hop_number ASC LIMIT 1))
         ) FROM UNNEST(r.raw.revtr_hops) h) AS n_intra,
        (SELECT COUNTIF(
           h.hop_type = 12
           OR (h.hop_type = 2 AND
               (SELECT b.asn FROM UNNEST(r.raw.revtr_hops) b
                WHERE b.hop_number < h.hop_number AND b.asn IS NOT NULL
                ORDER BY b.hop_number DESC LIMIT 1)
            != (SELECT a.asn FROM UNNEST(r.raw.revtr_hops) a
                WHERE a.hop_number > h.hop_number AND a.asn IS NOT NULL
                ORDER BY a.hop_number ASC LIMIT 1))
         ) FROM UNNEST(r.raw.revtr_hops) h) AS n_inter,
        (SELECT COUNTIF(h.hop_type = 4) FROM UNNEST(r.raw.revtr_hops) h) AS n_type4,
        (SELECT COUNTIF(h.hop_type = 1) FROM UNNEST(r.raw.revtr_hops) h) AS n_type1,
        (SELECT COUNTIF(h.hop_type = 3) FROM UNNEST(r.raw.revtr_hops) h) AS n_type3,
        (SELECT COUNTIF(h.hop_type = 5) FROM UNNEST(r.raw.revtr_hops) h) AS n_type5,
        (SELECT COUNTIF(h.hop_type = 6) FROM UNNEST(r.raw.revtr_hops) h) AS n_type6,
        EXISTS(
          SELECT 1 FROM UNNEST(r.raw.revtr_hops) h
          WHERE (h.hop_type = 12 AND h.asn IS NOT NULL
                 AND h.asn != IFNULL((SELECT h2.asn FROM UNNEST(r.raw.revtr_hops) h2
                                      WHERE h2.hop_number < h.hop_number AND h2.asn IS NOT NULL
                                      ORDER BY h2.hop_number DESC LIMIT 1), h.asn))
             OR (h.hop_type = 2 AND
                 (SELECT b.asn FROM UNNEST(r.raw.revtr_hops) b
                  WHERE b.hop_number < h.hop_number AND b.asn IS NOT NULL
                  ORDER BY b.hop_number DESC LIMIT 1)
              != (SELECT a.asn FROM UNNEST(r.raw.revtr_hops) a
                  WHERE a.hop_number > h.hop_number AND a.asn IS NOT NULL
                  ORDER BY a.hop_number ASC LIMIT 1))
        ) AS has_type12,
        EXISTS(
          SELECT 1 FROM UNNEST(r.raw.revtr_hops) h
          WHERE (h.hop_type IN (11,12) AND h.asn IS NOT NULL
                 AND h.asn != IFNULL((SELECT h2.asn FROM UNNEST(r.raw.revtr_hops) h2
                                      WHERE h2.hop_number < h.hop_number AND h2.asn IS NOT NULL
                                      ORDER BY h2.hop_number DESC LIMIT 1), h.asn))
             OR (h.hop_type = 2 AND
                 (SELECT b.asn FROM UNNEST(r.raw.revtr_hops) b
                  WHERE b.hop_number < h.hop_number AND b.asn IS NOT NULL
                  ORDER BY b.hop_number DESC LIMIT 1)
              != (SELECT a.asn FROM UNNEST(r.raw.revtr_hops) a
                  WHERE a.hop_number > h.hop_number AND a.asn IS NOT NULL
                  ORDER BY a.hop_number ASC LIMIT 1))
        ) AS has_interdomain
      FROM `measurement-lab.revtr_raw.revtr1` r, gcp_filter g
      WHERE r.date = target
        AND NOT (NET.IP_FROM_STRING(r.raw.dst) BETWEEN g.lo AND g.hi)
    ),
    meas_flagged AS (
      SELECT
        *,
        (stop_reason = 'REACHES') AS is_reach,
        -- type-4-induced loop: loop exists with all hops AND clears without type-4 hops
        (n_type4 > 0 AND (
           (`nsf-2148275-66720.revtr_dashboard.has_loop`(as_all)
              AND NOT `nsf-2148275-66720.revtr_dashboard.has_loop`(as_no4))
           OR
           (`nsf-2148275-66720.revtr_dashboard.has_loop`(geo_all)
              AND NOT `nsf-2148275-66720.revtr_dashboard.has_loop`(geo_no4))
        )) AS is_suspect
      FROM meas
    ),
    revtr_agg AS (
      SELECT
        target AS day,
        COUNT(*) AS total_measurements,
        COUNTIF(is_reach) AS reaches_count,
        COUNTIF(fail_reason IS NOT NULL AND NOT is_reach) AS failed_count,
        COUNTIF(is_reach) AS total_reaching,
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
      FROM meas_flagged
    ),
    fr AS (
      SELECT ARRAY_AGG(STRUCT(fail_reason AS reason, cnt) ORDER BY cnt DESC) AS fail_reasons
      FROM (
        SELECT fail_reason, COUNT(*) AS cnt
        FROM meas_flagged
        WHERE fail_reason IS NOT NULL AND NOT is_reach
        GROUP BY fail_reason
      )
    ),
    -- ---- ping1 side: RR responsiveness (non-spoofed) ----
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
      a.day, a.total_measurements, a.reaches_count, a.failed_count,
      fr.fail_reasons, a.total_reaching, a.interdomain_count, a.type12_count,
      a.suspect_rr_atlas_count, a.total_hops, a.measured_hops, a.assumed_hops,
      a.intradomain_assumed_hops, a.interdomain_assumed_hops, a.suspect_rr_atlas_hops,
      rr.rr_probed_targets, rr.rr_responsive_targets,
      CURRENT_TIMESTAMP() AS updated_at,
      a.type1_hops, a.type3_hops, a.type4_hops, a.type5_hops, a.type6_hops
    FROM revtr_agg a CROSS JOIN fr CROSS JOIN rr
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
