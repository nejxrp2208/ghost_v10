#!/usr/bin/env python3
"""ghost-pool export validator — run before every push: python tools/validate_export.py data/J
Stdlib only. Checks contract conformance + leak scan. Exit 1 on any failure."""
import csv, json, os, re, sys

TRADES_HDR = ("schema_v,book_id,trade_id,era_id,ts_fire_utc,ts_resolve_utc,market_slug,coin,"
              "duration,side,entry_price,size_usdc,pnl_usdc,status,move_bps,secs_left,"
              "frozen_best_ask,inband_depth_usdc,fok_rt_ms,ret_per_usd,extra_json")
BIDLOG_HDR = ("schema_v,book_id,ts_utc,market_slug,coin,duration,side,secs_left,entry_price,"
              "best_bid,bid_depth_top5_usd,n_bid_levels,best_ask,binance_move_bps,era_id")
ERAS_HDR = ("era_id,start_ts_utc,snipe_window_secs,min_fill_secs,min_move_weekday,"
            "min_move_weekend,min_ask,max_ask,fill_floor,depth_gate_usdc,walk_fill,paroli,"
            "base_size,durations,note")
LEAKS = [re.compile(r"0x[a-fA-F0-9]{40}"),
         re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
         re.compile(r"\b[a-fA-F0-9]{48,}\b")]
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

fails = []
def fail(msg): fails.append(msg); print("FAIL:", msg)

def check_csv(path, hdr, ts_cols, id_col=None):
    raw = open(path, "rb").read()
    if raw[:3] == b"\xef\xbb\xbf": fail(f"{path}: BOM present")
    if b"\r" in raw: fail(f"{path}: CRLF line endings (need LF)")
    txt = raw.decode("utf-8", errors="replace")
    for rx in LEAKS:
        for m in rx.findall(txt):
            fail(f"{path}: leak-pattern match: {m[:20]}...")
    rows = list(csv.reader(txt.splitlines()))
    if not rows or ",".join(rows[0]) != hdr:
        fail(f"{path}: header mismatch"); return []
    cols = rows[0]; out = []
    seen = set()
    for i, r in enumerate(rows[1:], 2):
        if len(r) != len(cols): fail(f"{path}:{i}: column count"); continue
        d = dict(zip(cols, r))
        for tc in ts_cols:
            if d.get(tc) and not TS.match(d[tc]):
                fail(f"{path}:{i}: bad ts {tc}={d[tc]}")
        if id_col:
            k = d.get(id_col)
            if k in seen: fail(f"{path}:{i}: duplicate {id_col}={k}")
            seen.add(k)
        out.append(d)
    return out

def main(member_dir):
    eras = set()
    ef = os.path.join(member_dir, "config_eras.csv")
    if os.path.exists(ef):
        for d in check_csv(ef, ERAS_HDR, ["start_ts_utc"]):
            eras.add(d["era_id"])
    else:
        fail(f"{ef} missing")
    for sub, hdr, ts_cols, idc in (("trades", TRADES_HDR, ["ts_fire_utc", "ts_resolve_utc"], "trade_id"),
                                   ("bid_log", BIDLOG_HDR, ["ts_utc"], None)):
        dirp = os.path.join(member_dir, sub)
        if not os.path.isdir(dirp): continue
        for fn in sorted(os.listdir(dirp)):
            if not fn.endswith(".csv"): continue
            for d in check_csv(os.path.join(dirp, fn), hdr, ts_cols, idc):
                if d.get("era_id") and d["era_id"] not in eras:
                    fail(f"{sub}/{fn}: unknown era_id {d['era_id']}")
                if sub == "trades":
                    try:
                        j = d.get("extra_json")
                        if j: json.loads(j)
                    except Exception:
                        fail(f"{sub}/{fn}: invalid extra_json on trade_id={d.get('trade_id')}")
    print("OK" if not fails else f"{len(fails)} failure(s)")
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main(sys.argv[1])
