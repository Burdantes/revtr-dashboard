-- Precompute the "Tests per Destination AS" table for the dashboard panel,
-- broken out DAY BY DAY over the last 7 full days (one row per AS per day).
-- Runs daily on the VM cron (see cron/as_dist_runner.py): the longest-prefix
-- match against the full RouteViews table (~1.1M prefixes) takes ~3 min, which
-- exceeds the dashboard's 90s worker timeout, so /api/as_distribution just reads
-- this small result table.
--
-- Destination AS is resolved by longest-prefix-matching raw.dst against
-- hopannotation2 prefixes (which carry AS names) UNION the RouteViews prefix
-- table (full routing-table coverage); on equal mask hopannotation2 wins so we
-- keep its names. RouteViews-only ASNs get a name from a hopannotation2
-- ASN->name lookup when available. The mapping is date-independent (an IP's AS
-- is the same every day), so it is computed once over distinct dst IPs.
--
-- Billed to nsf-2148275-66720 (it CREATEs a table there; a job billed to
-- measurement-lab cannot create tables cross-project). It reads measurement-lab
-- (revtr_raw / ndt_raw) cross-project. GCP-range (34.0.0.0/8) destinations are
-- aggregated into one labeled 'gcp' row per day and are excluded from every
-- other dashboard panel. Limited to the top 500 ASes by 7-day total tests.
CREATE OR REPLACE TABLE `nsf-2148275-66720.revtr_dashboard.as_distribution_7d` AS
WITH
hop_prefix AS (
  SELECT DISTINCT
    NET.IP_FROM_STRING(REGEXP_EXTRACT(raw.Annotations.Network.CIDR, r'^(.*)/')) AS net_bin,
    CAST(REGEXP_EXTRACT(raw.Annotations.Network.CIDR, r'/(\d+)$') AS INT64) AS mask,
    raw.Annotations.Network.ASNumber AS asn,
    raw.Annotations.Network.ASName AS asname
  FROM `measurement-lab.ndt_raw.hopannotation2`
  WHERE date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
    AND raw.Annotations.Network.CIDR IS NOT NULL AND raw.Annotations.Network.ASNumber IS NOT NULL
),
prefix_all AS (
  SELECT net_bin, mask, asn, asname, 1 AS prio FROM hop_prefix
    WHERE mask BETWEEN 1 AND 32 AND BYTE_LENGTH(net_bin) = 4
  UNION ALL
  SELECT NET.IP_FROM_STRING(network), mask, asn, CAST(NULL AS STRING), 0 AS prio
    FROM `nsf-2148275-66720.revtr_dashboard.routeviews_pfx2as`
    WHERE mask BETWEEN 1 AND 32
),
asn_names AS (
  SELECT asn, ANY_VALUE(asname) AS asname FROM hop_prefix WHERE asname IS NOT NULL GROUP BY asn
),
masks AS (SELECT DISTINCT mask FROM prefix_all),
per_dst_day AS (
  SELECT t.raw.dst AS dst, t.date AS day,
    (NET.IP_FROM_STRING(t.raw.dst) BETWEEN NET.IP_FROM_STRING('34.0.0.0')
       AND NET.IP_FROM_STRING('34.255.255.255')) AS is_gcp,
    COUNT(*) AS tests,
    COUNTIF(t.raw.stop_reason = 'REACHES') AS reaches,
    COUNTIF(t.raw.stop_reason = 'REACHES' AND EXISTS(
      SELECT 1 FROM UNNEST(t.raw.revtr_hops) h
      WHERE h.hop_type = 12 AND h.asn IS NOT NULL
        AND h.asn != IFNULL((SELECT h2.asn FROM UNNEST(t.raw.revtr_hops) h2
              WHERE h2.hop_number < h.hop_number AND h2.asn IS NOT NULL
              ORDER BY h2.hop_number DESC LIMIT 1), h.asn))) AS interdomain
  FROM `measurement-lab.revtr_raw.revtr1` t
  WHERE t.date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) AND t.date < CURRENT_DATE()
  GROUP BY t.raw.dst, t.date
),
dst_keys AS (
  SELECT dst, NET.IP_FROM_STRING(dst) AS dst_bin
  FROM (SELECT DISTINCT dst FROM per_dst_day WHERE NOT is_gcp)
),
dst_asn AS (
  SELECT dst, asn, asname FROM (
    SELECT k.dst, p.asn, p.asname,
      ROW_NUMBER() OVER (PARTITION BY k.dst ORDER BY p.mask DESC, p.prio DESC) AS rn
    FROM dst_keys k
    JOIN masks m ON BYTE_LENGTH(k.dst_bin) = 4
    JOIN prefix_all p ON p.mask = m.mask AND p.net_bin = NET.IP_TRUNC(k.dst_bin, m.mask)
  ) WHERE rn = 1
),
labeled AS (
  SELECT
    CASE WHEN d.is_gcp THEN 'gcp' ELSE COALESCE(CAST(da.asn AS STRING), 'unmapped') END AS asn,
    CASE WHEN d.is_gcp THEN 'GCP clients (34.0.0.0/8)' ELSE COALESCE(da.asname, n.asname) END AS as_name,
    d.is_gcp, d.day, d.dst, d.tests, d.reaches, d.interdomain
  FROM per_dst_day d
  LEFT JOIN dst_asn da USING(dst)
  LEFT JOIN asn_names n ON n.asn = da.asn
),
top_ases AS (
  SELECT asn FROM labeled GROUP BY asn ORDER BY SUM(tests) DESC LIMIT 500
)
SELECT
  l.asn,
  ANY_VALUE(l.as_name) AS as_name,
  LOGICAL_OR(l.is_gcp) AS is_gcp,
  l.day,
  COUNT(*) AS unique_ips,
  SUM(l.tests) AS tests,
  SUM(l.reaches) AS reaches,
  SUM(l.interdomain) AS interdomain_count,
  CURRENT_TIMESTAMP() AS computed_at
FROM labeled l
WHERE l.asn IN (SELECT asn FROM top_ases)
GROUP BY l.asn, l.day
ORDER BY tests DESC;
