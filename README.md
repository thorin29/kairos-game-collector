# Kairos game-time collector

A standalone service that watches each kid's game activity and feeds **resolved
per-person daily totals** into Kairos, which owns the long-term history. Kairos
never holds any game-service credentials — the collector is the only thing that
talks to Home Assistant / Steam.

```
Xbox (Home Assistant integration) ─┐
Family Safety balance (HA)         ├─► collector (normalize → resolve) ─► Kairos POST /api/v1/game-time/ingest
Steam Web API (direct poll)       ─┘         │
                                             └─ local SQLite buffer (/data/game_playtime.db)
```

It's one file — `game_collector.py` — with no third-party runtime deps beyond
`websockets`. See `DECISIONS.md` for the reasoning (and the rejected
alternatives) behind everything below.

---

## What it does

- **Xbox** via a Home Assistant WebSocket subscription. It self-discovers each
  friend's per-friend sensors from the HA Xbox integration: `online` (binary),
  `in_game` (binary), `now_playing` (sensor, with a `platform` attribute like
  `Xbox One` / `Windows`), `last_online`, `gamer_score`, `has_game_pass`, plus a
  gamerpic. Also reads the Microsoft Family Safety `*_balance` sensors.
- **Steam** via the official Web API (`GetPlayerSummaries`, polled every 60s) for
  Steam-primary kids. No HA dependency for Steam — it has a clean API.
- Resolves each kid's **actual game-session time** for today and every ~5 min
  pushes the resolved totals + per-game breakdown + status + platform icons to
  Kairos (idempotent). Buffers everything in SQLite, pruned to `RETAIN_DAYS`.

---

## The detection model (the important part)

**Online presence is not gameplay.** Xbox Network "online" is *account*
presence: a kid can be "online" from an idle PC Xbox app, Game Pass, or lingering
presence with no game running. So a kid who is merely online logs **zero**.
`online_sessions` are still recorded, but only as diagnostics — they never count.

**The credited daily total is the union of the kid's game-session intervals.** A
game session records only while `online == True` **and** `now_playing` has a title
**and** `in_game` is not explicitly off. `online` gates; `now_playing`/`in_game`
decide *which* game. A stale `now_playing` can therefore never manufacture time,
because a session can't open unless the online sensor says on.

**The reconnect bridge (coalescing).** Game presence blinks constantly — the
title drops to `None`, `in_game` flips off, the account briefly goes offline. A
stopped game is held **pending** rather than closed. If the **same game** returns
within the reconnect window, the two pieces are rejoined and the gap between them
is counted as play (evidence-based: the kid was in that game before *and* after).
If it never returns, the session finalizes at the drop with **nothing added** —
no time is invented past the last real game signal.

**Per-purpose coalesce gaps.** Each coalescer takes its own `gap`:

| Coalescer | Gap | Constant / env |
|-----------|-----|----------------|
| Xbox **game** reconnect | **8 min** | `XBOX_GAME_GAP` ← env `XBOX_GAME_GAP_MIN` (default 8) |
| Xbox **online** diagnostic | 5 min | `COALESCE_GAP` |
| Steam game / online | 5 min | `COALESCE_GAP` |

The Xbox *game* window is wider because a console/PC's presence can blink offline
for several minutes mid-game; Steam reports the title directly and keeps the
tighter 5-min default so a real Steam break isn't swallowed.

**Steam-primary precedence.** A kid with a Steam id configured is Steam-primary.
Their daily total is a **union with precedence**: Steam (weight 2) wins any
minute where Steam and Xbox overlap; genuinely separate Xbox / Game Pass play
still counts. `resolve_precedence(intervals)` returns
`(total, per_game, per_source, per_game_plat)`.

**Sources and platform come from the winners.** The per-system icons (`sources`)
and each game's displayed `platform` are derived from the **winners** of the
precedence merge — not from every row that shared a title. So a Steam-won title
shows Steam only, with no phantom Xbox device attached; a genuinely separate Xbox
game shows Xbox.

**Platform metadata is session-scoped.** Each coalescer session carries a `meta`
slot — the platform captured when that session opened. It travels with that
session through reconnect bridges and is never overwritten by a later game or
device. There is no per-friend "last platform" global.

> **Known limit:** on Windows the `now_playing` title frequently drops to `None`
> or reports `is_game=false` even during real play (party chat can steal the
> "now playing" slot on the same PC), and HA collapses a kid's multi-device
> presence into one per-friend signal + a platform attribute. The bridge recovers
> same-game gaps, but there is no signal that cleanly separates "playing, title
> dropped" from "idle with the app open" on the same PC. This is an
> HA-integration limit, not a collector bug — see `DECISIONS.md`.

---

## Reliability behaviors

- **HA reconnect uses `suspend()`, not `flush_all()`.** A dropped WebSocket is
  transient: `suspend()` stops open sessions into *pending* without flushing, so a
  reconnect that re-lands the same game within the window bridges across the
  outage. A longer outage lets the pending expire at the disconnect with no
  invented time. (`flush_all()` is reserved for a deliberate exit.)
