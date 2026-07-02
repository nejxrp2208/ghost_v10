# VPS Setup & Migration Runbook

> Cilj: če najdeš boljši VPS, da **takoj veš kaj narediti**. Vse — kako je postavljeno,
> requirementi, in koraki za selitev na nov server, brez izgube zgodovine.
>
> **2 ZLATI PRAVILI selitve:**
> 1. **Koda gre prek gita** (`git clone`), **`.env` in `.db` NE** — oba sta gitignorana,
>    obstajata SAMO na VPS-u. Ob selitvi ju moraš ročno prenesti (scp).
> 2. **PAPER ostane PAPER.** Live creds se nikoli ne pushajo; vneseš jih ročno v `.env`.

---

## 0. PRED ugasnitvijo VPS-a — BACKUP (KRITIČNO)

⚠️ **`.env` in `.db` niso v gitu.** Če VPS UNIČIŠ (ne samo ustaviš), so izgubljeni:
trade zgodovina, **ladder state**, config, live creds. Runbook zgradi kodo, NE podatkov.

**"Ustavi" (stop) vs "uniči" (destroy):**
- **Stop** → disk ostane, podatki živi, ob ponovnem zagonu vse deluje kot prej (samo `pm2 resurrect`). Ampak marsikje še vedno plačuješ.
- **Uniči/delete** → vse zbrisano, plačila stop. **Rabiš backup + poln rebuild iz tega runbooka.**

**Backup ZDAJ (dokler je VPS živ):**
```bash
# NA VPS-u: zapakiraj .env + vse predator DB-je (majhno, ~7MB):
cd /root/ghost_v10
tar czf /root/gp_backup_$(date +%Y%m%d).tar.gz ghost_predator/.env ghost_predator/*.db
# (opcijsko v10, brez ogromne scanner.db):
tar czf /root/v10_backup_$(date +%Y%m%d).tar.gz $(ls *.db 2>/dev/null | grep -v scanner.db) 2>/dev/null
ls -lh /root/*_backup_*.tar.gz
```
```powershell
# NA LOKALNEM kompu (potegne backup dol, shrani NA VARNO — ne samo C:\tmp):
scp root@70.34.204.152:/root/gp_backup_*.tar.gz C:\Users\nejcj\Downloads\vps_backup\
scp root@70.34.204.152:/root/v10_backup_*.tar.gz C:\Users\nejcj\Downloads\vps_backup\
```

**Restore (na novem VPS-u, PO koraku 2 clone):**
```bash
cd /root/ghost_v10
# prenesi tarball na novi VPS (z lokalnega): scp C:\...\gp_backup_*.tar.gz root@NOVI:/root/
tar xzf /root/gp_backup_*.tar.gz          # razpakira .env + DB-je nazaj na pravo mesto
# potem nadaljuj z venv/pip (korak 3) in pm2 start (korak 6)
```

---

## 1. Trenutni VPS (stanje 2026-06-27)

| | |
|---|---|
| SSH | `root@70.34.204.152` (hostname `polybot`) |
| OS | Ubuntu 24.04 · Stockholm |
| Koda | `/root/ghost_v10` |
| Python venv | `/root/ghost_v10/venv` (`/root/ghost_v10/venv/bin/python3`) |
| Git remote | `https://github.com/nejxrp2208/ghost_v10.git` (branch `master`) |
| Process manager | **PM2** (rabi Node.js) |

---

## 2. Kaj teče na VPS-u (inventory)

### PM2 procesi
```
# ── GhostScanner v10 ──────────────────────────────
marketghost              # marketghost.py            — silent data collector
scanner4                 # crypto_ghost_scanner4.py  — Scanner 4 (s4_reversal)
resolver                 # crypto_ghost_resolver.py  — v10 resolver
redeemer                 # crypto_ghost_redeemer.py  — on-chain redeemer

# ── GHOST PREDATOR ────────────────────────────────
ghost-predator           # ghost_predator/ghost_predator.py   — snipe engine
ghost-predator-resolver  # ghost_predator/resolver.py         — on-chain CTF settlement
ghost-predator-alarm     # ghost_predator/alarmghost.py       — Telegram health alarm
ghost-predator-radar     # ghost_predator/divergence_scan.py  — Reversal Radar
ghost-predator-firstmover# ghost_predator/firstmover.py       — first-mover gate
ghost-predator-shadow    # ghost_predator/ghost_predator_shadow.py — shadow collector (BTC/ETH/XRP/SOL)
```
> (ghost-lattice procesi so ODSTRANJENI 2026-06; koda izbrisana iz repa.)

