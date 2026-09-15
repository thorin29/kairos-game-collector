# Collector decisions

Hard-won choices behind `game_collector.py`. Read before changing the engine.

## Architecture
- **Monitoring only** — no tokens, allowances, or manual logging. The old Kairos
  allowance model was retired.
- **The collector is the stable contract.** Sources (HA/Xbox, Steam, Family
  Safety) live here; Kairos only receives resolved numbers. Kairos never holds
  game-service credentials.
- **HA is used only for what it earns** — the fragile Xbox / Family Safety auth.
  Steam has a clean official API, so the collector polls Steam **directly**
  rather than through an HA integration (fewer moving parts, no HA dependency).
- **Kairos owns durable history** (Postgres). The local SQLite file is a
  disposable buffer, pruned to `RETAIN_DAYS` (default 14).

## Session engine
- **Xbox time is actual gameplay, not account presence.** The daily Xbox total is
  the **union of qualifying game sessions** (the same basis Steam-primary uses),
  for *every* kid — not the Xbox-Network `online` sensor. `online` is only a gate
  (a game session can't start unless the account is online) and a diagnostic
  (`online_sessions` are still recorded but never counted). Reason: Xbox Network
  "online" is *account* presence and can come from a PC Xbox app, Game Pass, or
  lingering presence with no game running — counting it once gave a kid 274 min
  on a day he played nothing. A kid who is merely online now logs 0, so a stale
  bogus total is replaced by 0 on the next push with no manual clearing.
- **`online` gates game sessions; `now_playing`/`in_game` decide which game.** A
  stale `now_playing` can't manufacture time because a game session requires
  `online == True`. The snapshot records OFFLINE explicitly on startup so stale
  presence can't leak in. (This fixed the earlier ~6h-zero-play bug; the
  online-as-total bug above was a separate, later fix.)
- **Console-only via the `now_playing` `platform` attribute.** A game session is
  dropped when its platform is a *known* non-Xbox device (Windows/Android/iOS/
  Nintendo Switch/web), so PC Game-Pass play isn't attributed to the console
  metric. Unknown/absent platform is allowed (benefit of the doubt) so a missing
  attribute never zeroes real console play. Captured on both the startup snapshot
  and `state_changed` events.
- **Coalescing** — online/game sessions split by ≤ `COALESCE_GAP` (5 min) merge
  into one, and the gap counts as time. Kills presence-flap fragmentation.
- **Games = `in_game` + `now_playing`** — active-play boundary from `in_game`
  (falling back to `now_playing` presence when `in_game` isn't reported),
  labelled by `now_playing`. Distinguishes real play from a title left on the
  dashboard.
- **`last_online` end cross-check** — presence polls ~every 30s, so the
  "went offline" event lags; the online session's end is trimmed to Microsoft's
  own last-seen timestamp when it's more precise.
- **Everything is quantized to the ~30s poll**, so edges are ±30s and sub-minute
  sessions are noise; coalescing smooths this.

## Steam & the merge
- A kid with a Steam id configured is **Steam-primary**. Their total is the
  **union** of Steam gameplay and any Xbox gameplay that does *not* overlap Steam
  (Steam wins overlapping minutes), and Xbox online-only is dropped. This stops
  double-counting the Xbox-app-on-PC "online" that shows while they're in a
  Steam game. Precedence resolver: Steam (priority 2) > Xbox (priority 1).
- For Steam, "online" is derived from being in a game (`gameextrainfo`), so the
  Steam online session brackets Steam gameplay — no always-on Steam-client noise.

## Push to Kairos
- Pushes **today's resolved totals** (Steam-primary merge applied) + per-game
  breakdown + per-person status + platforms, every 5 min. **Idempotent** — Kairos
  upserts/replaces the day, so re-sending "today so far" is safe and never
  accumulates. Kairos also replaces the day's whole game breakdown, so a
  corrected push clears stale games.
- **In-progress (open + pending) sessions are included** in the push, so a kid
  who's still playing shows current time instead of 0. They're never in the DB,
  so no double count.
- **DB reads happen on the event-loop thread only**; only the blocking HTTP POST
  runs in a worker thread. Reading SQLite from a worker thread threw
  "SQLite objects created in a thread…" — the connection is single-thread.
- **`KAIROS_URL` must be the internal address**, not the public domain — a
  LAN-to-LAN collector shouldn't hairpin out through Cloudflare. Auth is the
  `GAMETIME_INGEST_TOKEN` header (not Authelia), so going direct is fine.
- **Identity mapping** — Kairos resolves each person by gamertag → steamId →
  name; the kids' Kairos names match the collector's friend names, so it links
  with no config. A parent friend (no Kairos person) shows up as `unmatched`.

## Platforms (per-system icon)
- Platforms = distinct `source` values across a person's sessions. A
  Steam-primary kid shows **steam only** (their overlapping Xbox time is
  suppressed anyway); others show **xbox** from their real sessions. Automatic —
  follows real usage.

## Storage
- **`DB_PATH` defaults next to the script**, not `./`. The stock `python` image
  runs with the working dir at `/`, so `./game_playtime.db` landed at the
  container root (ephemeral, invisible in the Unraid file browser, wiped on
  recreate). Resolving relative to the script puts it in the bind-mounted `/app`.
- **Retention prune** — on startup and once a day, delete sessions older than
  `RETAIN_DAYS` and `VACUUM`. Keeps the file flat (~hundreds of KB) forever.
