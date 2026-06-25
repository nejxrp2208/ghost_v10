# J's ghost-pool exporter (native to MY schema — do not copy to other books).
# Usage: python ghost_pool_export.py <snapshot.db> <pool_repo_path>
# Writes data/J/trades/YYYY-MM-DD.csv for every CLOSED UTC day not yet exported.
# Real fills only; era stamped from data/J/config_eras.csv. Read-only on the DB.
import csv, json, os, sqlite3, sys
from datetime import datetime, timezone

SNAP, POOL = sys.argv[1], sys.argv[2]
J = os.path.join(POOL, "data", "J")
ERAS = []
with open(os.path.join(J, "config_eras.csv"), encoding="utf-8") as f:
    for r in csv.DictReader(f):
        ERAS.append((r["start_ts_utc"], r["era_id"]))
ERAS.sort()

def era_for(ts_z):
    cur = ERAS[0][1]
    for start, eid in ERAS:
        if ts_z >= start:
            cur = eid
    return cur

def to_z(ts):
    if not ts:
        return ""
    ts = ts.replace("+00:00", "").split(".")[0]
    return ts + "Z"

HDR = ("schema_v,book_id,trade_id,era_id,ts_fire_utc,ts_resolve_utc,market_slug,coin,"
       "duration,side,entry_price,size_usdc,pnl_usdc,status,move_bps,secs_left,"
       "frozen_best_ask,inband_depth_usdc,fok_rt_ms,ret_per_usd,extra_json").split(",")

db = sqlite3.connect(f"file:{SNAP}?mode=ro&immutable=1", uri=True)
rows = db.execute("""
    SELECT id, ts, resolved_at, slug, coin, outcome, entry_price, size_usdc, pnl,
           status, move_bps, secs_left, depth_curve_json, cum_usdc, fok_rt_ms,
           quoted_ask, slippage_bps, presign_hit, detect_to_post_ms
    FROM positions
    WHERE status IN ('won','lost')
      AND (COALESCE(making_amt,0) > 0 OR fok_rt_ms IS NOT NULL)
    ORDER BY id""").fetchall()
db.close()

# Exclude the snapshot's OWN (incomplete) UTC day and anything later — a day
# file is written only once that UTC day has fully closed in the data we hold.
snap_day = max((r[1] or "")[:10] for r in rows) if rows else "9999-99-99"
bydays = {}
for (pid, ts, rts, slug, coin, side, entry, size, pnl, status, mv, sl, dcj,
     cum, fok, qask, slip, ph, dtp) in rows:
    day = (ts or "")[:10]
    if not day or day >= snap_day:     # closed UTC days only (snapshot's day is incomplete)
        continue
    frozen = ""
    try:
        lad = json.loads(dcj) if dcj else None
        if lad:
            frozen = min(p for p, s in lad)
    except Exception:
        pass
    tsz = to_z(ts)
    extra = {k: v for k, v in (("quoted_ask", qask), ("slippage_bps", slip),
                               ("presign_hit", ph), ("detect_to_post_ms", dtp)) if v is not None}
    bydays.setdefault(day, []).append([
        1, "J", pid, era_for(tsz), tsz, to_z(rts), slug or "", coin,
        ("15m" if slug and "-15m-" in slug else "5m"), side,
        entry, round(size or 0, 2), round(pnl or 0, 2), status,
        (round(mv, 2) if mv is not None else ""), (sl if sl is not None else ""),
        frozen, (cum if cum is not None else ""), (fok if fok is not None else ""),
        (round((pnl or 0) / size, 4) if size else ""),
        (json.dumps(extra, separators=(",", ":")) if extra else "")])

outdir = os.path.join(J, "trades")
os.makedirs(outdir, exist_ok=True)
written = skipped = 0
for day, rs in sorted(bydays.items()):
    fp = os.path.join(outdir, f"{day}.csv")
    if os.path.exists(fp):
        skipped += 1
        continue
    with open(fp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HDR)
        w.writerows(rs)
    written += 1
print(f"exported {written} day file(s), skipped {skipped} existing, "
      f"{sum(len(r) for r in bydays.values())} trades total span {min(bydays)}..{max(bydays)}")
