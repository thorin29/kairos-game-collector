#!/usr/bin/env python3
"""
Kairos game-time collector — v4 (pushes to Kairos).

Sources feed one coalescing session engine:
  * Xbox (Home Assistant Xbox integration, WebSocket)
  * Steam (direct Steam Web API poll) — for Steam-primary kids

The daily total each kid is credited is their actual GAME-session time (in_game +
now_playing, with coalescing and an 8-minute Xbox reconnect bridge). Xbox-Network
"online" presence is recorded too but kept only as a diagnostic — a kid who is
merely online with no game running counts zero. It also reads each kid's profile
status (gamerscore, Game Pass, gamerpic from Xbox; Microsoft spending balance from
Family Safety sensors).

On a schedule it computes the RESOLVED per-person daily rollups (Steam-primary
merge applied) + status and POSTs them to Kairos's ingest endpoint. Kairos owns
the durable history; this container is just the collector.

Env:
  HA_URL, HA_TOKEN                      Home Assistant (Xbox)
  STEAM_API_KEY, STEAM_IDS             Steam (e.g. STEAM_IDS="Ethan=765...")
  KAIROS_URL                           e.g. https://home.ninjaknox.net
  GAMETIME_INGEST_TOKEN                shared secret (same value set on Kairos)
  DB_PATH (./game_playtime.db), TZ_NAME (America/Chicago), ONLY (comma names)

Usage: run live | --summary | --selftest
"""
from __future__ import annotations
import asyncio, json, os, re, signal, sqlite3, sys, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
try:
    import websockets
except Exception:
    websockets = None

UNSET = {"", "none", "unknown", "unavailable"}
COALESCE_GAP = timedelta(minutes=5)
# How long a game session stays reconnectable after the game signal drops. If the
# SAME game is confirmed again within this window, the two pieces are joined and
# the gap between them is counted as play (evidence-based: the kid was in the game
# before and after). If it does NOT come back, the session is finalized at the drop
# and NO extra time is added. Xbox presence for a PC/console can blink offline for
# several minutes mid-game, so the Xbox *game* window is wider than the online-
# diagnostic window; Steam reports the title directly and keeps the tighter default
# so a real Steam break isn't swallowed. Tune the Xbox game window via env.
XBOX_GAME_GAP = timedelta(minutes=int(os.environ.get("XBOX_GAME_GAP_MIN", "8")))
FLUSH_INTERVAL = 20
STEAM_POLL = 60
PUSH_INTERVAL = 300   # push rollups+status to Kairos every 5 min
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "game_playtime.db")


def parse_ts(s):
    if not s:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _mins(a, b):
    return round(max(0.0, (b - a).total_seconds() / 60.0), 2)


