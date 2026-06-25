-- 4-book ensemble/disagreement study (DuckDB). Event-spine join: same window,
-- did books pick different sides, and who was right? Dedupes to one row per
-- (event, book). Windows with <2 books are excluded (no disagreement possible).
WITH t AS (
  SELECT book_id, market_slug, coin, duration, side, status, pnl_usdc, ret_per_usd,
         era_id, ts_fire_utc
  FROM read_csv_auto('data/*/trades/*.csv', union_by_name=true)
),
ev AS (
  SELECT market_slug, coin, duration,
         COUNT(DISTINCT book_id) AS books,
         COUNT(DISTINCT side) AS sides,
         MAX(CASE WHEN status='won' THEN side END) AS winning_side
  FROM t GROUP BY market_slug, coin, duration
  HAVING COUNT(DISTINCT book_id) >= 2
)
SELECT ev.books, (ev.sides > 1) AS disagreed,
       COUNT(*) AS n_events,
       ROUND(AVG(CASE WHEN t.status='won' THEN 100.0 ELSE 0 END), 1) AS pooled_wr,
       ROUND(SUM(t.pnl_usdc), 2) AS net
FROM ev JOIN t USING (market_slug, coin, duration)
GROUP BY ev.books, disagreed
ORDER BY ev.books, disagreed;
-- Interpretation guide: if disagreed-window WR is ~coin-flip, disagreement is a
-- VETO candidate (chop detector). If one book is systematically right in
-- disagreements, that book's feed/timing leads — study, don't copy (rule 2).
