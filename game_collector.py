#!/usr/bin/env python3
"""
Kairos game-time collector — v4 (pushes to Kairos).

Sources feed one coalescing session engine:
  * Xbox (Home Assistant Xbox integration, WebSocket)
  * Steam (direct Steam Web API poll) — for Steam-primary kids

It records TIME ON XBOX (online) + GAME BREAKDOWN with coalescing,
in_game+now_playing game sessions, and a last_online end cross-check; and it
reads each kid's profile status (gamerscore, Game Pass, gamerpic from Xbox;
Microsoft spending balance from Family Safety sensors).

On a schedule it computes the RESOLVED per-person daily rollups (Steam-primary
merge applied) + status and POSTs them to Kairos's ingest endpoint. Kairos owns
the durable history; this container is just the collector.

Env:
  HA_URL, HA_TOKEN                      Home Assistant (Xbox)
  STEAM_API_KEY, STEAM_IDS             Steam (e.g. STEAM_IDS="P1=765...")
  KAIROS_URL                           e.g. https://kairos.example.com
  GAMETIME_INGEST_TOKEN                shared secret (same value set on Kairos)
  DB_PATH (./game_playtime.db), TZ_NAME (America/Chicago), ONLY (comma names)

Usage: run live | --summary | --selftest
"""
from __future__ import annotations
import asyncio, json, os, re, sqlite3, sys, urllib.parse, urllib.request
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
    def __init__(self, on_emit):
        self.on_emit = on_emit
        self.open: dict[str, tuple[str, datetime]] = {}
        self.pending: dict[str, tuple[str, datetime, datetime]] = {}

    def start(self, friend, label, ts):
        o = self.open.get(friend)
        if o and o[0] == label:
            return
        if o:
            self.stop(friend, ts)
        p = self.pending.get(friend)
        if p:
            if p[0] == label and (ts - p[2]) <= COALESCE_GAP:
                self.open[friend] = (p[0], p[1]); del self.pending[friend]; return
            self._flush(friend)
        self.open[friend] = (label, ts)

    def stop(self, friend, ts):
        o = self.open.pop(friend, None)
        if o:
            if friend in self.pending:
                self._flush(friend)
            self.pending[friend] = (o[0], o[1], ts)

    def flush_expired(self, now):
        for friend in list(self.pending):
            if (now - self.pending[friend][2]) > COALESCE_GAP:
                self._flush(friend)

    def flush_all(self, now):
        for friend in list(self.open):
            self.stop(friend, now)
        for friend in list(self.pending):
            self._flush(friend)

    def _flush(self, friend):
        p = self.pending.pop(friend, None)
        if p:
            self.on_emit(friend, p[0], p[1], p[2])


# ---------------------------------------------------------------- engine
# Home Assistant now_playing "platform" values that are NOT an Xbox console — a
# game showing one of these is being played on PC/mobile via the same Xbox
# account, not the console we track.
_NON_XBOX_PLATFORMS = {"windows", "android", "ios", "nintendo switch", "web"}


def _is_xbox_console(platform) -> bool:
    """True unless the platform is a KNOWN non-Xbox device. Unknown/None is
    allowed (benefit of the doubt) so a missing attribute never zeroes real
    console play; only confirmed PC/mobile platforms are excluded."""
    if not platform:
        return True
    p = str(platform).strip().lower()
    if p.startswith("xbox"):
        return True
    return p not in _NON_XBOX_PLATFORMS


