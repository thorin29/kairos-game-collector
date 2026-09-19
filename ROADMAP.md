# Collector roadmap

Open items, in priority order. The timing/coalescing logic is **frozen** until
item 1 produces real observation data — don't change it on theory.

## 1. Deploy the frozen collector and observe (do this first)

The collector compiles and `--selftest` passes, but it has **not** been deployed
and watched against real play yet. Run a few known play sessions and compare the
collector's numbers to what the kids actually did. Capture a `device_tracker` /
`media_player` state trace during a real session for later use. This must happen
before any further collector code change.

## 2. Physical-console corroboration (future, evidence-gated)

HA's Xbox integration can expose each physical console as a `media_player` (power
state + focused app resolved to a title, ~10s refresh; requires "Remote features"
enabled on the console). There are also Omada `device_tracker.xbox` /
`device_tracker.xbox_2` entities (per-MAC on/off).

- **Value:** `media_title` = *what's running*; `device_tracker` off = a candidate
  *hard stop*; on ≠ playing.
- **Hard problems:** a physical-console signal can't attribute to a specific child
  on a shared box (account presence does that); two consoles swapping /
  simultaneous play makes kid↔console mapping non-trivial. HA can delete/recreate
  the console `media_player` entity (a known HA bug), so **discover entities
  dynamically — never hard-code IDs**. Downgrade `device_tracker: not_home` from
  "hard stop" to "strong corroborating stop" until its real timing on this network
  is observed (Omada can take minutes to notice a disconnect).
- **Gate:** don't build until #1 gives real data.

## 3. Midnight-splitting

A session that crosses midnight is currently credited entirely to the start day.
Real bug, but only matters if kids regularly play across 12 AM. Left for later.

## Hygiene notes (not behavior changes)

- `Client._post` still declares a default `timeout: int = 20`, but every call site
  (`_push_loop`, `_graceful_shutdown`) passes `5` explicitly, so the 20s default
  is never exercised. Harmless today; worth dropping the stale default to `5` next
  time the file is touched, so a future caller can't reintroduce a 20s stall.
