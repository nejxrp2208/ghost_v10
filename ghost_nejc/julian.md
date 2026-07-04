# JULIAN [J] — kompleten dosje (deep research, 2026-07-04)

> Vir: privatni team repo `Ghinsinc/Ghins-inc` — njegov `fills.csv` (5.191 LIVE fillov,
> 29.05–03.07), `data/configs/julian.yaml`, 50 njegovih commitov, 43 [J] LESSONS vnosov v
> teamskem CLAUDE.md, reporti (golden-hour recept, weekly audit, T-12 loss anatomy).
> Vse številke iz fillov so MOJ preračun surovega CSV — ne njegove trditve.
> ⚠️ Njihovo železno pravilo (in naše): **ure/bandi so PER-BOOK — nič ne kopiraj dobesedno,
> vse reproduciraj na lastnih podatkih.**

---

## 1. TL;DR — zakaj je najbolj profitabilen

**+$464 na 5.191 LIVE fillih · 78,2% WR · +2,05% ROI/staviran $.** Ampak resnica je ožja:

| Segment | n | WR | PnL |
|---|--:|--:|--:|
| **Mon–Thu 08–11Z (golden blok)** | 637 | 83,5% | **+$679** |
| — od tega stake ≥ $20 | 272 | 87,1% | +$521 |
| Mon–Thu vse OSTALE ure, stake ≥ $8 | 380 | 71,8% | **−$134** |
| Vikend (Fri–Sun), vse | 1.797 | 75,8% | **−$170** |
| Vikend 08–11Z ("golden" ob vikendih) | 269 | 77,7% | +$20 (mrtvo) |

**Njegov celoten edge je EN 4-urni blok, 4 dni v tednu.** Vse izven njega neto izgublja.
Njegova rešitev ni blokiranje — je **sizing po urah**: $25 v golden, $5 v secondary, $1 povsod
drugje (mikro-probe = skoraj zastonj zbiranje podatkov: 3.608 fillov <$2 → skupaj +$2).

Tedenska evolucija (učenje se pozna): W21 −$16 → W22 −$95 → W23 +$63 → W24 +$159 → W25 +$150 → W26 +$203.

---

## 2. Moja analiza njegovih 5.191 fillov (surovi CSV)

### Stake razredi
| Stake | n | WR | PnL |
|---|--:|--:|--:|
| <$2 (probe) | 3.608 | 78,3% | +$2 |
| $2–5 | 575 | 80,0% | +$84 |
| $5–8 | 62 | 74,2% | −$14 |
| $8–15 | 514 | 73,7% | +$17 |
| **$15+** | **432** | **80,8%** | **+$375** |

### Ure (UTC), samo stave ≥ $8
Zeleno: 09h +$148 (87,2%) · 10h +$217 (86,0%) · 11h +$244 (86,6%) · 08h +$53 · 02/04/06/14/19/20 rahlo +.
Rdeče: **01h −$175 (52,8%!)** · 12h −$75 · 17h −$57 · 22h −$57 · 18h −$37 · 23h −$20.

### Coini
| Coin | BIG (≥$8) | probe | opomba |
|---|---|---|---|
| BTC | n=474, 77,4%, **+$203** | n=1545, +$22 | engine |
| ETH | n=472, 76,5%, **+$189** | n=1512, −$26 | engine #2 (v golden bloku ETH > BTC!) |
| SOL | — (nikoli big) | n=518, −$13 | "basis-noise coin", golden zanj NE dela (−5,2pp pod fair) |
| XRP | — | n=496, **+$71** | najboljša proba, "the keeper" |
| DOGE | — | n=174, +$18 | dodan 30.06 kot $1 proba, 01.07 $5 v golden |

