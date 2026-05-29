# API Contracts

> Layer 3 reference. Read when touching any API client.

This file documents the **three** external APIs Politicas talks to. **EdgeFinder is the new primary source** as of 2026-05-27.

---

## 1. EdgeFinder — Signal source (primary)

See [edgefinder-api.md](edgefinder-api.md) for the full reference. Quick recap:

| Endpoint | Purpose | Auth |
|----------|---------|------|
| `https://api.edgefinderai.org/api/signals` | Live + resolved signal feed | None |
| `https://api.edgefinderai.org/api/stats` | Aggregate win rate by signal_type | None |

Polite-client rules in [edgefinder-api.md](edgefinder-api.md). Client lives at [edgefinder_client.py](../edgefinder_client.py).

---

## 2. Polymarket Gamma — Event metadata (secondary)

| Item | Value |
|------|-------|
| Base | `https://gamma-api.polymarket.com` |
| Endpoint used | `/events` |
| Limit | `EVENTS_LIMIT = 100` ([config.py](../config.py)) |
| Timeout | `REQUEST_TIMEOUT_GAMMA = 30s` |

**Role in v2:** We no longer scan Gamma for our own signals. We use it only to enrich EdgeFinder signals with volume / liquidity / resolution metadata when we need to verify a signal's quality before sizing.

### Response shape (relevant fields)

```jsonc
{
  "id": "...",
  "title": "...",
  "category": "...",
  "endDate": "ISO-8601",
  "markets": [
    {
      "id": "...",
      "question": "...",
      "outcomes":       "[\"Yes\",\"No\"]",            // JSON-encoded STRING
      "clobTokenIds":   "[\"0x...\",\"0x...\"]",       // JSON-encoded STRING
      "volumeNum":      0.0,
      "liquidityNum":   0.0,
      "endDate":        "ISO-8601"
    }
  ]
}
```

`outcomes` and `clobTokenIds` arrive as JSON-encoded **strings**, not arrays. Decode with `json.loads(...)` before iterating.

---

## 3. Polymarket CLOB — Order book (secondary)

| Item | Value |
|------|-------|
| Base | `https://clob.polymarket.com` |
| Endpoint used | `/book?token_id=<hex>` |
| Timeout | `REQUEST_TIMEOUT_CLOB = 15s` |

**Role in v2:** Used to (a) verify the current NO ask before paper-filling an EdgeFinder signal, (b) compute slippage / drift vs the signal's published `yes_price`.

### Response shape

```jsonc
{
  "bids": [{"price": "0.45", "size": "1234.5"}, ...],
  "asks": [{"price": "0.46", "size": "5432.1"}, ...]
}
```

Both `price` and `size` arrive as **strings**. Convert before math.

---

## Defensive coding rules (all three)

1. **Every external read has a timeout.** Use the config constant, not a literal.
2. **No `r.json()` without try/except.** Empty bodies and HTML error pages are normal failure modes.
3. **Default every field.** `.get("field", default)`.
4. **Log to `bot_logs` on failure**, not stdout.
5. **Cache aggressively.** EdgeFinder caches 5 min in-process; Gamma + CLOB use the existing `markets` table as a TTL-free cache.

---

## When to revise this file

- New endpoint added or existing one renamed
- New field consumed
- Auth becomes required
- Timeout adjustment
