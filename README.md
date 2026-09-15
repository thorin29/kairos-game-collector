# Kairos game-time collector

A standalone service that watches game activity for the household's kids and
feeds **resolved daily totals** into Kairos, which owns the long-term history.
Kairos never holds any game-service credentials — the collector is the only
thing that talks to Home Assistant / Steam.

```
Xbox (Home Assistant integration) ─┐
Family Safety balance (HA)         ├─► collector (normalize + resolve) ─► Kairos /api/v1/game-time/ingest
Steam Web API (direct)            ─┘         │
                                             └─ local SQLite buffer (/data/game_playtime.db)
```

See `DECISIONS.md` for the reasoning behind the design.

## What it does
- **Xbox** via Home Assistant (self-discovers each friend's `online`, `in_game`,
  `now_playing`, `last_online`, `gamer_score`, `has_game_pass`).
- **Steam** via the official Web API (`GetPlayerSummaries`) for Steam-primary kids.
- **Family Safety** balance from the `..._family_safety_..._balance` HA sensors.
- Coalesces flap fragments; games from `in_game`+`now_playing`; `last_online`
  end cross-check; the **online sensor is authoritative** (a stale `now_playing`
  never manufactures time). Steam-primary merge unions Steam + non-overlapping Xbox.
- Every 5 min pushes today's resolved totals + games + status + platforms to
  Kairos (idempotent). Buffers in SQLite, pruned to `RETAIN_DAYS` (default 14).

## Deploy (Unraid, via image + auto-update)
Push to `main` → GitHub Actions builds and pushes
`ghcr.io/thorin29/kairos-game-collector:latest`. Point the Unraid container at
that image and enable auto-update, so new builds deploy on their own.

Container settings:
- Repository: `ghcr.io/thorin29/kairos-game-collector:latest`
- Volume: host `/mnt/user/appdata/game-collector` → container **`/data`** (holds
  `game_playtime.db`; `DB_PATH` already defaults to `/data/game_playtime.db`)
- Env vars: see the table below.

> Migrating from the old bind-mounted-script container: copy the existing
> `game_playtime.db` into the `/data` volume folder so history carries over.

## Environment variables
| Var | Required | Default | Notes |
|-----|----------|---------|-------|
| `HA_URL` | for Xbox | — | Home Assistant, LAN address (not the public domain) |
| `HA_TOKEN` | for Xbox | — | HA long-lived access token |
| `STEAM_API_KEY` | for Steam | — | free key from steamcommunity.com/dev/apikey |
| `STEAM_IDS` | for Steam | — | `Name=SteamID64`, comma-separated |
| `KAIROS_URL` | to push | — | Kairos **internal** address, not the public domain |
| `GAMETIME_INGEST_TOKEN` | to push | — | shared secret, identical to Kairos's value |
| `DB_PATH` | no | `/data/game_playtime.db` | on the mounted volume |
| `TZ_NAME` | no | `America/Chicago` | local day for rollups |
| `RETAIN_DAYS` | no | `14` | prune sessions older than this |
| `ONLY` | no | — | comma list of friend names to restrict to |

## Usage
```
python game_collector.py            # run live
python game_collector.py --summary  # today's per-kid totals from the local DB
python game_collector.py --selftest # engine + resolver + balance-format tests
```