### Price band (vsi filli)
| Band | n | WR | PnL |
|---|--:|--:|--:|
| <0.50 | 60 | 30,0% | −$113 |
| 0.50–0.65 | 541 | 59,1% | +$49 |
| 0.65–0.70 | 430 | 65,3% | +$151 |
| 0.70–0.75 | 661 | 75,5% | +$142 |
| **0.75–0.80** | 932 | 76,5% | **−$158** ← njegov bleed |
| **0.80–0.85** | 923 | 84,8% | **+$292** ← njegovo zlato (BIG: 91,0% WR, +$258) |
| 0.85–0.90 | 1.264 | 86,8% | +$78 (tanek) |
| 0.90+ | 380 | 91,3% | +$24 |

### Timing / trajanje
- Strelja pri T-25..28 (28s okno) — T-25 bucket +$238, T-20 +$177; T-10 −$77.
- **15m: n=608, 81,9%, +$136** — 15m BTC/ETH pri stake ≥$8: **85,1% WR, +$123 na samo 67 fillih**.

---

## 3. Njegov sistem (julian.yaml + config disclosure timeline)

**Filozofija: ure niso filtri, so SIZING TIERI. Nič ni blokirano, vse je stopnjevano.**

- Entry: band 0.50–0.90 (base .env MAX_ASK=0.78, ampak `eff_max_ask()` dvigne na 0.90),
  fill_floor 0.50, **min_move 1,5 bps FLAT čez vse ure** (nikoli hour-varied!), T-28 okno
  (5m in 15m), min_fill T-2, WALK_FILL, DYNAMIC_SIZE.
- Sizing prek `eff_base()` ob strelu (Mon–Thu): **golden 08–11Z: BTC/ETH $25, SOL/XRP $5** ·
  secondary 13/14/19/20Z: BTC/ETH $5, SOL/XRP $1 · off-peak + CEL vikend: $1.
- Brez Paroli lestve (flat stake znotraj tiera); "instant-ladder prov" samo za sizing telemetrijo.
- Guards: DAILY_LOSS_LIMIT $75→$100 (01.07), MAX_LOSS_STREAK=1000 (izklopljen!), max/mkt $25.
- **Friday lockdown** (03.07): petek = samo BTC/ETH $5 v petih urah, ostalo $1 (dopolnjeno isti dan).
- Weekday-gate NI v botu — bot strelja golden sizing tudi ob vikendih (znan bug, vikend 08–11Z ~mrtev);
  Mon–Thu je analitični filter.

Evolucija tierov: 24.06 secondary tier → 27.06 XRP/SOL 24/7 $1 (FLAT_COINS) → 29.06 XRP/SOL $5 ob 13/14Z
→ 30.06 XRP/SOL $5 golden vrnjen + DOGE $1 → 01.07 DOGE $5 v golden.

---

## 4. GOLDEN HOUR — njegova glavna zgodba (kronologija)

1. **12.06 — odkritje:** "hour-of-day is real on my book" — 08–11Z weekday, 92% WR, p=0,0005,
   vseh 6 dni pozitivnih; ves weekday profit v enem 4h bloku.
2. **13.06 — backtest paket:** 08–11Z Mon–Thu $20-flat + MAX_ASK 0.90 = +$338 @ 92,4% (depth-walked).
3. **15.06 — samo-korekcija:** ura POŽRE within-window timing (T-15..20 je bil hour confound —
   pade na Bonferroniju). "Hour subsumes timing — they don't stack."
4. **18.06 — reprodukcijski recept** (ker teammate ni mogel reproducirati): min_move 1,5 FLAT,
   ask 0.50–0.90, $25/$1, T-28. Past #1: stari skripti s trdimi floori (9bps/0.65) ubijejo
   morning edge — **edge živi v POCENI, TANKO-MOVE fillih pri ohlapnih filtrih**. Past #2: timezone
   (08–11 UTC!). Past #3: vikendi v vzorcu. Reproduciran na 9–10 dneh.
