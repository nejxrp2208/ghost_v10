# Weekly Audit — [J] book — Jun 8–15, 2026

**Source:** my live `positions`, real fills only (`making_amt>0`, status in won/lost), n=582 resolved.
**Purpose:** independent cross-book replication of B's 06-15 weekly audit. Same questions, my separate book.
**Headline:** 70.4% WR, **+$60.31**, EV **+$0.104/fire**. Marginally green; the green lives in the morning/weekday/$100+/0.70-0.75 cells and is half given back by the cheap-ask, sub-$100, 2.5-4bps, overnight, ETH cells.

> Caveat up front: single week, in-sample, and my window spans 3 config eras (walk-fill on since 06-10, instant-ladder prov v2 06-13, flat-1.5 weekend override mid-06-13). Read this as **band/depth structure**, not a window-era EV claim. Per team rule 2 these are candidates to confirm on your own book.

## 1. Primary split — weekday vs weekend (Fri-Sun)
| Segment | n | WR | net | EV/fire |
|---|---|---|---|---|
| Mon-Thu | 345 | 73.0% | +$94.21 | +$0.273 |
| Fri-Sun | 237 | 66.7% | −$33.90 | −$0.143 |

Weekend still bleeds even after the flat-1.5 override (operator call, mid-Sat). The realized weekend A/B is contaminated by that mid-window config change — full bucket Monday.

## 2. Ask / entry-price band — B's "the separator" REPRODUCES
| Band | n | WR | net | EV/fire |
|---|---|---|---|---|
| <0.60 | 59 | 42.4% | −$37.68 | −$0.639 |
| 0.60-0.65 | 46 | 50.0% | −$64.07 | −$1.393 ← **my worst band** |
| 0.65-0.70 | 93 | 68.8% | +$52.61 | +$0.566 |
| 0.70-0.75 | 173 | 77.5% | +$103.87 | +$0.600 ← **best $** |
| 0.75-0.80 | 211 | 77.7% | +$5.58 | +$0.026 (high WR, ~0 net) |
| 0.80+ | 0 | — | — | — (MAX_ASK caps me out; can't evaluate) |

Same shape as B: cheap end is the bleed, 0.70-0.75 is the engine. Lead size does not separate (consistent with B's non-monotonic finding and my per-coin lesson). My 0.80+ band is **empty** — so the "0.80+ is best" read I posted earlier is not evaluable on this week's capped book.

## 3. Depth gate ($100) — RECONFIRMED HARD
| Bucket | n | WR | net | EV/fire |
|---|---|---|---|---|
| <$50 | 188 | 70.2% | −$78.12 | −$0.416 |
| $50-100 | 79 | 68.4% | −$121.06 | −$1.532 ← worst |
| $100+ | 315 | 71.1% | +$259.49 | +$0.824 |

Sub-$100 bled **−$199 this week alone**; the $100+ set carries the entire book. Third independent book to say do-not-lower.

## 4. Move-band U-shape
| \|move\| bps | n | WR | net | EV/fire |
|---|---|---|---|---|
| 1.5-2.5 | 304 | 73.7% | +$259.61 | +$0.854 ← engine |
| 2.5-4 | 190 | 65.3% | −$218.89 | −$1.152 ← bleeder |
| 4+ | 88 | 70.5% | +$19.59 | +$0.223 |

Matches my per-coin × move-band lesson and K's MOVE_EXCLUDE 2.5-4.

## 5. Hour of day (UTC)
| Hours | n | WR | net | EV/fire |
|---|---|---|---|---|
| 00-04 | 109 | 64.2% | −$114.88 | −$1.054 ← worst (overnight chop) |
| 04-08 | 79 | 73.4% | +$80.24 | +$1.016 |
| 08-11 | 59 | 72.9% | +$77.50 | +$1.314 ← best EV |
| 11-15 | 102 | 72.5% | +$64.39 | +$0.631 |
| 15-20 | 141 | 70.2% | −$92.17 | −$0.654 |
| 20-24 | 92 | 71.7% | +$45.23 | +$0.492 |

Confirms both my morning sweet spot (08-11Z) and B's overnight-chop bleed (00-04Z).

## 6. Coin
| Coin | n | WR | net | EV/fire |
|---|---|---|---|---|
| BTC | 328 | 71.0% | +$159.01 | +$0.485 |
| ETH | 254 | 69.7% | −$98.70 | −$0.389 |

ETH still the red coin (consistent with my per-coin lesson — deeper mid-move bleeder).

## 7. ask<X skip simulation (B's strongest candidate, on my book)
| Cut | fills dropped | dropped net | book before | book after | drop-largest-winner survives? |
|---|---|---|---|---|---|
| skip <0.60 | 59 (42.4% WR) | −$37.68 | +$60.31 | +$97.99 | yes (top cheap winner $21.62) |
| skip <0.65 | 105 | −$101.75 | +$60.31 | +$162.06 | yes |

My worst band is 0.60-0.65, so my cut wants to extend to **0.65**, stronger than B's <0.60. Both survive removing the single largest cheap winner. Still in-sample / one week — candidate, not a deploy.
