# ghost-pool — Export Contract v1

Four operators, four builds, ONE canonical data shape. Each member exports their
own book into `data/<initial>/` daily. Git is the transport, archive, and audit
log. **This repo holds trade data only — it must never contain keys, seeds,
wallet addresses, server IPs, or my-bot-style files.** Treat the contents as
wallet-equivalent: access stays limited to exactly the 4 operators.

## Why a contract instead of a shared exporter
Builds differ (team rule: never run another member's script against your bot).
You re-implement a ~80-line exporter natively against YOUR schema; this file +
the test vector in `tools/` is the common ground truth. Share the TEST, not the
code.

## The event spine
The cross-book join key is the public market event:
`event_key = (coin, duration, window_end_ts)` — derivable from the market slug
(`{coin}-updown-{dur}-{startTs}`). Every table carries `market_slug`. Four
books firing the same window = FOUR sensor readings of ONE event (dedupe with
GROUP BY event in any pooled stat; never count one window 4x).

## data/<X>/trades/YYYY-MM-DD.csv  (one file per closed UTC day)
Header (exact order):
```
schema_v,book_id,trade_id,era_id,ts_fire_utc,ts_resolve_utc,market_slug,coin,duration,side,entry_price,size_usdc,pnl_usdc,status,move_bps,secs_left,frozen_best_ask,inband_depth_usdc,fok_rt_ms,ret_per_usd,extra_json
```
Rules:
- UTF-8, no BOM, LF line endings, ISO-8601 UTC timestamps ending in `Z`.
- REAL fills only (your build's real-fill predicate; J's: `making_amt>0 OR
  fok_rt_ms IS NOT NULL`). Paper/shadow/skips go in `ext/`, never here.
- `trade_id` = stable unique id within your book (your DB rowid is fine).
- `status` in {won,lost}; unresolved trades wait for tomorrow's file.
- `ret_per_usd` = pnl/size (sizing-normalized return; pooled EV uses this).
- `inband_depth_usdc` = resting in-band ask USDC at fire (B's depth metric)
  if your build captures it; blank if not.
- `extra_json` = anything build-specific, valid JSON or blank. No secrets.
- Blank = null. No commas inside fields except within extra_json (quote it).
- A day file is immutable 48h after the day closes. Corrections = replace the
  whole file, commit message `restate(X): YYYY-MM-DD — reason`.

## data/<X>/bid_log/YYYY-MM-DD.csv
The exit-liquidity logger output, normalized:
```
schema_v,book_id,ts_utc,market_slug,coin,duration,side,secs_left,entry_price,best_bid,bid_depth_top5_usd,n_bid_levels,best_ask,binance_move_bps,era_id
```
Note your series start date in `meta.yml` (J starts 2026-06-11T16:32Z).

## data/<X>/ext/ — sensor tables (the multi-sensor part)
Free-form CSVs for whatever YOUR build uniquely captures (J: depth ladders,
skips, shadow picks; B: hunt_log; K: tape captures). Each file must carry
`book_id, market_slug, ts_utc` so it joins the event spine. Declare each table
+ its columns in `meta.yml`. Same privacy rules.

## data/<X>/config_eras.csv  (append-only)
```
era_id,start_ts_utc,snipe_window_secs,min_fill_secs,min_move_weekday,min_move_weekend,min_ask,max_ask,fill_floor,depth_gate_usdc,walk_fill,paroli,base_size,durations,note
```
A new row whenever ANY listed param changes (the machine-readable CONFIG
DISCLOSURE). Scheduled toggles (J's weekday/weekend MIN_MOVE cron) are TWO
COLUMNS of one era, not two eras per week. Pooled queries refuse to cross era
boundaries unless explicitly told to.

## data/<X>/meta.yml
book_id, build label, schema_v, series start dates per table, known quirks
("pnl is net of fees: yes/no"), declared ext tables. NO hostnames, NO paths,
NO IPs.

## Validation (run tools/validate_export.py before every push)
- header equality vs this contract; UTC `Z` timestamps; unique trade_ids
- leak scan: any `0x[a-fA-F0-9]{40}`, IPv4 literal, or 40+ char hex/base64
  string FAILS the export
- era coverage: every row's era_id exists in config_eras.csv

## Governance
- Write only inside YOUR `data/<X>/` directory. Never touch another member's.
- No force-push; linear history (set branch protection on GitHub).
- Pooled reports answer MECHANISM questions; config adoption still requires
  your own-book evidence (iron rule 2). No standing member leaderboard.
- Pooled headline numbers must print: freshness per book, era scope,
  weekend/weekday split, per-event dedupe, and leave-one-out sensitivity
  (see tools/pooled/README inside the queries).
