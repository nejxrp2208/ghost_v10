#!/usr/bin/env python3
"""Validate a config file against _schema.yaml: every section/key present, no stray keys,
and flag any value still UNVERIFIED or empty. Pure stdlib (no yaml dep needed -- light parse).
Usage: python3 tools/validate_config.py configs/branden.yaml
"""
import sys, os, re

def sections(path):
    """Return {section: set(keys)} from a simple 2-space-indented yaml."""
    out, cur = {}, None
    for ln in open(path):
        raw = ln.rstrip("\n")
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if re.match(r"^[A-Za-z_]+:\s*(#.*)?$", raw):         # top-level section (allow trailing comment)
            cur = raw.split(":")[0]; out[cur] = {}
        elif raw.startswith("  ") and ":" in raw and cur:    # key under section
            k = raw.strip().split(":")[0]
            v = raw.split(":", 1)[1].split("#")[0].strip()
            out[cur][k] = v
    return out

def main():
    if len(sys.argv) < 2:
        print("usage: validate_config.py <config.yaml>"); sys.exit(2)
    cfg_path = sys.argv[1]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    schema = sections(os.path.join(here, "configs", "_schema.yaml"))
    cfg = sections(cfg_path)
    errs, warns = [], []

    for sec, keys in schema.items():
        if sec == "meta":
            continue
        if sec not in cfg:
            errs.append(f"missing section: {sec}"); continue
        for k in keys:
            if k not in cfg[sec]:
                errs.append(f"missing key: {sec}.{k}")
        for k in cfg[sec]:
            if k not in keys:
                warns.append(f"stray key not in schema: {sec}.{k}")

    # value-level flags
    for sec in cfg:
        for k, v in cfg[sec].items():
            if v == "" or v is None:
                warns.append(f"empty value: {sec}.{k}")

    txt = open(cfg_path).read()
    unv = txt.count("UNVERIFIED")
    status = re.search(r"status:\s*(\w+)", txt)
    status = status.group(1) if status else "?"

    print(f"=== {cfg_path}  (status={status}) ===")
    for e in errs:  print("  ERROR :", e)
    for w in warns: print("  warn  :", w)
    if unv: print(f"  warn  : {unv} value(s) marked UNVERIFIED")
    if not errs and not warns and not unv:
        print("  ✅ schema-complete, all values set, nothing unverified")
    sys.exit(1 if errs else 0)

if __name__ == "__main__":
    main()
