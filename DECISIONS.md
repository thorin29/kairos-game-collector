# Collector decisions

Hard-won choices behind `game_collector.py`, each with its rationale and the
alternative that was tried and rejected. Read this before changing the engine.
**The timing/coalescing logic is frozen pending real-world observation — do not
change it without new evidence** (see ROADMAP.md).

---

## Architecture

- **Monitoring only.** No tokens, allowances, or manual logging — the old Kairos
  allowance model was retired. Time is pulled automatically and shown for
  awareness, not enforced.
- **The collector is the stable contract.** All source integrations (HA/Xbox,
  Steam, Family Safety) live here; Kairos only receives resolved numbers and
  never holds game-service credentials.
- **HA is used only for what it earns** — the fragile Xbox / Family Safety auth.
  Steam has a clean official API, so the collector polls Steam **directly** rather
  than through an HA integration (fewer moving parts, no HA dependency).
- **Kairos owns durable history** (Postgres: GameDay / GameDayTitle / PlayerCard).
  The local SQLite file is a disposable buffer, pruned to `RETAIN_DAYS`.

## Count game-session time, not Xbox "online" presence

- **Decision.** The credited daily total is the **union of the kid's game
  intervals** — the same basis Steam-primary uses — for every kid. `online` is
  only a gate (a session can't open unless the account is online) and a diagnostic
  (`online_sessions` are recorded but never counted).
- **Why.** Xbox Network "online" is *account* presence: a PC Xbox app, Game Pass,
  or lingering presence with no game running all read as online. Counting it once
  gave a kid **274 min on a day he played nothing**. A kid who is merely online
  now logs 0, so a stale bogus total is replaced by 0 on the next push with no
  manual clearing.
- **Rejected: crediting online presence.** The original design; it manufactured
  false hours. Removed in favor of the game-interval union.

## The 8-minute same-game reconnect bridge — not a "forward grace"

- **Decision.** When the game signal drops, hold the session **pending**. If the
  **same game** is confirmed again within the reconnect window, rejoin the two
  pieces and count the gap between them. If it never returns, finalize at the drop
  and add nothing.
- **Why this shape.** The evidence is symmetric: the kid was confirmed in that
  game *before and after* the gap, so the gap was almost certainly play. A real
  ~47-minute session reconstructed to **46.2 min** purely by bridging two
  mid-session presence outages (6m27s and 5m16s — both just over the old 5-minute
  window) with the same game confirmed on both sides, adding nothing to the idle
  tail. Widening the *game* reconnect window to 8 minutes recovers exactly that,
  with no invented time.
- **Rejected: a forward grace.** An earlier attempt added a 5-minute "keep
  counting while still online" grace *after* the last game signal. That fixed the
  wrong end — it invented unverified time on the tail and compounded with the
  coalesce window to bridge ~10 min. **Removed.** The bridge only ever fills a gap
  that is bracketed by the same game on both sides.

## Per-purpose coalesce gaps

- **Decision.** The `Coalescer` takes a per-instance `gap`. The Xbox *game*
  reconnect window is 8 min (`XBOX_GAME_GAP`, env `XBOX_GAME_GAP_MIN`); the Xbox
  *online* diagnostic and the *Steam* coalescers keep the 5-min `COALESCE_GAP`.
- **Why.** Xbox console/PC presence blinks offline for minutes mid-game, so its
  game window needs to be wide. Steam reports the title directly, so a tighter
  window there avoids swallowing a genuine Steam break.

## Platform metadata is session-scoped

- **Decision.** The platform is captured on the coalescer **session** (`meta`)
  when it opens, travels with that session through bridges, and is never
  overwritten by a later game/device.
- **Why.** A per-friend "last platform" global got clobbered when a new game
  started before the old one flushed — moving it onto the session fixed the
  mislabeling.

## Sources & platform from the merge winners

- **Decision.** `sources` (the per-system icons) and each game's `platform` are
  taken from the **winners** of `resolve_precedence`, not from every row that
  shared a title.
- **Why.** When Steam wins an overlapping minute, the losing Xbox presence for the
  same instant shouldn't attach a phantom Xbox device to the title. A genuinely
  separate Xbox game still surfaces Xbox because it wins its own minutes.

## PC/Windows presence is unreliable (irreducible)

- **Observation.** On Windows the `now_playing` title frequently drops to `None`
  or reports a non-game title even during real play (party chat can steal the
  "now playing" slot on the same PC). HA diagnostics confirmed PC entries
  reporting non-game titles.
- **Consequence.** There is no signal that separates "playing but the title
  dropped" from "idle with the app open" on the same PC. The bridge recovers
  *same-game* gaps; the residual is an HA-integration limit, not a collector bug.

## HA collapses multi-device presence — the "phantom" device

- **Observation.** When a kid's account is live on two devices (e.g. a console
  they're playing on plus a PC with the Xbox app idling in party chat), HA
  collapses them into one per-friend signal + a platform attribute that
  flip-flops. That phantom idle device is why a console-only kid once showed
  `platform=Windows` lines.
- **Handled by** the bridge + session-scoped platform + source/platform-from-
  winners.
- **Rejected (deferred): a full cross-device phantom guard** (compare incoming
  platform vs the active game's and reject mismatches). Considered and
  deliberately **not built** — it's rare and doesn't affect credited minutes. See
  ROADMAP.md for the evidence-gated version.

## Reliability

- **`suspend()` on HA reconnect, not `flush_all()`.** A transient socket drop
  suspends open sessions into pending (bridgeable); `flush_all()` is only for a
  deliberate exit. See README "Reliability behaviors."
- **Graceful shutdown flushes to SQLite first, then one best-effort push.** A
  deliberate restart is not a presence blip, so shutdown finalizes sessions at the
  stop time and does *not* enter the reconnect bridge. SQLite is the safety net; a
  failed push never blocks shutdown. Does not cover `kill -9` / power loss —
  periodic checkpointing was considered and deliberately not added.
- **Bounded timeouts** (push 5s, Steam 8s, shutdown push 5s / 7s outer guard). No
  20s HTTP timeout is exercised anywhere in the running system.

## Steam & the merge

- A Steam-configured kid is **Steam-primary**: their total is the union of Steam
  gameplay and any Xbox gameplay that does *not* overlap Steam (Steam wins
  overlapping minutes). This stops double-counting the Xbox-app-on-PC "online"
  that shows while they're in a Steam game.
- For Steam, "online" is derived from being in a game (`gameextrainfo`), so the
  Steam session brackets Steam gameplay — no always-on Steam-client noise.

## Push to Kairos

- Pushes today's resolved totals + per-game breakdown + status + platforms every
  5 min. **Idempotent** — Kairos upserts the day and replaces the whole game
  breakdown, so re-sending "today so far" never accumulates and a corrected push
  clears stale games.
- In-progress (open + pending) sessions are included so a still-playing kid shows
  current time; they're never written to SQLite, so no double-count.
- `KAIROS_URL` must be the **internal** address — a LAN-to-LAN collector shouldn't
  hairpin out through Cloudflare. Auth is the `X-Ingest-Token` header (not
  Authelia), so going direct is fine.

## Storage

- **`DB_PATH` defaults next to the script**, not `./`. The stock `python` image
  runs with the working dir at `/`, so `./game_playtime.db` landed at the
  container root (ephemeral, wiped on recreate). Under Docker, point `DB_PATH` at
  the mounted `/data` volume.
- **Retention prune** on startup and once a day: delete rows older than
  `RETAIN_DAYS` and `VACUUM`. Keeps the file flat forever.

## Debugging discipline that worked

Pull the actual SQLite rows and `docker logs -t` (timestamped), reconstruct
against observed reality, and prefer evidence over theory. The diagnosis shifted
several times (coalesce gap → PC title drop → phantom device) as new data
arrived — let the data lead. A peer AI reviewer caught several real bugs across
iterations (a duplicate `_recompute`, an unbounded shutdown POST, a source-by-title
mislabel, a platform-metadata bug); the right response each time was to verify the
claim against the code and own it, not defend the prior version.
