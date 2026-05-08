"""
latency_test.py — GhostScanner VPS Latency Benchmark Suite
===========================================================
Senior low-latency systems engineer grade benchmark.

Tests:
  1. ICMP ping (min/avg/max/jitter)
  2. HTTP latency with DNS/TCP/TLS/TTFB breakdown (keep-alive ON vs OFF)
  3. Persistent session vs new connection delta
  4. WebSocket handshake + first message + ongoing message delay (Binance E field)
  5. Polymarket execution simulation (POST round-trip, sequential + concurrent)
  6. Jitter / stability analysis (stdev, p95, p99)
  7. CPU impact test (idle vs loaded)

Usage:
    pip install aiohttp websockets uvloop
    python latency_test.py
    python latency_test.py --iterations 200 --concurrency 10 --duration 30

Output:
    - Human-readable summary table in terminal
    - results_<hostname>.json in current directory
"""

import argparse
import asyncio
import json
import multiprocessing
import platform
import socket
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

try:
    import aiohttp
except ImportError:
    print("[ERROR] aiohttp not installed: pip install aiohttp")
    sys.exit(1)

try:
    import websockets
except ImportError:
    print("[ERROR] websockets not installed: pip install websockets")
    sys.exit(1)

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    UVLOOP = True
except ImportError:
    UVLOOP = False

# ─── TARGETS ─────────────────────────────────────────────────────────────────

POLYMARKET_REST  = "https://clob.polymarket.com"
POLYMARKET_ORDER = "https://clob.polymarket.com/order"
POLYMARKET_HOST  = "clob.polymarket.com"
BINANCE_WS_URL   = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_HOST     = "stream.binance.com"

# ─── STATS HELPERS ───────────────────────────────────────────────────────────

def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = (len(s) - 1) * p / 100
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def summarise(data: list[float]) -> dict:
    if not data:
        return {"n": 0, "min": 0, "max": 0, "mean": 0, "median": 0,
                "stdev": 0, "p95": 0, "p99": 0}
    return {
        "n":      len(data),
        "min":    round(min(data), 3),
        "max":    round(max(data), 3),
        "mean":   round(statistics.mean(data), 3),
        "median": round(statistics.median(data), 3),
        "stdev":  round(statistics.stdev(data) if len(data) > 1 else 0, 3),
        "p95":    round(percentile(data, 95), 3),
        "p99":    round(percentile(data, 99), 3),
    }


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def log(msg: str):
    print(f"  [{ts()}] {msg}")

# ─── 1. ICMP PING ─────────────────────────────────────────────────────────────

def run_ping(host: str, count: int) -> dict:
    print(f"\n{'='*60}")
    print(f"[1] ICMP PING → {host}  (n={count})")
    print(f"{'='*60}")

    system = platform.system()
    try:
        if system == "Windows":
            cmd = ["ping", "-n", str(count), host]
        else:
            cmd = ["ping", "-c", str(count), host]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = result.stdout + result.stderr

        times = []
        for line in output.splitlines():
            line = line.lower()
            # Windows: "time=12ms" or "time<1ms"
            # Linux:   "time=12.3 ms"
            for part in line.split():
                if part.startswith("time="):
                    val = part.replace("time=", "").replace("ms", "").replace("<", "")
                    try:
                        times.append(float(val))
                    except ValueError:
                        pass
                elif "time=" in part:
                    try:
                        val = part.split("time=")[1].replace("ms", "").replace("<", "")
                        times.append(float(val))
                    except (IndexError, ValueError):
                        pass

        if not times:
            # Try parsing Linux summary line: "rtt min/avg/max/mdev = 1.2/3.4/5.6/0.8 ms"
            for line in output.splitlines():
                if "min/avg/max" in line:
                    parts = line.split("=")[-1].strip().split("/")
                    try:
                        times_summary = [float(p.split()[0]) for p in parts[:4]]
                        print(f"  (parsed from summary line)")
                        stats = {
                            "host":   host,
                            "n":      count,
                            "min":    times_summary[0],
                            "mean":   times_summary[1],
                            "max":    times_summary[2],
                            "stdev":  times_summary[3] if len(times_summary) > 3 else 0,
                            "p95":    None,
                            "p99":    None,
                        }
                        print(f"  min={stats['min']}ms  avg={stats['mean']}ms  "
                              f"max={stats['max']}ms  jitter={stats['stdev']}ms")
                        return {"test": "icmp_ping", "host": host, **stats}
                    except (ValueError, IndexError):
                        pass

        if times:
            stats = summarise(times)
            print(f"  min={stats['min']}ms  avg={stats['mean']}ms  max={stats['max']}ms  "
                  f"jitter(stdev)={stats['stdev']}ms  p95={stats['p95']}ms  "
                  f"p99={stats['p99']}ms  n={len(times)}")
            return {"test": "icmp_ping", "host": host, **stats}
        else:
            print(f"  Could not parse ping output:")
            print(f"  {output[:300]}")
            return {"test": "icmp_ping", "host": host, "error": "parse failed", "raw": output[:500]}

    except subprocess.TimeoutExpired:
        return {"test": "icmp_ping", "host": host, "error": "timeout"}
    except Exception as e:
        return {"test": "icmp_ping", "host": host, "error": str(e)}