class Engine:
    def __init__(self, on_online, on_game, use_last_online=True, label=None):
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
        self.oc = Coalescer(self._emit_online)
        self.gc = Coalescer(self._emit_game)

    def _emit_online(self, friend, label, start, end):
        if self.use_last_online:
            lo = self.last_online.get(friend)
            if lo and start <= lo <= end:
                end = lo
        self.on_online_cb(friend, start, end, _mins(start, end))

    def _emit_game(self, friend, title, start, end):
        self.on_game_cb(friend, title, start, end, _mins(start, end))

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
        console = _is_xbox_console(self.plat.get(friend))
        recording = self.online.get(friend) is True and title is not None and ig is not False and console
        if recording:
            self.gc.start(friend, title, ts)
        else:
            self.gc.stop(friend, ts)
        self._diag(friend, title, ig, console, recording)

    def _diag(self, friend, title, ig, console, recording):
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
        elif not console:
            verdict = f"not recording (platform {plat} is not an Xbox console)"
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

    def live_online(self, now):
        """In-progress online sessions not yet written: open ones end at `now`,
        pending (recently closed, awaiting the coalesce flush) keep their end."""
        out = [(f, st, now) for f, (lbl, st) in self.oc.open.items()]
        out += [(f, st, en) for f, (lbl, st, en) in self.oc.pending.items()]
        return out

    def live_games(self, now):
        out = [(f, title, st, now) for f, (title, st) in self.gc.open.items()]
        out += [(f, title, st, en) for f, (title, st, en) in self.gc.pending.items()]
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
        self.db.commit()

    def add_online(self, friend, source, s, e, m):
        self.db.execute("INSERT INTO online_sessions(friend,source,start_utc,end_utc,minutes) VALUES(?,?,?,?,?)",
                        (friend, source, s.isoformat(), e.isoformat(), m)); self.db.commit()

    def add_game(self, friend, source, game, s, e, m):
        self.db.execute("INSERT INTO game_sessions(friend,source,game,start_utc,end_utc,minutes) VALUES(?,?,?,?,?,?)",
                        (friend, source, game, s.isoformat(), e.isoformat(), m)); self.db.commit()

    def online_rows(self):
        return self.db.execute("SELECT friend,source,start_utc,end_utc,minutes FROM online_sessions").fetchall()

    def game_rows(self):
        return self.db.execute("SELECT friend,source,game,start_utc,end_utc,minutes FROM game_sessions").fetchall()

    def platforms_by_friend(self) -> dict:
        out: dict[str, set] = {}
        for tbl in ("online_sessions", "game_sessions"):
            for friend, source in self.db.execute(f"SELECT DISTINCT friend, source FROM {tbl}").fetchall():
                out.setdefault(friend, set()).add(source)
        return out

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
    per_game, total = {}, 0.0
    for a, b in zip(pts, pts[1:]):
        if b <= a:
            continue
        cov = [i for i in intervals if i[0] <= a and i[1] >= b]
        if not cov:
            continue
        win = max(cov, key=lambda i: i[3])
        mins = (b - a).total_seconds() / 60.0
        per_game[win[2]] = per_game.get(win[2], 0.0) + mins
        total += mins
    return total, per_game


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
    for friend, source, game, s, e, m in store.game_rows():
        if today_iso(s):
            games.setdefault(friend, []).append((source, game, parse_ts(s), parse_ts(e)))
    for friend, source, game, s_dt, e_dt in (extra_games or []):
        if today_dt(s_dt):
            games.setdefault(friend, []).append((source, game, s_dt, e_dt))

    out = {}
    for friend in set(online) | set(games):
        rows = games.get(friend, [])
        if friend in steam_friends:
            intervals = [(s, e, g, 2 if src == "steam" else 1) for (src, g, s, e) in rows]
            tot, per = resolve_precedence(intervals)
        else:
            # Xbox time is actual gameplay, not account-online presence. Xbox
            # Network "online" can be a PC Xbox app, Game Pass, or lingering
            # presence with no game running, so a kid who is merely online must
            # log 0. Total = union of the kid's Xbox game sessions (the same
            # basis Steam-primary uses); online_sessions stay for diagnostics.
            intervals = [(s, e, g, 1) for (src, g, s, e) in rows if src == "xbox"]
            tot, per = resolve_precedence(intervals)
        out[friend] = {
            "friend": friend, "date": today, "minutes": round(tot),
            "games": [{"game": g, "minutes": round(mn)} for g, mn in per.items() if round(mn) > 0],
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
        self.xbox = Engine(self._mk_online("xbox"), self._mk_game("xbox"), use_last_online=True, label="xbox")
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
        def cb(friend, game, s, e, m):
            self.store.add_game(friend, source, game, s, e, m)
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
        asyncio.create_task(self._flush_loop())
        asyncio.create_task(self._prune_loop())
        if self.steam_key and self.steam_ids:
            asyncio.create_task(self._steam_loop())
            print(f"[+] Steam enabled for: {sorted(self.steam_ids)} (Steam-primary)", flush=True)
        if self.kairos_url and self.ingest_token:
            asyncio.create_task(self._push_loop())
            print(f"[+] Kairos push enabled -> {self.kairos_url}/api/v1/game-time/ingest", flush=True)
        if not self.ws_url:
            while True:
                await asyncio.sleep(3600)
        backoff = 2
        while True:
            try:
                async with websockets.connect(self.ws_url, max_size=8_000_000) as ws:
                    await self._auth(ws); await self._discover(ws); await self._snapshot(ws)
                    backoff = 2
                    await self._listen(ws)
            except Exception as e:
                self.xbox.flush_all(datetime.now(timezone.utc))
                print(f"[!] HA disconnected: {e} — retry in {backoff}s", flush=True)
                await asyncio.sleep(backoff); backoff = min(backoff * 2, 60)

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
                resp = await asyncio.to_thread(self._post, payload)
                print(f"[+] pushed to Kairos: matched={resp.get('matched')} unmatched={resp.get('unmatched')}", flush=True)
            except Exception as e:
                print(f"[!] Kairos push failed: {e}", flush=True)

    def _build_payload(self) -> dict:
        today = datetime.now(self.tz).date().isoformat()
        now = datetime.now(timezone.utc)
        extra_online = ([(f, "xbox", s, e) for (f, s, e) in self.xbox.live_online(now)]
                        + [(f, "steam", s, e) for (f, s, e) in self.steam.live_online(now)])
        extra_games = ([(f, "xbox", g, s, e) for (f, g, s, e) in self.xbox.live_games(now)]
                       + [(f, "steam", g, s, e) for (f, g, s, e) in self.steam.live_games(now)])
        days = compute_daily(self.store, self.tz, set(self.steam_ids), extra_online, extra_games)
        for friend in self.status:                       # include status even with no play today
            days.setdefault(friend, {"friend": friend, "date": today, "minutes": 0, "games": []})
        # Which systems each kid actually uses (for the per-system icon). Start from
        # recorded + live sessions; the Steam-primary kid shows Steam only, since
        # their overlapping Xbox time is suppressed in the merge anyway.
        plats = self.store.platforms_by_friend()
        for f, _s, _e in self.xbox.live_online(now):
            plats.setdefault(f, set()).add("xbox")
        for f, _s, _e in self.steam.live_online(now):
            plats.setdefault(f, set()).add("steam")
        steam_friends = set(self.steam_ids)
        for friend in days:
            srcs = set(plats.get(friend, set()))
            if friend in steam_friends:
                srcs.discard("xbox")
                srcs.add("steam")
            if srcs:
                days[friend]["platforms"] = sorted(srcs)
            if friend in self.status:
                days[friend]["status"] = self.status[friend]
        return {"days": list(days.values())}

    def _post(self, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.kairos_url + "/api/v1/game-time/ingest", data=data, method="POST",
            headers={"Content-Type": "application/json", "X-Ingest-Token": self.ingest_token,
                     "User-Agent": "kairos-game-collector"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    @staticmethod
    def _get_json(url):
        req = urllib.request.Request(url, headers={"User-Agent": "kairos-game-collector"})
        with urllib.request.urlopen(req, timeout=20) as r:
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
                 lambda f, ga, s, e, m: g.append((f, ga, round(m))))
    eng.set_online("P1", True, M(0)); eng.set_now_playing("P1", "Grounded", M(2))
    eng.set_in_game("P1", True, M(2)); eng.set_online("P1", False, M(20))
    eng.set_online("P1", True, M(21)); eng.set_last_online("P1", M(49))
    eng.set_online("P1", False, M(50)); eng.flush_all(M(200))
    assert ("P1", 49) in o and len([x for x in o if x[0] == "P1"]) == 1, o
    assert ("P1", "Grounded", 48) in g, g

    intervals = [(M(0), M(60), "Grounded", 2), (M(30), M(45), "Grounded", 1),
                 (M(70), M(80), "Grounded", 1), (M(90), M(100), "Halo", 1)]
    tot, per = resolve_precedence(intervals)
    assert round(tot) == 80 and round(per["Grounded"]) == 70 and round(per["Halo"]) == 10, (tot, per)

    assert fmt_balance("12.50") == "$12.50"
    assert fmt_balance("$8") == "$8.00"
    assert fmt_balance("5.5 USD") == "$5.50"
    assert fmt_balance("unknown") is None
    # Rollup: an Xbox account merely ONLINE with no game logs 0 (the old code
    # counted online presence as game time); a real game still counts.
    now = datetime.now(timezone.utc)
    st = Store(":memory:")
    st.add_online("P2", "xbox", now, now + timedelta(minutes=274), 274.0)
    st.add_game("P3", "xbox", "ARK", now, now + timedelta(minutes=53), 53.0)
    st.add_online("P3", "xbox", now, now + timedelta(minutes=160), 160.0)
    days = compute_daily(st, timezone.utc, set())
    assert days["P2"]["minutes"] == 0, days["P2"]
    assert days["P3"]["minutes"] == 53, days["P3"]

    # Platform gate: Windows/Android are not console time; Xbox / unknown are.
    assert _is_xbox_console("Xbox Series X|S") and _is_xbox_console(None)
    assert not _is_xbox_console("Windows") and not _is_xbox_console("Android")

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