5. **24.06 — depth gate v golden:** flat $100 floor je v golden uri ~nevtralen;
   **stake-proporcionalen floor book ≥ 3×stake zmaga** (+$441 vs +$392 no-gate). "$100 team
   pravilo velja za cel dan, jutro hoče proporcionalno."
6. **24.06 — moč jutra NI pre-fire napovedljiva:** testiral vse signale pred 08:00 — NIČ ne napove
   (vse p ≥ 0,16). Slabo jutro (23.06) prepoznaš šele po mehanizmu: reversal tape — final-30s
   sign-flip rate, hiter book reprice, izgube na NAJdražjih/najbolj samozavestnih firih (ask ≥ 0,85).
   Dobro jutro = MIREN, ne-reverzalen tape.
7. **03.07 — GRADIENT audit (n=313):** edge znotraj bloka NI raven: **08Z +2,1pp → 09Z +6,8 →
   10Z +9,9 → 11Z +12,6pp** (PnL +$24/+$99/+$220/+$284). Njegov 08Z pri $25 je na BTC celo
   NEGATIVEN (−$59). ETH v golden +$428 / BTC +$200 / XRP +$28 / **SOL −$28 (golden zanj ne dela)**.
   ROI/$ se je stisnil 24%→8,5%, ko je stake šel $12→$25 (depth strop!) — "več profita na
   depth-capped edgu = realociraj stake proti močnim uram, NE večaj sizea."

---

## 5. Vse njegove lekcije (destilat 50 commitov + 43 LESSONS vnosov)

### Struktura trga / edge
- **Entry band je PER-BOOK** (06-11): njemu je bil takrat 0.70–0.75 engine in <0.65 bleed; drugim drugače.
  (Danes na 5k fillih: 0.80–0.85 zlato, 0.75–0.80 bleed — glej §2.)
- **U-shape move bandov** (06-11/13, replicirano na 3 bookih): 1,5–2,5 bps = engine, **2,5–4 bps =
  bleeder**, 4+ flat. ETH globlji mid-move bleeder od BTC. 1,5–2,5 engine se ob vikendih OBRNE v minus.
- **Fire-timing se NE prenaša med booki — INVERTIRA** (06-11): T-7..14 njegov bleeder, T-15..20 najboljši
  (medtem ko je drugim obratno). Zato nikoli ne kopiraj timing okna.
- **Weekend bleed NI manipulacija** (06-11, forenzika 26 oken): oster last-window underdog counterparty;
  mehanizem nedoločen. Rešitev: vikend = $1.
- **Depth: sub-$100 in-band bleed** (06-11, 3–4 booki) — ampak prava ločnica je
  **in-flight reprice >5c** (06-11): mild collapse OK, >5c = 53% WR, −$2,87/fire = ves weekday damage.
  "Depth is a red herring." Kasneje (07-04): floor-invisible reprice-down class (quoted ≥0.65,
  filled <0.65) = 77 fillov, 55,8% WR, −$71/3 tedne — "the real $ lever", noben entry gate ga ne nadomesti.

### Eksekucija / tehnika
- **Pre-POST price floorji so premagani IN FLIGHT** (06-11): 19 sub-floor fillov kljub floorju v kodi.
- **FOK 400 "fully filled or killed" semantika** v py_clob_client_v2 (06-11) — pazi pri raise-anju.
- **Fire-path serialization** (06-23): so-časni co-fires čakajo v vrsti za vsakim FOK POST-om —
  fire-order-priority fix. (Relevantno za naju s 4 coini!)
- **Nikoli ne restartaj live bota brez diffa** on-disk kode vs tekoči proces (06-11).
- **Scheduled config lahko laže** (06-12): cron-fliped config je 16h tiho odpovedal — enforce v entry checku.
  (lbnpt kasneje: dotenv no-override + inherited environ past.)
- **Phantom mode='LIVE' vrstice** (07-02): failane/unfillane vrstice označene LIVE so napihnile deploy
  evidenco. Audit: `making_amt>0 OR fok_rt_ms IS NOT NULL` = real-fill predicate.