### Cron joby
```
5 * * * *   hour_scan.py append        # urni backfill Hour Scanner
# + (preveri z `crontab -l`): ledger_report.py daily, watchdog vsako minuto, hourly monitor :07
```

### `.env` datoteke (GITIGNORANE — niso v gitu!)
| Pot | Vsebina |
|---|---|
| `/root/ghost_v10/ghost_predator/.env` | predator config + Telegram + (live) creds — kopija iz `.env.example` |
| `/root/ghost_v10/.env` (če obstaja) | v10 scanner config (preveri) |

### DB datoteke (GITIGNORANE — vsebujejo zgodovino!)
| DB | Kaj | Selitev? |
|---|---|---|
| `ghost_predator/ghost_predator.db` | main predator trade-i (paper+live) | **scp** (ladder state, stats) |
| `ghost_predator/shadow_predator.db` | shadow 4-coin data (~5200 fillov) | **scp** (Task B podatki) |
| `ghost_predator/divergence.db` | Reversal Radar | scp ali fresh |
| `ghost_predator/hour_scan.db` | Hour Scanner (~43k) | scp ali fresh |
| `crypto_ghost_PAPER.db` | scanner4 trade-i | scp ali fresh |
| `marketghost.db` | market snapshots | opcijsko |
| `scanner.db` | book/summary — **~300MB+, velik** | opcijsko (počasen prenos) |

---

## 3. Requirementi (kar mora nov VPS imeti)

**Sistem (apt):**
```bash
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip curl
# Node.js + npm (za PM2):
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo bash -
sudo apt install -y nodejs
sudo npm install -g pm2
```

**Python pip (v venv):**
```
aiohttp        # async HTTP (predator, marketghost, scanner)
rich           # dashboard TUI
py-clob-client-v2   # SAMO za live trading (PAPER ga ne rabi)
```
> Ostalo je stdlib (`urllib`, `sqlite3`, `asyncio`) — resolver/divergence/hour_scan ne rabijo pip dep.

---

## 4. Migracija STARI → NOVI VPS (po korakih)

```bash
# ── NA NOVEM VPS-u ──────────────────────────────────────────────
# 1) sistem deps (glej razdelek 3) — git, python3-venv, nodejs, pm2

# 2) koda
cd /root && git clone https://github.com/nejxrp2208/ghost_v10.git
cd /root/ghost_v10

# 3) venv + pip
python3 -m venv venv
./venv/bin/pip install aiohttp rich
# (live only: ./venv/bin/pip install py-clob-client-v2)

# 4) .env — kopiraj s STAREGA VPS-a (ali na novo iz .env.example)
#    z LOKALNEGA kompa kot most, ALI direktno stari->novi:
#    scp root@STARI:/root/ghost_v10/ghost_predator/.env  ./ghost_predator/.env
#    (drugače: cp ghost_predator/.env.example ghost_predator/.env  pa izpolni)

# 5) DB-ji — prenesi s STAREGA VPS-a (da ohraniš zgodovino + ladder state)
#    scp root@STARI:/root/ghost_v10/ghost_predator/ghost_predator.db   ./ghost_predator/
#    scp root@STARI:/root/ghost_v10/ghost_predator/shadow_predator.db  ./ghost_predator/
#    (divergence.db, hour_scan.db, crypto_ghost_PAPER.db po želji)
#    Če ne preneseš: DB se ustvari prazna ob prvem zagonu (izgubiš zgodovino).

# 6) zaženi procese
pm2 start ghost_predator/ghost_predator.py   --name ghost-predator           --interpreter ./venv/bin/python3
pm2 start ghost_predator/resolver.py         --name ghost-predator-resolver  --interpreter ./venv/bin/python3
pm2 start ghost_predator/alarmghost.py       --name ghost-predator-alarm     --interpreter ./venv/bin/python3
pm2 start ghost_predator/divergence_scan.py  --name ghost-predator-radar     --interpreter ./venv/bin/python3
pm2 start ghost_predator/firstmover.py       --name ghost-predator-firstmover --interpreter ./venv/bin/python3
pm2 start ghost_predator/ghost_predator_shadow.py --name ghost-predator-shadow --interpreter ./venv/bin/python3
# v10 (po potrebi):
pm2 start crypto_ghost_scanner4.py --name scanner4   --interpreter ./venv/bin/python3
pm2 start crypto_ghost_resolver.py --name resolver   --interpreter ./venv/bin/python3
pm2 start crypto_ghost_redeemer.py --name redeemer   --interpreter ./venv/bin/python3
pm2 start marketghost.py           --name marketghost --interpreter ./venv/bin/python3

# 7) persist + auto-start ob reboot-u
pm2 save
pm2 startup        # izpiše ukaz ki ga moraš pognati (systemd hook)

# 8) cron joby
( crontab -l 2>/dev/null; echo "5 * * * * /root/ghost_v10/venv/bin/python3 /root/ghost_v10/ghost_predator/hour_scan.py append >> /tmp/hour_scan.log 2>&1" ) | crontab -
#   (+ ostali cron joby s starega VPS-a: crontab -l na starem, prekopiraj)
```