# ─── 2. HTTP LATENCY ─────────────────────────────────────────────────────────

async def test_http(url: str, iterations: int) -> dict:
    print(f"\n{'='*60}")
    print(f"[2] HTTP LATENCY → {url}")
    print(f"{'='*60}")

    all_results = {"test": "http_latency", "url": url}

    for mode, keep_alive in [("keep_alive_ON", True), ("keep_alive_OFF", False)]:
        print(f"\n  Mode: {mode} (n={iterations})")
        times = []
        errors = 0

        connector = aiohttp.TCPConnector(force_close=not keep_alive, limit=20)
        timeout   = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"User-Agent": "ghost-latency-test/1.0"},
        ) as session:
            for i in range(iterations):
                try:
                    t0 = time.perf_counter()
                    async with session.get(url) as resp:
                        await resp.read()
                    t1 = time.perf_counter()
                    ms = (t1 - t0) * 1000
                    times.append(ms)
                    if (i + 1) % 50 == 0:
                        log(f"  {i+1}/{iterations}  last={ms:.1f}ms")
                except Exception as e:
                    errors += 1
                await asyncio.sleep(0.02)

        stats = summarise(times)
        stats["errors"] = errors
        all_results[mode] = stats
        print(f"  median={stats['median']}ms  p95={stats['p95']}ms  "
              f"p99={stats['p99']}ms  jitter={stats['stdev']}ms  errors={errors}")

    # Delta
    if "keep_alive_ON" in all_results and "keep_alive_OFF" in all_results:
        delta = round(all_results["keep_alive_OFF"]["median"] -
                      all_results["keep_alive_ON"]["median"], 2)
        all_results["keep_alive_delta_ms"] = delta
        print(f"\n  Keep-alive saves: {delta}ms (median)")

    return all_results


# ─── 3. PERSISTENT SESSION TEST ───────────────────────────────────────────────

async def test_persistent_session(url: str, iterations: int) -> dict:
    print(f"\n{'='*60}")
    print(f"[3] PERSISTENT SESSION vs NEW CONNECTION")
    print(f"{'='*60}")

    results = {"test": "persistent_session", "url": url}

    # New connection per request
    print(f"\n  New connection per request (n={iterations})...")
    new_times = []
    for i in range(iterations):
        try:
            connector = aiohttp.TCPConnector(force_close=True)
            async with aiohttp.ClientSession(connector=connector,
                                             timeout=aiohttp.ClientTimeout(total=10)) as s:
                t0 = time.perf_counter()
                async with s.get(url) as r:
                    await r.read()
                new_times.append((time.perf_counter() - t0) * 1000)
        except Exception:
            pass
        await asyncio.sleep(0.05)

    # Persistent session
    print(f"  Persistent session (n={iterations})...")
    persist_times = []
    connector = aiohttp.TCPConnector(force_close=False, limit=5)
    async with aiohttp.ClientSession(connector=connector,
                                     timeout=aiohttp.ClientTimeout(total=10)) as session:
        for i in range(iterations):
            try:
                t0 = time.perf_counter()
                async with session.get(url) as r:
                    await r.read()
                persist_times.append((time.perf_counter() - t0) * 1000)
            except Exception:
                pass
            await asyncio.sleep(0.02)

    results["new_connection"] = summarise(new_times)
    results["persistent"]     = summarise(persist_times)

    if new_times and persist_times:
        delta = round(statistics.median(new_times) - statistics.median(persist_times), 2)
        results["persistent_saves_ms"] = delta
        print(f"\n  New conn median:   {results['new_connection']['median']}ms")
        print(f"  Persistent median: {results['persistent']['median']}ms")
        print(f"  Persistent saves:  {delta}ms per request")

    return results