- **Instant-ladder verdict past** (06-13): post-close book je ENOSTRANSKI — beri preživelo stran, ne midpointa.

### Resolucija / grading
- ★ **Chainlink exact resolution cene so berljive ON-CHAIN** (06-18): brez credsov, retroaktivno
  (recept je tudi v našem ghost_nejc/CLAUDE.md). 57% razor confirm.
- **Candle-shadow benchmark** (06-28): T-20s leader-hold 91–93% na vseh 4 coinih po SVEČKAH, ampak
  KOREKCIJA: na TRADED oknih authoritative leader-hold 79,9% vs realized 76,9% = ~3pt execution gap
  (ne 15pt). **Binance-vs-Chainlink basis disagree: SOL 14,0% / XRP 13,3% vs BTC 6,6% / ETH 6,4%
  = strukturni razlog alt bleeda.** (→ za naju: XRP/SOL resolucija po Binance close je 2× bolj "narobe".)
- **Razor grading** (06-25): ~9,4% traded oken = Binance close ≠ PM/Chainlink settlement.
  Grade on settlement or not at all (late-cheap lekcija: feed-grading napihne cheap WR 24→36%).

### Poceni vstopi (⚠️ direktno za najin 0.35 eksperiment!)
- **LATE-CHEAP TEZA OVRŽENA NA REALNIH KNJIGAH** (07-02): paper sim +$2.005 je bil 83% treh
  unfillable lottery fillov; real T-12 sampler: WR 24,4% vs ask 0,360 = cushion −11,5pt.
  **0,35–0,50 je toksično jedro: WR 13,3% vs ask 0,423, EV −$6,76/$10, CI izključuje nič.**
  Cushion pozitiven ŠELE od 0,65+. **Mehanizem = adverse selection:** poceni aski, ki jih NI ujel,
  bi zmagali 93,4%; tisti, ki so se FILLALI, 55,6% (z=6,15) — informed flow pobere dobre,
  tebi ostane smetje. "Any sim/paper result on cheap entries without depth+FOK-kill model is
  structurally inflated." Njegov 0,65 floor = empirični breakeven na njegovem booku.
- **T-4 contrarian** (07-03/04): mehanizem realen (opp-ask decay 0,256→0,138 od T-12→T-4 pri
  konstantni ~28% true win prob), ampak fillable cell Mon–Thu MRTVA — ves cushion en vikend;
  protokol: še 2 vikenda free shadow, šele potem $1 proba. "Rank any hot cell against the full
  grid you searched."

### Izhodi
- **FLIP DETECTION ≠ MONETIZACIJA** (07-03/04, n=492 + 139 loss anatomy): "opposite ask ≥0,50 pri
  T-12" ujame 58,5% izgub pri samo 6,2% false-alarm — AMPAK exit po 1−opp_ask neto **+$1,48/48h**
  (book je flip že prevrednotil, plačaš fair value informacije). Izgube: 32 REV / 40 FLIP /
  45 last-12s (neulovljivi razor) / 22 no-shadow. **Pravilo: vsak exit study, ki šteje "izognjene
  izgube" po polni stavi, pretirava ~2,5×** — vedno ceni unwind po 1−opp_ask ali slabše.
  Telemetrija: loguje `sibling_ask` (opposite best ask v fire()) — en stolpec, ki je omogočil vse to.

### Fees (odprto vprašanje v teamu)
- J-jev operator trdi "Polymarket charges no trading fees" (gross=net) — ampak J3D-jev wallet
  reconcile (03.07) kaže **$41 realnih taker fees** na $210 depozita (452/452 fillov matchanih).
  Model: `fee = 0.072·(1−p)·stake`. Dokler ni potrjeno: računaj z NET-of-fee. (Naš live_ledger to že dela.)

---

## 6. Njegova orodja (metode, vredne kraje)

