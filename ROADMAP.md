# Collector roadmap

Status as of **13 Nov 2026** (collector 5.0.0): the collector is **deployed and
running**. Xbox time credited to the kids comes from real game sessions rather
than account presence, and the `ally` source credits allowlisted PC apps
through Family Safety (see DECISIONS). Item 1 below (deploy and observe) is closed — the numbers were compared
against known play sessions and the engine held up.

The timing/coalescing logic stays **conservative**: change it only with evidence
from a real observed session, not on theory.

## 1. Deploy the frozen collector and observe — **done**

Deployed against real play; Xbox sessions resolve to sensible per-kid daily
totals. The remaining inaccuracies are source-signal gaps (below), not timing
logic. Capturing a `device_tracker` / `media_player` state trace during a real
session is still worth doing before anything in item 3 is attempted.

## 2. Open gaps in what can be seen at all

- **PC play — now covered where the PC is a managed Family Safety device.** Such
  a PC exposes per-app daily counters, which the `ally` source credits (see
  DECISIONS). What remains uncovered is a child playing on a **shared or
  unmanaged** computer: Xbox reports presence with no title and Family Safety has
  no device for them, so that time is still unmeasured — not zero. One observed
  case remains unexplained (a console-only child online all day with no credited
  game); check that child's `now_playing` timeline before assuming a cause.
- **Minecraft Java — now partially tracked (closed).** The Family Safety
  `applications` sensor reports the Java launcher and its runtime by name, so the
  `ally` source credits it (both processes collapse to one title via `GAME_APPS`).
  This is app-open time at ~5-minute resolution, not a confirmed session, so the
  RCON/server-log option remains the only route to exact play times — but the gap
  is no longer total and nothing needs to be built on spec.
- **`--backfill` for past days.** A mode to recompute and re-push earlier days
  whose totals were produced by the old presence-based logic. Offered, not built;
  wanted only if a bogus historical day actually bothers someone.

## 3. Physical-console corroboration (future, evidence-gated)

HA's Xbox integration can expose each physical console as a `media_player` (power
state + focused app resolved to a title, ~10s refresh; requires "Remote features"
enabled on the console). There are also per-MAC `device_tracker` entities.

- **Value:** `media_title` = *what's running*; `device_tracker` off = a candidate
  *hard stop*; on ≠ playing.
- **Hard problems:** a physical-console signal can't attribute to a specific child
  on a shared box (account presence does that); two consoles swapping /
  simultaneous play makes kid↔console mapping non-trivial. HA can delete/recreate
  the console `media_player` entity (a known HA bug), so **discover entities
  dynamically — never hard-code IDs**. Treat `device_tracker: not_home` as a
  strong corroborating stop, not a hard stop, until its real timing on this
  network is observed (the switch can take minutes to notice a disconnect).
- **Gate:** only worth building if a specific miscount traces back to a missing
  stop signal.

## 4. Midnight-splitting

A session that crosses midnight is credited entirely to the start day. A real
bug, but it only matters if play regularly runs past 12 AM. Still open.

## Hygiene notes (not behavior changes)

- `Client._post` still declares a default `timeout: int = 20`, but every call site
  (`_push_loop`, `_graceful_shutdown`) passes `5` explicitly, so the 20s default
  is never exercised. Harmless today. **Decided (13 Nov 2026):** change the
  default to `5` in the next release that touches the collector for any other
  reason, so a future caller can't reintroduce a 20s stall. Not worth a release
  on its own.

## Docs

`README.md` describes the detection model and the environment; `DECISIONS.md`
holds the reasoning and the rejected alternatives. Update both in the same change
that alters behaviour — the web repo's `ROADMAP.md`/`DECISIONS.md` carry the
Kairos-side view of game time.