def _norm(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in UNSET else s


def _tri(state):
    s = str(state).strip().lower()
    return True if s == "on" else (False if s == "off" else None)


def _to_int(v):
    v = _norm(v)
    if v is None:
        return None
    try:
        return int(float(str(v).replace(",", "")))
    except ValueError:
        return None


def fmt_balance(v):
    """Normalize a Family Safety balance state to a USD display string."""
    v = _norm(v)
    if v is None:
        return None
    m = re.search(r"-?\d+(\.\d+)?", str(v).replace(",", ""))
    if not m:
        return str(v)
    return f"${float(m.group()):.2f}"


# ---------------------------------------------------------------- coalescer
class Coalescer:
    def __init__(self, on_emit, gap=COALESCE_GAP):
        self.on_emit = on_emit
        self.gap = gap
        # open:    friend -> (label, start, meta)
        # pending: friend -> (label, start, end, meta)
        # `meta` (e.g. the platform a game is on) is captured when the session opens
        # and travels with THAT session — it is never overwritten mid-session or by a
        # later different session, and a bridge keeps the original session's meta.
        self.open: dict[str, tuple] = {}
        self.pending: dict[str, tuple] = {}

    def start(self, friend, label, ts, meta=None):
        o = self.open.get(friend)
        if o and o[0] == label:
            return
        if o:
            self.stop(friend, ts)
        p = self.pending.get(friend)
        if p:
            if p[0] == label and (ts - p[2]) <= self.gap:
                self.open[friend] = (p[0], p[1], p[3]); del self.pending[friend]; return
            self._flush(friend)
        self.open[friend] = (label, ts, meta)

    def stop(self, friend, ts):
        o = self.open.pop(friend, None)
        if o:
            if friend in self.pending:
                self._flush(friend)
            self.pending[friend] = (o[0], o[1], ts, o[2])

    def flush_expired(self, now):
        for friend in list(self.pending):
            if (now - self.pending[friend][2]) > self.gap:
                self._flush(friend)

    def flush_all(self, now):
        for friend in list(self.open):
            self.stop(friend, now)
        for friend in list(self.pending):
            self._flush(friend)

    def _flush(self, friend):
        p = self.pending.pop(friend, None)
        if p:
            self.on_emit(friend, p[0], p[1], p[2], p[3])


# ---------------------------------------------------------------- engine
class Engine:
    def __init__(self, on_online, on_game, use_last_online=True, label=None, game_gap=COALESCE_GAP):
        self.on_online_cb = on_online
        self.on_game_cb = on_game
        self.label = label          # set (e.g. "xbox") to log state transitions
        self._diag_last: dict[str, str] = {}
        self.use_last_online = use_last_online
        self.last_online: dict[str, datetime] = {}
        self.np: dict[str, str | None] = {}
        self.plat: dict[str, str | None] = {}
        self.ig: dict[str, bool | None] = {}
        self.online: dict[str, bool] = {}   # authoritative ONLINE-sensor state
        self.oc = Coalescer(self._emit_online)                # online: default gap
        self.gc = Coalescer(self._emit_game, gap=game_gap)    # game: reconnect window

    def _emit_online(self, friend, label, start, end, meta=None):
        if self.use_last_online:
            lo = self.last_online.get(friend)
            if lo and start <= lo <= end:
                end = lo
        self.on_online_cb(friend, start, end, _mins(start, end))

    def _emit_game(self, friend, title, start, end, meta=None):
        # `meta` is the platform captured when THIS session opened, so it can't be
        # overwritten by a later different game/device.
        self.on_game_cb(friend, title, start, end, _mins(start, end), meta)

    def set_online(self, friend, is_on, ts):
        self.online[friend] = bool(is_on)
        if is_on:
            self.oc.start(friend, "online", ts); self._recompute(friend, ts)
        else:
            self.oc.stop(friend, ts); self.gc.stop(friend, ts)

    def set_in_game(self, friend, tri, ts):
        self.ig[friend] = tri; self._recompute(friend, ts)

    def set_now_playing(self, friend, value, ts, platform=None):
        self.np[friend] = _norm(value)
        if platform is not None:
            self.plat[friend] = platform
        self._recompute(friend, ts)

    def set_last_online(self, friend, dt):
        if dt:
            self.last_online[friend] = dt

    def _recompute(self, friend, ts):
        # The ONLINE sensor is the only thing that may count Xbox time. now_playing
        # / in_game only decide WHICH game while already online, so a stale
        # now_playing can never manufacture Xbox time when online says off.
        title = self.np.get(friend); ig = self.ig.get(friend)
        recording = self.online.get(friend) is True and title is not None and ig is not False
        if recording:
            # Capture the device on the session as it opens; the coalescer keeps it
            # with that session so a later game/device can't relabel it.
            self.gc.start(friend, title, ts, self.plat.get(friend))
        else:
            # Stop at the drop and hold it pending. If the SAME game returns within
            # the reconnect window the coalescer rejoins the pieces and counts the
            # gap; if it never returns, the session finalizes here with nothing added.
            self.gc.stop(friend, ts)
        self._diag(friend, title, ig, recording)

    def _diag(self, friend, title, ig, recording):
        """Log a deduped state line per friend so a dropped game is explainable."""
        if not self.label:
            return
        online = self.online.get(friend); plat = self.plat.get(friend)
        if recording:
            verdict = f"recording {title}"
        elif online is not True:
            verdict = "not recording (account offline)"
        elif title is None:
            verdict = "not recording (no now_playing title)"
        elif ig is False:
            verdict = "not recording (in_game off)"
        else:
            verdict = "not recording"
        o = "on" if online is True else ("off" if online is False else "?")
        g = "on" if ig is True else ("off" if ig is False else "?")
        line = (f"[{self.label}] {friend:<14} online={o} in_game={g} "
                f"now_playing={title or 'None'} platform={plat or 'None'} -> {verdict}")
        if self._diag_last.get(friend) != line:
            self._diag_last[friend] = line
            print("  " + line, flush=True)

    def flush(self, now):
        self.oc.flush_expired(now); self.gc.flush_expired(now)

    def flush_all(self, now):
        self.oc.flush_all(now); self.gc.flush_all(now)

    def suspend(self, now):
        # For a TRANSIENT loss of the source (e.g. the HA WebSocket dropping): stop
        # open sessions into PENDING at `now` but do NOT flush pending. If the same
        # game is confirmed again within the reconnect window the coalescer rejoins
        # the pieces; if the outage outlasts the window the normal flush loop expires
        # them. Unlike flush_all (a deliberate exit), this preserves the bridge.
        for friend in list(self.oc.open):
            self.oc.stop(friend, now)
        for friend in list(self.gc.open):
            self.gc.stop(friend, now)

    def live_online(self, now):
        """In-progress online sessions not yet written: open ones end at `now`,
        pending (recently closed, awaiting the coalesce flush) keep their end."""
        out = [(f, st, now) for f, (lbl, st, meta) in self.oc.open.items()]
        out += [(f, st, en) for f, (lbl, st, en, meta) in self.oc.pending.items()]
        return out

    def live_games(self, now):
        # Each row carries the platform captured on that session (the 4th tuple slot).
        out = [(f, title, st, now, meta) for f, (title, st, meta) in self.gc.open.items()]
        out += [(f, title, st, en, meta) for f, (title, st, en, meta) in self.gc.pending.items()]
        return out


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """CREATE TABLE IF NOT EXISTS online_sessions(
                 id INTEGER PRIMARY KEY AUTOINCREMENT, friend TEXT, source TEXT DEFAULT 'xbox',
                 start_utc TEXT, end_utc TEXT, minutes REAL);
               CREATE TABLE IF NOT EXISTS game_sessions(
                 id INTEGER PRIMARY KEY AUTOINCREMENT, friend TEXT, source TEXT DEFAULT 'xbox',
                 game TEXT, start_utc TEXT, end_utc TEXT, minutes REAL);"""
        )
        for t in ("online_sessions", "game_sessions"):
            try:
                self.db.execute(f"ALTER TABLE {t} ADD COLUMN source TEXT DEFAULT 'xbox'")
            except sqlite3.OperationalError:
                pass
        # Additive: which platform a game session ran on (e.g. "PC" vs a console),
        # captured live from the Xbox presence attribute. Existing rows stay NULL.
        try:
            self.db.execute("ALTER TABLE game_sessions ADD COLUMN platform TEXT")
        except sqlite3.OperationalError:
            pass
        self.db.commit()

    def add_online(self, friend, source, s, e, m):
        self.db.execute("INSERT INTO online_sessions(friend,source,start_utc,end_utc,minutes) VALUES(?,?,?,?,?)",
                        (friend, source, s.isoformat(), e.isoformat(), m)); self.db.commit()

    def add_game(self, friend, source, game, s, e, m, platform=None):
        self.db.execute("INSERT INTO game_sessions(friend,source,game,start_utc,end_utc,minutes,platform) VALUES(?,?,?,?,?,?,?)",
                        (friend, source, game, s.isoformat(), e.isoformat(), m, platform)); self.db.commit()

    def online_rows(self):
        return self.db.execute("SELECT friend,source,start_utc,end_utc,minutes FROM online_sessions").fetchall()

    def game_rows(self):
        return self.db.execute("SELECT friend,source,game,start_utc,end_utc,minutes,platform FROM game_sessions").fetchall()

    def prune(self, days: int) -> int:
        """Delete sessions older than `days` and reclaim space. The local DB is
        only a buffer (Kairos keeps the real history), so old rows aren't needed
        once pushed. Called on the event-loop thread, where the connection lives."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        n = 0
        for table in ("online_sessions", "game_sessions"):
            cur = self.db.execute(f"DELETE FROM {table} WHERE start_utc < ?", (cutoff,))
            n += max(0, cur.rowcount)
        self.db.commit()
        try:
            self.db.execute("VACUUM")   # actually shrink the file (must be outside a txn)
        except Exception:
            pass
        return n


# ---------------------------------------------------------------- resolver + rollups
def resolve_precedence(intervals):
    pts = sorted(set([i[0] for i in intervals] + [i[1] for i in intervals]))
    per_game, per_source, per_game_plat, total = {}, {}, {}, 0.0
    for a, b in zip(pts, pts[1:]):
        if b <= a:
            continue
        cov = [i for i in intervals if i[0] <= a and i[1] >= b]
        if not cov:
            continue
        win = max(cov, key=lambda i: i[3])
        mins = (b - a).total_seconds() / 60.0
        g = win[2]
        per_game[g] = per_game.get(g, 0.0) + mins
        src = win[4] if len(win) > 4 else None      # source of the WINNING interval
        if src is not None:
            per_source[src] = per_source.get(src, 0.0) + mins
        per_game_plat[g] = win[5] if len(win) > 5 else None   # platform of the winner
        total += mins
    return total, per_game, per_source, per_game_plat


def compute_daily(store, tz, steam_friends, extra_online=None, extra_games=None):
    """Resolved per-person totals + game breakdown for *today* (local).

    `extra_online`/`extra_games` carry in-progress sessions the collector holds in
    memory but hasn't written yet, so a kid who is still playing shows their
    current time instead of 0. Those rows are never in the DB, so no double count.
    """
    today = datetime.now(tz).date().isoformat()

    def today_iso(iso):
        return parse_ts(iso).astimezone(tz).date().isoformat() == today

    def today_dt(dt):
        return dt.astimezone(tz).date().isoformat() == today

    online = {}
    for friend, source, s, e, m in store.online_rows():
        if source == "xbox" and today_iso(s):
            online[friend] = online.get(friend, 0.0) + m
    for friend, source, s_dt, e_dt in (extra_online or []):
        if source == "xbox" and today_dt(s_dt):
            online[friend] = online.get(friend, 0.0) + _mins(s_dt, e_dt)
    games = {}
    for friend, source, game, s, e, m, plat in store.game_rows():
        if today_iso(s):
            games.setdefault(friend, []).append((source, game, plat, parse_ts(s), parse_ts(e)))
    for friend, source, game, s_dt, e_dt, plat in (extra_games or []):
        if today_dt(s_dt):
            games.setdefault(friend, []).append((source, game, plat, s_dt, e_dt))

    out = {}
    for friend in set(online) | set(games):
        rows = games.get(friend, [])
        if friend in steam_friends:
            intervals = [(s, e, g, 2 if src == "steam" else 1, src, plat) for (src, g, plat, s, e) in rows]
            tot, per, psrc, pplat = resolve_precedence(intervals)
        else:
            # Xbox time is actual gameplay, not account-online presence. Xbox
            # Network "online" can be a PC Xbox app, Game Pass, or lingering
            # presence with no game running, so a kid who is merely online must
            # log 0. Total = union of the kid's Xbox game sessions (the same
            # basis Steam-primary uses); online_sessions stay for diagnostics.
            intervals = [(s, e, g, 1, src, plat) for (src, g, plat, s, e) in rows if src == "xbox"]
            tot, per, psrc, pplat = resolve_precedence(intervals)
        # Sources and per-game platform both come from the WINNERS of the precedence
        # merge, not from every source/row that shared a title. So a Steam game that
        # also surfaces under Xbox presence at the same instant shows Steam only with
        # no Xbox device attached; a genuinely separate Xbox/Game Pass game shows Xbox.
        sources = sorted(sc for sc, mn in psrc.items() if round(mn) > 0)
        out[friend] = {
            "friend": friend, "date": today, "minutes": round(tot),
            "games": [{"game": g, "minutes": round(mn), "platform": pplat.get(g)} for g, mn in per.items() if round(mn) > 0],
            "sources": sources,
        }
    return out


# ---------------------------------------------------------------- client
class Client:
    def __init__(self, ha_url, ha_token, store, tz, only, steam_key, steam_ids, kairos_url, ingest_token):
        self.ws_url = (ha_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
                       + "/api/websocket") if ha_url else None
        self.ha_token, self.store, self.tz, self.only = ha_token, store, tz, only
        self.steam_key, self.steam_ids = steam_key, steam_ids
        self.kairos_url = kairos_url.rstrip("/") if kairos_url else None
        self.ingest_token = ingest_token
        self._id = 0
        self.on: dict[str, str] = {}; self.ig: dict[str, str] = {}
        self.np: dict[str, str] = {}; self.lo: dict[str, str] = {}
        self.gs: dict[str, str] = {}; self.gp: dict[str, str] = {}; self.bal: dict[str, str] = {}
        self.status: dict[str, dict] = {}
        self.xbox = Engine(self._mk_online("xbox"), self._mk_game("xbox"), use_last_online=True, label="xbox", game_gap=XBOX_GAME_GAP)
        self.steam = Engine(self._mk_online("steam"), self._mk_game("steam"), use_last_online=False)

    def _set_status(self, friend, key, val):
        if val is not None:
            self.status.setdefault(friend, {})[key] = val

    def _mk_online(self, source):
        def cb(friend, s, e, m):
            self.store.add_online(friend, source, s, e, m)
            tag = "on xbox " if source == "xbox" else "on steam"
            print(f"  \u25a0 {tag} {friend:<14} {s.astimezone(self.tz):%H:%M}\u2192{e.astimezone(self.tz):%H:%M}  {m:>5.1f} min", flush=True)
        return cb

    def _mk_game(self, source):
        def cb(friend, game, s, e, m, platform=None):
            self.store.add_game(friend, source, game, s, e, m, platform)
            src = "" if source == "xbox" else " (steam)"
            print(f"    \u00b7 game   {friend:<14} {game+src:<26} {s.astimezone(self.tz):%H:%M}\u2192{e.astimezone(self.tz):%H:%M}  {m:>5.1f} min", flush=True)
        return cb

    async def _cmd(self, ws, payload):
        self._id += 1; payload["id"] = self._id
        await ws.send(json.dumps(payload))
        while True:
            m = json.loads(await ws.recv())
            if m.get("id") == self._id and m.get("type") == "result":
                if not m.get("success"):
                    raise RuntimeError(m.get("error"))
                return m["result"]

    async def run(self):
        if websockets is None:
            sys.exit("Missing dependency: pip install websockets")
        self._stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass  # signal handlers aren't available on every platform
        bg = [asyncio.create_task(self._flush_loop()),
              asyncio.create_task(self._prune_loop())]
        if self.steam_key and self.steam_ids:
            bg.append(asyncio.create_task(self._steam_loop()))
            print(f"[+] Steam enabled for: {sorted(self.steam_ids)} (Steam-primary)", flush=True)
        if self.kairos_url and self.ingest_token:
            bg.append(asyncio.create_task(self._push_loop()))
            print(f"[+] Kairos push enabled -> {self.kairos_url}/api/v1/game-time/ingest", flush=True)
        try:
            await self._serve()               # runs until SIGTERM/SIGINT sets _stop
        finally:
            for t in bg:
                t.cancel()
            # Let the cancellations settle (their still-running HTTP calls are bounded
            # by the short per-request timeouts, not by this await).
            await asyncio.gather(*bg, return_exceptions=True)
            await self._graceful_shutdown()

    async def _serve(self):
        if not self.ws_url:
            await self._stop.wait()
            return
        backoff = 2
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.ws_url, max_size=8_000_000) as ws:
                    await self._auth(ws); await self._discover(ws); await self._snapshot(ws)
                    backoff = 2
                    listen = asyncio.ensure_future(self._listen(ws))
                    stopw = asyncio.ensure_future(self._stop.wait())
                    done, pending = await asyncio.wait({listen, stopw}, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                        try:
                            await t
                        except BaseException:
                            pass
                    if listen in done:
                        listen.result()       # re-raise a socket error to reconnect
            except Exception as e:
                if self._stop.is_set():
                    break
                # A dropped HA socket is transient: suspend open Xbox sessions into
                # pending (bridgeable) rather than committing them, so if HA
                # reconnects and the same game is still running within the reconnect
                # window it rejoins seamlessly. A longer outage lets the pending
                # expire normally, ending the session at the disconnect with no
                # invented time.
                self.xbox.suspend(datetime.now(timezone.utc))
                print(f"[!] HA disconnected: {e} — retry in {backoff}s", flush=True)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60)

    async def _graceful_shutdown(self):
        # On a normal Docker stop/restart/update (SIGTERM) or Ctrl-C (SIGINT):
        # 1) finalize every open/pending session INTO SQLite at the shutdown time.
        #    A deliberate restart is not a presence blip, so it does NOT enter the
        #    8-minute reconnect bridge — the session simply ends here and is saved.
        now = datetime.now(timezone.utc)
        try:
            self.xbox.flush_all(now); self.steam.flush_all(now)
        except Exception as e:
            print(f"[!] shutdown flush failed: {e}", flush=True)
        # 2) one best-effort push so Kairos reflects the just-finalized totals right
        #    away. SQLite is the safety net: a failed or slow push must never block
        #    shutdown or alter the persisted rows — the restarted collector will send
        #    them on its next cycle. Bounded under Docker's default stop grace.
        if self.kairos_url and self.ingest_token:
            try:
                # The HTTP op itself must use a short timeout — an outer wait_for
                # can stop *waiting* but can't kill the worker thread, and
                # asyncio.run() blocks on that thread at exit. 5s keeps us well
                # inside Docker's stop grace; the wait_for is a second guard.
                resp = await asyncio.wait_for(
                    asyncio.to_thread(self._post, self._build_payload(), 5), timeout=7)
                print(f"[+] final push on shutdown: matched={resp.get('matched')}", flush=True)
            except Exception as e:
                print(f"[!] final push skipped ({e}); sessions are safe in SQLite", flush=True)
        print("[+] graceful shutdown complete", flush=True)

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            now = datetime.now(timezone.utc)
            self.xbox.flush(now); self.steam.flush(now)

    async def _prune_loop(self):
        try:
            days = max(1, int(os.getenv("RETAIN_DAYS", "14")))
        except ValueError:
            days = 14
        while True:
            try:
                removed = self.store.prune(days)   # DB op on the event-loop thread
                if removed:
                    print(f"[+] pruned {removed} old session(s) (keeping {days}d)", flush=True)
            except Exception as e:
                print(f"[!] prune failed: {e}", flush=True)
            await asyncio.sleep(24 * 3600)

    async def _auth(self, ws):
        assert json.loads(await ws.recv())["type"] == "auth_required"
        await ws.send(json.dumps({"type": "auth", "access_token": self.ha_token}))
        if json.loads(await ws.recv())["type"] != "auth_ok":
            raise RuntimeError("auth failed — check HA_TOKEN")
        print(f"[+] connected to {self.ws_url}", flush=True)

    async def _discover(self, ws):
        ents = await self._cmd(ws, {"type": "config/entity_registry/list"})
        devs = await self._cmd(ws, {"type": "config/device_registry/list"})
        dev_name = {d["id"]: (d.get("name_by_user") or d.get("name") or d["id"]) for d in devs}
        for d in (self.on, self.ig, self.np, self.lo, self.gs, self.gp, self.bal):
            d.clear()
        for e in ents:
            eid = e["entity_id"]; tk = e.get("translation_key")
            if e.get("platform") == "xbox" and not e.get("disabled_by"):
                friend = dev_name.get(e.get("device_id"), eid.split(".")[-1])
                if self.only and friend not in self.only:
                    continue
                if tk == "online" and eid.startswith("binary_sensor."):
                    self.on[eid] = friend
                elif tk == "in_game" and eid.startswith("binary_sensor."):
                    self.ig[eid] = friend
                elif tk == "has_game_pass" and eid.startswith("binary_sensor."):
                    self.gp[eid] = friend
                elif tk == "now_playing" and eid.startswith("sensor."):
                    self.np[eid] = friend
                elif tk == "last_online" and eid.startswith("sensor."):
                    self.lo[eid] = friend
                elif tk == "gamer_score" and eid.startswith("sensor."):
                    self.gs[eid] = friend
        friends = sorted(set(self.on.values()) | set(self.np.values()))
        # Family Safety balance sensors (different integration) mapped by first name.
        for e in ents:
            eid = e["entity_id"]
            if eid.endswith("_balance") and "family_safety" in eid:
                for friend in friends:
                    if friend.split()[0].lower() in eid:
                        self.bal[eid] = friend
                        break
        print(f"[+] tracking {len(friends)} Xbox friend(s): {friends}", flush=True)
        if self.bal:
            print(f"[+] Family Safety balance for: {sorted(set(self.bal.values()))}", flush=True)

    async def _snapshot(self, ws):
        states = {s["entity_id"]: s for s in await self._cmd(ws, {"type": "get_states"})}
        now = datetime.now(timezone.utc)
        for eid, friend in self.lo.items():
            self.xbox.set_last_online(friend, parse_ts(states.get(eid, {}).get("state")))
        for eid, friend in self.ig.items():
            self.xbox.ig[friend] = _tri(states.get(eid, {}).get("state"))
        for eid, friend in self.np.items():
            st = states.get(eid, {})
            self.xbox.np[friend] = _norm(st.get("state"))
            self.xbox.plat[friend] = (st.get("attributes") or {}).get("platform")
        for eid, friend in self.on.items():
            self.xbox.set_online(
                friend, str(states.get(eid, {}).get("state", "")).lower() == "on", now,
            )
        for friend in set(self.np.values()):
            self.xbox._recompute(friend, now)
        # status
        for eid, friend in self.gs.items():
            self._set_status(friend, "gamerscore", _to_int(states.get(eid, {}).get("state")))
        for eid, friend in self.gp.items():
            self._set_status(friend, "hasGamePass", _tri(states.get(eid, {}).get("state")))
        for eid, friend in self.bal.items():
            self._set_status(friend, "msBalance", fmt_balance(states.get(eid, {}).get("state")))
        for eid, friend in list(self.on.items()) + list(self.np.items()):
            pic = states.get(eid, {}).get("attributes", {}).get("entity_picture")
            if pic and str(pic).startswith("http") and "gamerpic" not in self.status.get(friend, {}):
                self._set_status(friend, "gamerpic", pic)

    async def _listen(self, ws):
        await self._cmd(ws, {"type": "subscribe_events", "event_type": "state_changed"})
        print("[+] watching… (Ctrl-C to stop)\n", flush=True)
        async for raw in ws:
            m = json.loads(raw)
            if m.get("type") != "event":
                continue
            d = m["event"]["data"]; eid = d.get("entity_id")
            ts = parse_ts(m["event"].get("time_fired"))
            new = (d.get("new_state") or {}).get("state")
            if eid in self.on:
                self.xbox.set_online(self.on[eid], str(new).lower() == "on", ts)
            elif eid in self.ig:
                self.xbox.set_in_game(self.ig[eid], _tri(new), ts)
            elif eid in self.np:
                plat = ((d.get("new_state") or {}).get("attributes") or {}).get("platform")
                self.xbox.set_now_playing(self.np[eid], new, ts, plat)
            elif eid in self.lo:
                self.xbox.set_last_online(self.lo[eid], parse_ts(new))
            elif eid in self.gs:
                self._set_status(self.gs[eid], "gamerscore", _to_int(new))
            elif eid in self.gp:
                self._set_status(self.gp[eid], "hasGamePass", _tri(new))
            elif eid in self.bal:
                self._set_status(self.bal[eid], "msBalance", fmt_balance(new))

    # ---- Steam ----
    async def _steam_loop(self):
        ids = ",".join(self.steam_ids.values())
        by_id = {sid: friend for friend, sid in self.steam_ids.items()}
        url = ("https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?"
               + urllib.parse.urlencode({"key": self.steam_key, "steamids": ids}))
        while True:
            try:
                players = await asyncio.to_thread(self._get_json, url)
                players = players.get("response", {}).get("players", [])
                now = datetime.now(timezone.utc); seen = set()
                for p in players:
                    friend = by_id.get(str(p.get("steamid")))
                    if not friend:
                        continue
                    seen.add(friend)
                    title = p.get("gameextrainfo"); playing = bool(title)
                    self.steam.set_now_playing(friend, title, now)
                    self.steam.set_in_game(friend, playing, now)
                    self.steam.set_online(friend, playing, now)
                for friend in self.steam_ids:
                    if friend not in seen:
                        self.steam.set_online(friend, False, now)
            except Exception as e:
                print(f"[!] Steam poll failed: {e}", flush=True)
            await asyncio.sleep(STEAM_POLL)

    # ---- Kairos push ----
    async def _push_loop(self):
        while True:
            await asyncio.sleep(PUSH_INTERVAL)
            try:
                # Build the payload on the event-loop thread (that's where the
                # SQLite connection was created and where all writes happen); only
                # the blocking HTTP POST goes to a worker thread. Reading the DB
                # from a worker thread is what caused the "SQLite objects created
                # in a thread can only be used in that same thread" failures.
                payload = self._build_payload()
                resp = await asyncio.to_thread(self._post, payload, 5)
                print(f"[+] pushed to Kairos: matched={resp.get('matched')} unmatched={resp.get('unmatched')}", flush=True)
            except Exception as e:
                print(f"[!] Kairos push failed: {e}", flush=True)

    def _build_payload(self) -> dict:
        today = datetime.now(self.tz).date().isoformat()
        now = datetime.now(timezone.utc)
        extra_online = ([(f, "xbox", s, e) for (f, s, e) in self.xbox.live_online(now)]
                        + [(f, "steam", s, e) for (f, s, e) in self.steam.live_online(now)])
        extra_games = ([(f, "xbox", g, s, e, plat) for (f, g, s, e, plat) in self.xbox.live_games(now)]
                       + [(f, "steam", g, s, e, None) for (f, g, s, e, plat) in self.steam.live_games(now)])
        days = compute_daily(self.store, self.tz, set(self.steam_ids), extra_online, extra_games)
        for friend in self.status:                       # include status even with no play today
            days.setdefault(friend, {"friend": friend, "date": today, "minutes": 0, "games": []})
        # Platform icons = the sources that actually earned credited game time today
        # (from compute_daily), not "seen anywhere in the DB retention window." A kid
        # merely online with no game shows no icon; one who genuinely played both
        # Xbox and Steam today shows both.
        for friend in days:
            srcs = days[friend].pop("sources", [])
            if srcs:
                days[friend]["platforms"] = srcs
            if friend in self.status:
                days[friend]["status"] = self.status[friend]
        return {"days": list(days.values())}

    def _post(self, payload: dict, timeout: int = 20) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.kairos_url + "/api/v1/game-time/ingest", data=data, method="POST",
            headers={"Content-Type": "application/json", "X-Ingest-Token": self.ingest_token,
                     "User-Agent": "kairos-game-collector"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    @staticmethod
    def _get_json(url):
        req = urllib.request.Request(url, headers={"User-Agent": "kairos-game-collector"})
        with urllib.request.urlopen(req, timeout=8) as r:   # short: Steam re-polls every minute
            return json.loads(r.read().decode())


def _tzinfo(name):
    if ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def print_summary(store, tz, steam_friends):
    today = datetime.now(tz).date().isoformat()
    days = compute_daily(store, tz, steam_friends)
    if not days:
        print(f"No sessions recorded today ({today})."); return
    print(f"\n=== Game time for {today} ===")
    for friend in sorted(days):
        d = days[friend]; tot = d["minutes"]
        label = "gaming  [Steam-primary]" if friend in steam_friends else "on Xbox"
        print(f"\n{friend} — {tot/60:.1f}h {label} ({tot}m)")
        for g in sorted(d["games"], key=lambda x: -x["minutes"]):
            print(f"    {g['game']:<28} {g['minutes']/60:.1f}h  ({g['minutes']}m)")


def _parse_steam_ids(raw):
    out = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if "=" in part:
            name, sid = part.split("=", 1)
            out[name.strip()] = sid.strip()
    return out


def selftest():
    t = datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc)
    M = lambda n: t + timedelta(minutes=n)
    o, g = [], []
    eng = Engine(lambda f, s, e, m: o.append((f, round(m))),
                 lambda f, ga, s, e, m, plat=None: g.append((f, ga, round(m))))
    eng.set_online("Ethan", True, M(0)); eng.set_now_playing("Ethan", "Grounded", M(2))
    eng.set_in_game("Ethan", True, M(2)); eng.set_online("Ethan", False, M(20))
    eng.set_online("Ethan", True, M(21)); eng.set_last_online("Ethan", M(49))
    eng.set_online("Ethan", False, M(50)); eng.flush_all(M(200))
    assert ("Ethan", 49) in o and len([x for x in o if x[0] == "Ethan"]) == 1, o
    assert ("Ethan", "Grounded", 48) in g, g

    intervals = [(M(0), M(60), "Grounded", 2), (M(30), M(45), "Grounded", 1),
                 (M(70), M(80), "Grounded", 1), (M(90), M(100), "Halo", 1)]
    tot, per, _, _ = resolve_precedence(intervals)
    assert round(tot) == 80 and round(per["Grounded"]) == 70 and round(per["Halo"]) == 10, (tot, per)

    # source precedence: same title, same interval, Steam outranks Xbox -> Xbox earns
    # ZERO credited minutes, so it must NOT appear as a platform source.
    _t, _p, psrc, _ = resolve_precedence([(M(0), M(60), "Grounded", 2, "steam"),
                                       (M(0), M(60), "Grounded", 1, "xbox")])
    assert round(_t) == 60 and round(psrc.get("steam", 0)) == 60 and psrc.get("xbox", 0) == 0, psrc
    # genuinely separate Steam + Xbox play -> both sources credited.
    _t, _p, psrc, _ = resolve_precedence([(M(0), M(60), "DeepRock", 2, "steam"),
                                       (M(90), M(120), "Grounded", 1, "xbox")])
    assert sorted(sc for sc, mn in psrc.items() if round(mn) > 0) == ["steam", "xbox"], psrc
    # ...and the game's displayed platform follows the winner: Steam wins the shared
    # Grounded interval, so no lost Xbox/Windows device is attached to it.
    _t, _p, _ps, pplat = resolve_precedence([(M(0), M(60), "Grounded", 2, "steam", None),
                                             (M(0), M(60), "Grounded", 1, "xbox", "Windows")])
    assert pplat.get("Grounded") is None, pplat

    assert fmt_balance("12.50") == "$12.50"
    assert fmt_balance("$8") == "$8.00"
    assert fmt_balance("5.5 USD") == "$5.50"
    assert fmt_balance("unknown") is None
    # Rollup: an Xbox account merely ONLINE with no game logs 0 (the old code
    # counted online presence as game time); a real game still counts.
    now = datetime.now(timezone.utc)
    st = Store(":memory:")
    st.add_online("Caleb", "xbox", now, now + timedelta(minutes=274), 274.0)
    st.add_game("Aaron", "xbox", "ARK", now, now + timedelta(minutes=53), 53.0)
    st.add_online("Aaron", "xbox", now, now + timedelta(minutes=160), 160.0)
    days = compute_daily(st, timezone.utc, set())
    assert days["Caleb"]["minutes"] == 0, days["Caleb"]
    assert days["Aaron"]["minutes"] == 53, days["Aaron"]

    # --- regression: Ethan's real 2026-09-18 Grounded session ---------------
    # Confirmed Grounded 17:19:55, then Xbox presence blinked fully offline twice
    # mid-game (6m27s and 5m16s — both over the 5-min online window), then the game
    # signal ended at 18:06:06 for good while the account lingered online (party
    # chat) until 18:42. The two mid-game outages must be BRIDGED (game returned) and
    # counted; the post-18:06 online tail must add NOTHING. Expected ~46.2 min.
    eg = []
    eng2 = Engine(lambda *_: None,
                  lambda f, ga, s, e, m, plat=None: eg.append(round(m, 2)),
                  use_last_online=False, game_gap=XBOX_GAME_GAP)
    D = lambda h, mi, s=0: datetime(2026, 9, 18, h, mi, s, tzinfo=timezone.utc)
    def gon(h, mi, s=0):   # online + Grounded + in_game all on
        eng2.set_online("E", True, D(h, mi, s))
        eng2.set_now_playing("E", "Grounded", D(h, mi, s), "Windows")
        eng2.set_in_game("E", True, D(h, mi, s))
    def goff(h, mi, s=0):  # account drops offline (title/in_game clear too)
        eng2.set_now_playing("E", None, D(h, mi, s), "Windows")
        eng2.set_in_game("E", False, D(h, mi, s))
        eng2.set_online("E", False, D(h, mi, s))
    gon(17, 19, 55)
    goff(17, 39, 33)                 # gap 1 (6m27s) — bridge on return
    gon(17, 46, 0)
    goff(17, 52, 7)                  # gap 2 (5m16s) — bridge on return
    gon(17, 57, 23)
    eng2.set_in_game("E", False, D(18, 6, 6))       # game really ends here…
    eng2.set_now_playing("E", None, D(18, 6, 6), "Windows")
    eng2.set_online("E", False, D(18, 42, 0))       # …account lingered, then off
    eng2.flush_all(D(20, 0, 0))
    total = round(sum(eg), 1)
    assert len(eg) == 1 and 45.5 <= total <= 47.0, (eg, total)  # one joined session

    # --- regression: the 8-minute game reconnect window itself --------------
    # Bridge iff the SAME game returns within the window; never invent time.
    def sim(seq):
        got = []
        e = Engine(lambda *_: None,
                   lambda f, ga, s, en, mn, plat=None: got.append((ga, round(mn))),
                   use_last_online=False, game_gap=timedelta(minutes=8))
        T = lambda mins: datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=mins)
        e.set_online("K", True, T(0))
        for mins, game in seq:
            e.set_now_playing("K", game, T(mins), "Xbox One")
            e.set_in_game("K", game is not None, T(mins))
        e.flush_all(T(seq[-1][0] + 60))
        return got
    assert sim([(0, "Grounded"), (10, None), (17, "Grounded"), (25, None)]) == [("Grounded", 25)], "7-min gap must bridge"
    assert sim([(0, "Grounded"), (10, None), (19, "Grounded"), (25, None)]) == [("Grounded", 10), ("Grounded", 6)], "9-min gap must NOT bridge"
    assert sim([(0, "Grounded"), (10, None)]) == [("Grounded", 10)], "no return -> add nothing"
    assert sim([(0, "Grounded"), (10, None), (17, "Halo"), (25, None)]) == [("Grounded", 10), ("Halo", 8)], "different game -> gap not counted"

    # --- regression: transient HA disconnect preserves the bridge ------------
    # A dropped WebSocket calls suspend() (stop open -> pending, don't flush), so a
    # reconnect that re-lands the same game within the window rejoins across it.
    def sim_ha(gap_min, game2="Grounded"):
        got = []
        e = Engine(lambda *_: None,
                   lambda f, ga, s, en, mn, plat=None: got.append((ga, round(mn))),
                   use_last_online=False, game_gap=timedelta(minutes=8))
        T = lambda mins: datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=mins)
        e.set_online("K", True, T(0)); e.set_now_playing("K", "Grounded", T(0), "Xbox One"); e.set_in_game("K", True, T(0))
        e.suspend(T(20))                                            # HA socket drops
        r = 20 + gap_min                                           # reconnect snapshot order: np/ig, then online
        e.set_now_playing("K", game2, T(r), "Xbox One"); e.set_in_game("K", True, T(r)); e.set_online("K", True, T(r))
        e.set_now_playing("K", None, T(r + 10), "Xbox One"); e.set_in_game("K", False, T(r + 10))
        e.flush_all(T(r + 80))
        return got
    assert sim_ha(2) == [("Grounded", 32)], "HA reconnect 2 min -> one continuous session"
    assert sim_ha(9) == [("Grounded", 20), ("Grounded", 10)], "HA reconnect 9 min -> gap excluded"
    assert sim_ha(2, "Halo") == [("Grounded", 20), ("Halo", 10)], "HA reconnect to different game -> no bridge"

    # --- regression: platform metadata belongs to the SESSION -----------------
    # Grounded (Xbox One) stops -> pending; Halo (Windows) starts 2 min later, before
    # Grounded flushes. Grounded must stay Xbox One; Halo must be Windows — the later
    # game/device must not relabel the pending session.
    got2 = []
    e2 = Engine(lambda *_: None,
                lambda f, ga, s, en, mn, plat=None: got2.append((ga, round(mn), plat)),
                use_last_online=False, game_gap=timedelta(minutes=8))
    T2 = lambda mins: datetime(2026, 2, 1, tzinfo=timezone.utc) + timedelta(minutes=mins)
    e2.set_online("K", True, T2(0)); e2.set_now_playing("K", "Grounded", T2(0), "Xbox One"); e2.set_in_game("K", True, T2(0))
    e2.set_now_playing("K", None, T2(10), "Xbox One"); e2.set_in_game("K", False, T2(10))    # Grounded -> pending
    e2.set_now_playing("K", "Halo", T2(12), "Windows"); e2.set_in_game("K", True, T2(12))     # Halo starts before flush
    e2.set_now_playing("K", None, T2(20), "Windows"); e2.set_in_game("K", False, T2(20))
    e2.flush_all(T2(90))
    assert ("Grounded", 10, "Xbox One") in got2 and ("Halo", 8, "Windows") in got2, got2

    print("selftest OK \u2713")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    tz = _tzinfo(os.getenv("TZ_NAME", "America/Chicago"))
    store = Store(os.getenv("DB_PATH") or DEFAULT_DB)
    steam_ids = _parse_steam_ids(os.getenv("STEAM_IDS", ""))
    if "--summary" in sys.argv:
        return print_summary(store, tz, set(steam_ids))
    ha_url, ha_token = os.getenv("HA_URL"), os.getenv("HA_TOKEN")
    steam_key = os.getenv("STEAM_API_KEY")
    kairos_url, ingest_token = os.getenv("KAIROS_URL"), os.getenv("GAMETIME_INGEST_TOKEN")
    if not (ha_url and ha_token) and not (steam_key and steam_ids):
        sys.exit("Set HA_URL+HA_TOKEN (Xbox) and/or STEAM_API_KEY+STEAM_IDS (Steam).")
    only = set(x.strip() for x in os.getenv("ONLY", "").split(",") if x.strip()) or None
    try:
        asyncio.run(Client(ha_url, ha_token, store, tz, only, steam_key, steam_ids, kairos_url, ingest_token).run())
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