- `candle_shadow.py` + `candle_grade.py` — keyless razor grader: outcome bere iz resolverjevih
  `won/lost` (= Chainlink) namesto iz feed closa. (Naš resolver + audit_trades delata podobno.)
- Read-only **T-12/8/4 book sampler ("latecap")** joinan na positions po condition_id —
  z njim je naredil loss anatomy in late-cheap refutation. **(= točno to, kar naš novi LASTLAP
  že snema, samo midva na 500ms čez zadnje 4/8 min!)**
- `sibling_ask` stolpec v fire() — opposite best ask ob strelu (100% coverage).
- Wallet reconcile: fill-level diff DB vs Polymarket data-api (`?user=` je taker-only — gotcha).
- Vse config spremembe = **config disclosure commit z UTC timestampom** → čisti era-split pri analizi
  ("always split tier eras before judging a window").

---

## 7. Kaj od tega je ZA NAJU (in kaj ne)

**Potrjuje najino arhitekturo:** njegov $25/$5/$1 hour-tier sizing = najin golden/normal/blocked
ladder sistem (midva ga imava celo bolj čisto: per-tier neodvisne pozicije, env-driven).

**Portable (mehanizmi, ne številke):**
1. ⚠️ **0,35–0,50 cona:** njegovi LIVE podatki pravijo WR 13–30%, adverse selection strukturna.
   Najin MIN_ASK=0.35 paper eksperiment pusti teči, ampak sodi ga po settlement-graded rezultatih
   in računaj, da paper fill ≠ live fill na poceni askih (would_fill past!).
2. **MAX_ASK=0.78 premislek:** njegovo zlato je 0.80–0.85 (91% WR na big), 0.75–0.80 pa bleed.
   Najin novi strop 0.78 je točno na njegovi bleed/zlato meji — ko lastlap nabere podatke,
   preveri najin band 0.78–0.85.
3. **Gradient znotraj golden bloka** — ne raven blok: najin lastlap+shadow lahko isto izmeri
   za najine ure (npr. BTC 13–16 blok: je 16h res vrh?).
4. **15m BTC/ETH:** pri njem 85% WR na big stakes; najin shadow BTC 15m +16,3% — kandidat za un-skip.
5. **Vikend = $1** (ali najin blocked ladder) — 3 booki potrjujejo weekend bleed.
6. **Stake-proporcionalen depth floor (K=3)** namesto flat $100 za golden ure.
7. **Price the exit** (1−opp_ask) — če kdaj gradiva T-12 abort/exit, nikoli ne šteti po polni stavi.
8. **Real-fill predicate + era-split + settlement grading** v vseh najinih analizah.
9. **Alt basis disagree** (SOL/XRP 13–14%): za XRP/SOL še bolj velja on-chain resolucija (naš resolver ✓).

**NE kopiraj:** njegovih ur (08–11Z je NJEGOV book; najin BTC kaže 13–16 + 08/11), njegovega
banda, njegovega T-28 (naš je itak 28s, ampak iz lastnih razlogov), njegovega min_move 1,5.
Vse najprej skozi najin shadow/lastlap.

---

## 8. Viri v Ghins repo (za kasneje)

- `data/data/julian/fills.csv` — surovi filli (dnevni auto-sync)
- `data/configs/julian.yaml` — verified live config (25.06)
- `reports/2026-06-18-golden-hour-reproduction.md` — recept + pasti
- `reports/2026-06-25-J-golden-hour.md` — en golden dan fill-by-fill + shadow-grader metoda
- `docs/Weekly_Audit_J_2026-06-15.md` — teden 8–15.6: band/depth/hour/coin strukture
- `reports/2026-07-04-julian-t12-losses-part1.md` — 139 izgub, REV/FLIP/last-12s anatomija
- teamski `CLAUDE.md` — 43 [J] LESSONS vnosov (06-11 → 07-04)