- **Graceful shutdown.** On `SIGTERM` (Docker stop/restart/update) or `SIGINT`:
  flush every open/pending session **to SQLite first** — finalized at the
  shutdown time, *not* via the reconnect bridge — then make **one best-effort
  push** to Kairos, then exit. A failed or slow push never blocks shutdown or
  alters the persisted rows; the restarted collector sends them next cycle. This
  closes the "a kid's number drops on restart" failure mode for normal Docker
  stops. It does **not** protect against `kill -9` / power loss / kernel panic
  (that would need periodic checkpointing — deliberately not added).
- **Bounded HTTP timeouts** so a hung request can't drag out shutdown: normal
  Kairos push **5s**, Steam poll **8s**, shutdown push **5s** (with a 7s outer
  `wait_for` guard). `asyncio.run()` blocks on the worker thread at exit, so the
  per-request timeout — not the outer `wait_for` — is what actually bounds it.
- **DB access is single-threaded.** The SQLite connection lives on the event-loop
  thread; the payload is built there and only the blocking HTTP POST goes to a
  worker thread. (Reading SQLite from a worker thread raises "SQLite objects
  created in a thread can only be used in that same thread.")

---

## Storage & config

SQLite at `DB_PATH` (default `game_playtime.db` next to the script; set
`/data/game_playtime.db` on the mounted volume under Docker). Two tables:

- `online_sessions(id, friend, source, start_utc, end_utc, minutes)` — diagnostic only.
- `game_sessions(id, friend, source, game, start_utc, end_utc, minutes, platform)` — the credited sessions.

Both are pruned on startup and once a day: rows older than `RETAIN_DAYS` (default
14) are deleted and the file is `VACUUM`ed, so it stays flat (~hundreds of KB)
forever. The local DB is only a buffer — Kairos keeps the durable history.

### Environment variables

| Var | Required | Default | Notes |
|-----|----------|---------|-------|
| `HA_URL` | for Xbox | — | Home Assistant LAN address (not the public domain) |
| `HA_TOKEN` | for Xbox | — | HA long-lived access token |
| `STEAM_API_KEY` | for Steam | — | free key from steamcommunity.com/dev/apikey |
| `STEAM_IDS` | for Steam | — | `Name=SteamID64`, comma-separated |
| `KAIROS_URL` | to push | — | Kairos **internal** address, not the public domain |
| `GAMETIME_INGEST_TOKEN` | to push | — | shared secret, identical to Kairos's value |
| `DB_PATH` | no | `<script dir>/game_playtime.db` | set `/data/game_playtime.db` under Docker |
| `TZ_NAME` | no | `America/Chicago` | local day boundary for rollups |
| `RETAIN_DAYS` | no | `14` | prune sessions older than this |
| `ONLY` | no | — | comma list of friend names to restrict to |
| `XBOX_GAME_GAP_MIN` | no | `8` | Xbox same-game reconnect window, minutes |

Fixed timing constants (not env): online/Steam coalesce gap 5 min, flush loop
20s, Steam poll 60s, push interval 300s.

---

## How it pushes to Kairos

`POST {KAIROS_URL}/api/v1/game-time/ingest`, auth header `X-Ingest-Token:
<GAMETIME_INGEST_TOKEN>` (Kairos also accepts `Authorization: Bearer`). Body:

```json
{
  "days": [
    {
      "friend": "Ethan",
      "date": "2026-09-19",
      "minutes": 143,
      "games": [ { "game": "Grounded", "minutes": 143, "platform": "Windows" } ],
      "platforms": ["steam"],
      "status": { "gamerscore": 1240, "gamerpic": "https://…",
                  "hasGamePass": true, "msBalance": "$12.50" }
    }
  ]
}
```

Response: `{ "matched": [...names], "unmatched": [...] }`. The push is
**idempotent** — Kairos upserts the day and replaces the whole game breakdown, so
re-sending "today so far" never accumulates, and a corrected push clears stale
games. In-progress (open + pending) sessions are included so a kid who is still
playing shows current time rather than 0; those rows are never written to SQLite,
so there's no double-count. Kairos resolves each person by gamertag → steamId →
name; a friend with no matching Kairos person comes back as `unmatched`.

---

## Usage

```
python game_collector.py            # run live
python game_collector.py --summary  # today's per-kid totals from the local DB
python game_collector.py --selftest # engine + resolver + regression tests (must pass)
```

`--selftest` covers a real ~47-minute session regression, reconnect-bridge edge
cases, HA-disconnect handling, and source/platform precedence. Keep it green.

## Deploy (Unraid, via image + auto-update)

Push to `main` → GitHub Actions builds and pushes
`ghcr.io/thorin29/kairos-game-collector:latest`. Point the Unraid container at
that image and enable auto-update.

- Repository: `ghcr.io/thorin29/kairos-game-collector:latest`
- Volume: host `/mnt/user/appdata/game-collector` → container **`/data`**, with
  `DB_PATH=/data/game_playtime.db`
- Env vars: see the table above.
