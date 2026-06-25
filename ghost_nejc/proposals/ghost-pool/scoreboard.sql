-- ghost-pool pooled scoreboard (DuckDB):  duckdb -c ".read tools/pooled/scoreboard.sql"
-- Prints freshness FIRST (a pooled number over a stale book is a lie), then
-- per-book by-era stats with the weekend/weekday split (standing team rule).
WITH t AS (
  SELECT *,
         (strftime(ts_fire_utc, '%w') IN ('0','6')
          OR (strftime(ts_fire_utc, '%w') = '5' AND strftime(ts_fire_utc, '%H') >= '21')) AS wknd,
         coin || '-' || duration || '-' || market_slug AS event_key
  FROM read_csv_auto('data/*/trades/*.csv', union_by_name=true)
)
SELECT 'FRESHNESS' AS section, book_id, NULL AS era_id, NULL AS wknd,
       COUNT(*) AS n, NULL AS wr, NULL AS net, NULL AS per_fire,
       CAST(max(ts_fire_utc) AS VARCHAR) AS newest
FROM t GROUP BY book_id
UNION ALL
SELECT 'BY-ERA', book_id, era_id, wknd,
       COUNT(*), ROUND(AVG(CASE WHEN status='won' THEN 100.0 ELSE 0 END), 1),
       ROUND(SUM(pnl_usdc), 2), ROUND(AVG(ret_per_usd), 4), NULL
FROM t GROUP BY book_id, era_id, wknd
ORDER BY section, book_id, era_id, wknd;
