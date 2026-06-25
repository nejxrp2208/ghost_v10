-- Run these on YOUR positions table before adopting any team threshold.
-- 1. Entry band: where does your money actually live?
SELECT CASE WHEN entry_price<0.6 THEN '<0.60' WHEN entry_price<0.7 THEN '0.60-0.70'
            ELSE '0.70+' END band, COUNT(*) n,
       ROUND(AVG(status='won')*100,1) wr, ROUND(SUM(pnl),2) net
FROM positions WHERE status IN('won','lost') GROUP BY band;
-- 2. Book depth bands (tune your MIN_BOOK_DEPTH):
SELECT CASE WHEN cum_usdc<50 THEN '<50' WHEN cum_usdc<100 THEN '50-100'
            WHEN cum_usdc<300 THEN '100-300' WHEN cum_usdc<1000 THEN '300-1000'
            ELSE '1000+' END band, COUNT(*) n,
       ROUND(AVG(status='won')*100,1) wr, ROUND(SUM(pnl),2) net
FROM positions WHERE status IN('won','lost') GROUP BY band;
-- 3. Depth-gate backtest at threshold K (try 50/100/200/300):
SELECT COUNT(*), ROUND(SUM(pnl),2) FROM positions
WHERE status IN('won','lost') AND cum_usdc >= 100;
-- 4. Weekend split (Fri 21:00 UTC -> Sun):
SELECT (strftime('%w',substr(ts,1,10)) IN ('0','6')
     OR (strftime('%w',substr(ts,1,10))='5' AND substr(ts,12,2)>='21')) wknd,
       COUNT(*) n, ROUND(AVG(status='won')*100,1) wr, ROUND(SUM(pnl),2) net
FROM positions WHERE status IN('won','lost') GROUP BY wknd;
-- 5. Stake rung x duration (ladder audit):
SELECT ROUND(size_usdc,1) stake, slug LIKE '%-15m-%' is15m, COUNT(*) n,
       ROUND(AVG(status='won')*100,1) wr, ROUND(SUM(pnl),2) net
FROM positions WHERE status IN('won','lost') GROUP BY stake, is15m ORDER BY stake;
-- 6. Move-size / chop band: is the edge U-shaped? (bucket abs(move_bps) at fire; set your OWN MOVE_EXCLUDE -- bands are per-book)
SELECT CASE WHEN abs(move_bps)<1.5 THEN '<1.5' WHEN abs(move_bps)<2.5 THEN '1.5-2.5'
            WHEN abs(move_bps)<4 THEN '2.5-4' ELSE '4+' END band, COUNT(*) n,
       ROUND(AVG(status='won')*100,1) wr, ROUND(AVG(entry_price),3) avg_entry,
       ROUND(SUM(pnl),2) net
FROM positions WHERE status IN('won','lost') GROUP BY band;
-- 7. Strike time: 4h UTC blocks x weekday/weekend (J: 08-11 UTC weekday = 92% WR sweet spot; check yours.
--    wknd here = all of Fri-Sun to match J's published splits, unlike #4's Fri-21:00 cut)
SELECT (strftime('%w',substr(ts,1,10)) IN ('0','5','6')) wknd,
       (CAST(strftime('%H',ts) AS INT)/4)*4 AS hh4_utc, COUNT(*) n,
       ROUND(AVG(status='won')*100,1) wr, ROUND(SUM(pnl),2) net,
       ROUND(SUM(pnl)/COUNT(*),2) per_trade
FROM positions WHERE status IN('won','lost') GROUP BY wknd, hh4_utc ORDER BY wknd, hh4_utc;