---

## 5. Verifikacija po selitvi

```bash
pm2 list                                    # vsi procesi 'online', 0 restartov
pm2 logs ghost-predator --lines 20 --nostream      # ni traceback
pm2 logs ghost-predator-resolver --lines 20 --nostream

# on-chain RPC test Z NOVEGA VPS-a (nekateri RPC-ji zavračajo določene host-e!):
cd /root/ghost_v10/ghost_predator && /root/ghost_v10/venv/bin/python3 -c "import resolver; [print(r, resolver.eth_call(resolver.SEL_DEN+'0'*64, r)) for r in resolver.POLY_RPCS]"
#   0 = RPC dosegljiv, None = blokiran. Rabiš vsaj 2 z '0'.

# dashboard:
source venv/bin/activate && python3 ghost_predator/dashboard.py
```

**Banner mora pokazati `PAPER` (ne `*** LIVE ***`)** dokler zavestno ne preklopiš.

---

## 6. Gotcha-ji (naučeni to sejo)

- **Prazen `.env` ključ sesuje bot:** `MAX_LOSS_STREAK=` (brez vrednosti) je crashal `int('')`. Popravljeno (`envi/envf` zdaj `or d`), a vseeno **ne puščaj praznih ključev** — daj vrednost.
- **RPC-ji so host-odvisni:** publicnode / 1rpc.io / drpc.org so na Stockholm VPS-u vračali `None`; delovali so pokt.nodies / tenderly / onfinality. **Vedno poženi RPC test z novega host-a.**
- **DB + .env nista v gitu** — `git pull` ju NE prinese. Pri selitvi obvezno scp.
- **PM2 brez `pm2 startup`** se po reboot-u ne zažene sam — ne pozabi koraka 7.

---

## 7. Zajemi TOČNO trenutno stanje (poženi na starem VPS-u, posodobi ta dok)

```bash
echo "=== OS ==="; lsb_release -d; uname -m
echo "=== node/pm2/python ==="; node -v; pm2 -v; /root/ghost_v10/venv/bin/python3 -V
echo "=== pip freeze ==="; /root/ghost_v10/venv/bin/pip freeze
echo "=== pm2 procesi ==="; pm2 list
echo "=== cron ==="; crontab -l
echo "=== .env datoteke ==="; find /root/ghost_v10 -name ".env" -not -path "*/venv/*"
echo "=== DB-ji + velikosti ==="; find /root/ghost_v10 -name "*.db" -not -path "*/venv/*" -printf "%s\t%p\n" | sort -rn
echo "=== disk ==="; df -h /
```
Prilepi izpis → posodobiva razdelke 1–3 z resničnimi verzijami.