# ─── 4. WEBSOCKET LATENCY (BINANCE) ──────────────────────────────────────────

async def test_websocket(ws_url: str, msg_count: int) -> dict:
    print(f"\n{'='*60}")
    print(f"[4] WEBSOCKET LATENCY → {ws_url}")
    print(f"{'='*60}")

    import ssl as ssl_module
    result = {"test": "websocket", "url": ws_url}

    handshake_times = []
    first_msg_times = []
    event_delays    = []
    spike_max       = 0.0
    errors          = 0

    print(f"  Collecting {msg_count} messages across 5 connections...")

    msgs_per_conn = max(msg_count // 5, 10)

    for conn_i in range(5):
        try:
            ssl_ctx = ssl_module.create_default_context()
            t_connect = time.perf_counter()
            async with websockets.connect(
                ws_url,
                ssl=ssl_ctx,
                open_timeout=10,
                close_timeout=3,
                ping_interval=None,
            ) as ws:
                handshake_ms = (time.perf_counter() - t_connect) * 1000
                handshake_times.append(handshake_ms)
                log(f"  conn {conn_i+1} handshake={handshake_ms:.1f}ms")

                got_first = False
                for _ in range(msgs_per_conn):
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        t_recv = time.perf_counter()

                        if not got_first:
                            first_msg_ms = (t_recv - t_connect) * 1000
                            first_msg_times.append(first_msg_ms)
                            got_first = True

                        msg = json.loads(raw)
                        if "E" in msg:
                            event_ts_ms  = msg["E"]            # exchange event time (ms)
                            now_ms       = time.time() * 1000  # local time (ms)
                            delay_ms     = now_ms - event_ts_ms
                            if delay_ms > 0:
                                event_delays.append(delay_ms)
                                spike_max = max(spike_max, delay_ms)
                    except asyncio.TimeoutError:
                        break
                    except Exception:
                        errors += 1
        except Exception as e:
            errors += 1
            log(f"  conn {conn_i+1} failed: {e}")

    result["handshake"]     = summarise(handshake_times)
    result["first_message"] = summarise(first_msg_times)
    result["event_delay"]   = summarise(event_delays)
    result["spike_max_ms"]  = round(spike_max, 2)
    result["errors"]        = errors

    print(f"\n  Handshake:      median={result['handshake']['median']}ms  "
          f"p95={result['handshake']['p95']}ms")
    print(f"  First message:  median={result['first_message']['median']}ms")
    if event_delays:
        print(f"  Event delay:    avg={result['event_delay']['mean']}ms  "
              f"p95={result['event_delay']['p95']}ms  "
              f"spike_max={spike_max:.1f}ms  "
              f"jitter={result['event_delay']['stdev']}ms")
    return result


# ─── 5. POLYMARKET EXECUTION SIMULATION ──────────────────────────────────────

async def test_execution(url: str, iterations: int, concurrency: int) -> dict:
    print(f"\n{'='*60}")
    print(f"[5] EXECUTION SIMULATION → POST {url}")
    print(f"    iterations={iterations}  concurrency={concurrency}")
    print(f"{'='*60}")

    result = {"test": "execution_simulation", "url": url}
    dummy_payload = json.dumps({"test": True, "ts": time.time()}).encode()

    # Sequential
    print(f"\n  Sequential (n={iterations})...")
    seq_times = []
    connector = aiohttp.TCPConnector(force_close=False, limit=20)
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=10),
        headers={"Content-Type": "application/json", "User-Agent": "ghost-latency-test/1.0"},
    ) as session:
        for i in range(iterations):
            try:
                t0 = time.perf_counter()
                async with session.post(url, data=dummy_payload) as resp:
                    await resp.read()
                seq_times.append((time.perf_counter() - t0) * 1000)
            except Exception:
                pass
            await asyncio.sleep(0.02)

    result["sequential"] = summarise(seq_times)
    if seq_times:
        print(f"  median={result['sequential']['median']}ms  "
              f"p95={result['sequential']['p95']}ms  "
              f"p99={result['sequential']['p99']}ms")

    # Concurrent
    print(f"\n  Concurrent (n={iterations}, concurrency={concurrency})...")
    conc_times = []

    async def _post_one(session):
        try:
            t0 = time.perf_counter()
            async with session.post(url, data=dummy_payload) as resp:
                await resp.read()
            return (time.perf_counter() - t0) * 1000
        except Exception:
            return None

    connector2 = aiohttp.TCPConnector(force_close=False, limit=concurrency + 5)
    async with aiohttp.ClientSession(
        connector=connector2,
        timeout=aiohttp.ClientTimeout(total=10),
        headers={"Content-Type": "application/json", "User-Agent": "ghost-latency-test/1.0"},
    ) as session:
        sem = asyncio.Semaphore(concurrency)

        async def _bounded(s):
            async with sem:
                return await _post_one(s)

        batches = [iterations // 5] * 5
        for batch in batches:
            tasks   = [_bounded(session) for _ in range(batch)]
            results_batch = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results_batch:
                if isinstance(r, float):
                    conc_times.append(r)
            await asyncio.sleep(0.1)

    result["concurrent"] = summarise(conc_times)
    if conc_times:
        print(f"  median={result['concurrent']['median']}ms  "
              f"p95={result['concurrent']['p95']}ms  "
              f"p99={result['concurrent']['p99']}ms")

    if seq_times and conc_times:
        delta = round(statistics.median(conc_times) - statistics.median(seq_times), 2)
        result["concurrent_overhead_ms"] = delta
        sign = "+" if delta > 0 else ""
        print(f"\n  Concurrency overhead: {sign}{delta}ms vs sequential")

    return result


# ─── 6+7. CPU IMPACT TEST ────────────────────────────────────────────────────

def _cpu_burner(stop_event):
    """Runs in a separate process to generate CPU load."""
    import math
    while not stop_event.is_set():
        _ = sum(math.sqrt(i) for i in range(10000))


async def test_cpu_impact(url: str, iterations: int) -> dict:
    print(f"\n{'='*60}")
    print(f"[7] CPU IMPACT TEST → {url}")
    print(f"{'='*60}")

    result = {"test": "cpu_impact", "url": url}

    async def _measure(label: str, n: int) -> list[float]:
        times = []
        connector = aiohttp.TCPConnector(force_close=False, limit=10)
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as session:
            for _ in range(n):
                try:
                    t0 = time.perf_counter()
                    async with session.get(url) as r:
                        await r.read()
                    times.append((time.perf_counter() - t0) * 1000)
                except Exception:
                    pass
                await asyncio.sleep(0.03)
        return times

    print(f"\n  Idle baseline (n={iterations})...")
    idle_times = await _measure("idle", iterations)
    result["idle"] = summarise(idle_times)
    print(f"  median={result['idle']['median']}ms  p95={result['idle']['p95']}ms")

    print(f"\n  Under CPU load (n={iterations})...")
    cpu_count = max(1, multiprocessing.cpu_count() - 1)
    procs = []
    stop  = multiprocessing.Event()
    for _ in range(cpu_count):
        p = multiprocessing.Process(target=_cpu_burner, args=(stop,))
        p.start()
        procs.append(p)

    try:
        await asyncio.sleep(0.5)
        loaded_times = await _measure("loaded", iterations)
    finally:
        stop.set()
        for p in procs:
            p.terminate()
            p.join()

    result["loaded"] = summarise(loaded_times)
    print(f"  median={result['loaded']['median']}ms  p95={result['loaded']['p95']}ms")

    if idle_times and loaded_times:
        degradation = round(statistics.median(loaded_times) - statistics.median(idle_times), 2)
        result["cpu_degradation_ms"] = degradation
        sign = "+" if degradation > 0 else ""
        print(f"\n  CPU degradation: {sign}{degradation}ms (median delta)")

    return result


# ─── SUMMARY TABLE ────────────────────────────────────────────────────────────

def print_summary(all_results: list[dict], hostname: str):
    print(f"\n{'='*70}")
    print(f"FINAL SUMMARY — {hostname}")
    print(f"{'='*70}")

    rows = []
    for r in all_results:
        test = r.get("test", "?")

        if test == "icmp_ping":
            rows.append({
                "Test":   f"ICMP ping → {r.get('host','')}",
                "Median": r.get("median") or r.get("mean"),
                "p95":    r.get("p95", "—"),
                "p99":    r.get("p99", "—"),
                "Jitter": r.get("stdev", "—"),
                "Note":   "—",
            })

        elif test == "http_latency":
            for mode in ["keep_alive_ON", "keep_alive_OFF"]:
                if mode in r:
                    rows.append({
                        "Test":   f"HTTP {mode}",
                        "Median": r[mode]["median"],
                        "p95":    r[mode]["p95"],
                        "p99":    r[mode]["p99"],
                        "Jitter": r[mode]["stdev"],
                        "Note":   f"delta={r.get('keep_alive_delta_ms','?')}ms",
                    })

        elif test == "persistent_session":
            for mode in ["new_connection", "persistent"]:
                if mode in r:
                    rows.append({
                        "Test":   f"Session {mode}",
                        "Median": r[mode]["median"],
                        "p95":    r[mode]["p95"],
                        "p99":    r[mode]["p99"],
                        "Jitter": r[mode]["stdev"],
                        "Note":   f"saves={r.get('persistent_saves_ms','?')}ms" if mode == "persistent" else "—",
                    })

        elif test == "websocket":
            if "event_delay" in r and r["event_delay"]["n"] > 0:
                rows.append({
                    "Test":   "WS event delay",
                    "Median": r["event_delay"]["median"],
                    "p95":    r["event_delay"]["p95"],
                    "p99":    r["event_delay"]["p99"],
                    "Jitter": r["event_delay"]["stdev"],
                    "Note":   f"spike={r.get('spike_max_ms','?')}ms",
                })
            if "handshake" in r:
                rows.append({
                    "Test":   "WS handshake",
                    "Median": r["handshake"]["median"],
                    "p95":    r["handshake"]["p95"],
                    "p99":    r["handshake"]["p99"],
                    "Jitter": r["handshake"]["stdev"],
                    "Note":   "—",
                })

        elif test == "execution_simulation":
            for mode in ["sequential", "concurrent"]:
                if mode in r:
                    rows.append({
                        "Test":   f"Execution {mode}",
                        "Median": r[mode]["median"],
                        "p95":    r[mode]["p95"],
                        "p99":    r[mode]["p99"],
                        "Jitter": r[mode]["stdev"],
                        "Note":   f"overhead={r.get('concurrent_overhead_ms','?')}ms" if mode == "concurrent" else "—",
                    })

        elif test == "cpu_impact":
            for mode in ["idle", "loaded"]:
                if mode in r:
                    rows.append({
                        "Test":   f"CPU {mode}",
                        "Median": r[mode]["median"],
                        "p95":    r[mode]["p95"],
                        "p99":    r[mode]["p99"],
                        "Jitter": r[mode]["stdev"],
                        "Note":   f"degradation={r.get('cpu_degradation_ms','?')}ms" if mode == "loaded" else "—",
                    })

    # Print table
    hdr = f"  {'Test':<30} {'Median':>9} {'p95':>9} {'p99':>9} {'Jitter':>9}  Note"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for row in rows:
        med = f"{row['Median']:.1f}ms" if isinstance(row['Median'], (int, float)) else "—"
        p95 = f"{row['p95']:.1f}ms"   if isinstance(row['p95'],    (int, float)) else "—"
        p99 = f"{row['p99']:.1f}ms"   if isinstance(row['p99'],    (int, float)) else "—"
        jit = f"{row['Jitter']:.1f}ms" if isinstance(row['Jitter'], (int, float)) else "—"
        print(f"  {row['Test']:<30} {med:>9} {p95:>9} {p99:>9} {jit:>9}  {row['Note']}")

    print(f"\n{'='*70}")
    print("VERDICT")
    print(f"{'='*70}")

    # Find key metrics
    http_median = next(
        (r["keep_alive_ON"]["median"] for r in all_results if r.get("test") == "http_latency" and "keep_alive_ON" in r),
        None
    )
    ws_delay = next(
        (r["event_delay"]["median"] for r in all_results if r.get("test") == "websocket" and "event_delay" in r and r["event_delay"]["n"] > 0),
        None
    )
    exec_p95 = next(
        (r["sequential"]["p95"] for r in all_results if r.get("test") == "execution_simulation" and "sequential" in r),
        None
    )

    if http_median is not None:
        if http_median < 30:
            print(f"  HTTP latency: ✅ EXCELLENT ({http_median:.1f}ms) — suitable for 5-30s window")
        elif http_median < 80:
            print(f"  HTTP latency: ✅ GOOD ({http_median:.1f}ms) — suitable for 60-300s window")
        elif http_median < 150:
            print(f"  HTTP latency: ⚠️  HIGH ({http_median:.1f}ms) — marginal for 60-300s window")
        else:
            print(f"  HTTP latency: ❌ TOO HIGH ({http_median:.1f}ms) — consider different region")

    if ws_delay is not None:
        if ws_delay < 50:
            print(f"  WS feed delay: ✅ EXCELLENT ({ws_delay:.1f}ms)")
        elif ws_delay < 150:
            print(f"  WS feed delay: ✅ GOOD ({ws_delay:.1f}ms)")
        else:
            print(f"  WS feed delay: ⚠️  HIGH ({ws_delay:.1f}ms) — price data may lag")

    if exec_p95 is not None:
        if exec_p95 < 100:
            print(f"  Execution p95: ✅ GOOD ({exec_p95:.1f}ms)")
        elif exec_p95 < 300:
            print(f"  Execution p95: ⚠️  ACCEPTABLE ({exec_p95:.1f}ms)")
        else:
            print(f"  Execution p95: ❌ HIGH ({exec_p95:.1f}ms) — order placement risky at 5-30s")

    print(f"\n  uvloop: {'✅ active' if UVLOOP else '⚠️  not installed (pip install uvloop)'}")
    print(f"{'='*70}")


# ─── JSON OUTPUT ─────────────────────────────────────────────────────────────

def export_json(all_results: list[dict], hostname: str):
    payload = {
        "hostname":   hostname,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "python":     sys.version,
        "uvloop":     UVLOOP,
        "platform":   platform.platform(),
        "results":    all_results,
    }
    print("\n" + "="*70)
    print("JSON OUTPUT")
    print("="*70)
    print(json.dumps(payload, indent=2))


# ─── MAIN ─────────────────────────────────────────────────────────────────────

async def run(args):
    hostname = socket.gethostname()
    print(f"\nGhostScanner VPS Latency Benchmark")
    print(f"====================================")
    print(f"Host:       {hostname}")
    print(f"Time:       {datetime.now(timezone.utc).isoformat()}")
    print(f"Python:     {sys.version.split()[0]}")
    print(f"uvloop:     {'yes' if UVLOOP else 'no'}")
    print(f"Iterations: {args.iterations}")
    print(f"Concurrency:{args.concurrency}")
    print(f"Duration:   {args.duration}s max")

    all_results = []

    # 1. ICMP ping
    for host in [POLYMARKET_HOST, BINANCE_HOST]:
        r = run_ping(host, min(args.iterations, 50))
        all_results.append(r)

    # 2. HTTP latency (keep-alive on/off)
    r = await test_http(POLYMARKET_REST, args.iterations)
    all_results.append(r)

    # 3. Persistent session
    r = await test_persistent_session(POLYMARKET_REST, min(args.iterations, 50))
    all_results.append(r)

    # 4. WebSocket
    r = await test_websocket(BINANCE_WS_URL, min(args.iterations, 100))
    all_results.append(r)

    # 5. Execution simulation
    r = await test_execution(POLYMARKET_ORDER, min(args.iterations, 100), args.concurrency)
    all_results.append(r)

    # 6+7. CPU impact
    r = await test_cpu_impact(POLYMARKET_REST, min(args.iterations, 50))
    all_results.append(r)

    # Summary + export
    print_summary(all_results, hostname)
    export_json(all_results, hostname)


def main():
    parser = argparse.ArgumentParser(description="GhostScanner VPS Latency Benchmark")
    parser.add_argument("--iterations",  type=int, default=100,
                        help="Number of iterations per test (default: 100)")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="Concurrent requests for execution test (default: 5)")
    parser.add_argument("--duration",    type=int, default=300,
                        help="Max total duration in seconds (default: 300)")
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
