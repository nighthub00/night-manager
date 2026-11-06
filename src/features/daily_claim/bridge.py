"""
Executor bridge for the Anime Dice daily claim.

A small Lua script, run by the user's executor when Anime Dice loads (its auto-execute folder),
connects to this local WebSocket server, reports the account's daily-reward data and fires the
claim only when this app asks for it. The script never claims on its own.

Messages are JSON objects:
  script -> app  {"t":"hello","token":..,"userId":..,...state}      on connect
                 {"t":"state",...state}                              every few seconds
                 {"re":<id>,...}                                     reply to a request
  app -> script  {"id":<id>,"op":"state"} | {"id":<id>,"op":"claim"}

state = {userId, placeId, jobId, loaded, last, day, now, cooldown}: `last` is the save's
LastDailyRewardClaim, `now` the server clock (workspace:GetServerTimeNow), `cooldown` the
game's DailyRewardConfig.Cooldown.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import secrets
import threading
import time

import websockets

DEFAULT_PORT = 38917
ANIME_DICE_PLACE_ID = 113290951185459
SCRIPT_NAME = "nightmanager_dailyclaim.lua"


class BridgeError(Exception):
    pass


class _Peer:
    def __init__(self, websocket, user_id: str):
        self.websocket = websocket
        self.user_id = user_id
        self.state: dict = {}
        self.seen = time.time()
        self.replies: dict[int, dict] = {}


class ExecutorBridge:
    def __init__(self, port: int = DEFAULT_PORT, token: str = "", host: str = "127.0.0.1"):
        self.host = host
        self.port = int(port)
        self.token = token or secrets.token_hex(16)
        self._cond = threading.Condition()
        self._peers: dict[str, _Peer] = {}
        self._ids = itertools.count(1)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self.error = ""

    # lifecycle

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self.error)

    def start(self, timeout: float = 5.0) -> bool:
        if self.running:
            return True
        self.error = ""
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="DailyClaim-bridge")
        self._thread.start()
        self._ready.wait(timeout)
        return self.running

    def stop(self) -> None:
        loop, stop = self._loop, self._stop
        if loop and stop:
            loop.call_soon_threadsafe(stop.set)
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            print(f"[DailyClaim] Executor bridge stopped: {self.error}")
        finally:
            self._ready.set()
            loop.close()

    async def _serve(self) -> None:
        self._stop = asyncio.Event()
        # origins=[None]: only clients that send no Origin (executors), never a browser page.
        async with websockets.serve(self._handler, self.host, self.port, origins=[None],
                                    max_size=64 * 1024) as server:
            if self.port == 0:
                self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop.wait()

    async def _handler(self, websocket) -> None:
        peer: _Peer | None = None
        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if peer is None:
                    if msg.get("t") != "hello" or not secrets.compare_digest(
                            str(msg.get("token", "")), self.token):
                        await websocket.close(code=4001, reason="bad token")
                        return
                    peer = _Peer(websocket, str(msg.get("userId", "")))
                    with self._cond:
                        old = self._peers.get(peer.user_id)
                        self._peers[peer.user_id] = peer
                        self._cond.notify_all()
                    if old is not None:
                        asyncio.ensure_future(old.websocket.close())
                self._take(peer, msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            if peer is not None:
                with self._cond:
                    if self._peers.get(peer.user_id) is peer:
                        del self._peers[peer.user_id]
                    self._cond.notify_all()

    def _take(self, peer: _Peer, msg: dict) -> None:
        with self._cond:
            peer.seen = time.time()
            state = msg.get("state") if isinstance(msg.get("state"), dict) else msg
            if "last" in state or "loaded" in state:
                peer.state = {k: state.get(k) for k in
                              ("userId", "placeId", "jobId", "loaded", "last", "day", "now", "cooldown")}
            if "re" in msg:
                try:
                    peer.replies[int(msg["re"])] = msg
                except (TypeError, ValueError):
                    pass
            self._cond.notify_all()

    # queries (from worker threads)

    def connected(self) -> list[str]:
        with self._cond:
            return list(self._peers)

    def state_of(self, user_id: str) -> dict | None:
        with self._cond:
            peer = self._peers.get(str(user_id))
            return dict(peer.state) if peer else None

    def wait_for(self, user_id: str, timeout: float, cancel: threading.Event | None = None,
                 loaded: bool = True) -> dict:
        """Waits for the account's script to connect (and its game data to load)."""
        deadline = time.time() + timeout
        with self._cond:
            while True:
                peer = self._peers.get(str(user_id))
                if peer and (not loaded or peer.state.get("loaded")):
                    return dict(peer.state)
                remaining = deadline - time.time()
                if remaining <= 0 or (cancel is not None and cancel.is_set()):
                    break
                self._cond.wait(min(0.5, remaining))
        if cancel is not None and cancel.is_set():
            raise BridgeError("cancelled")
        if peer:
            raise BridgeError("the executor script connected but Anime Dice's data never loaded")
        raise BridgeError("the executor script never connected (is it in the executor's "
                          "auto-execute folder, and is the executor attached?)")

    def request(self, user_id: str, op: str, timeout: float) -> dict:
        with self._cond:
            peer = self._peers.get(str(user_id))
        if peer is None or self._loop is None:
            raise BridgeError("the executor script is not connected")
        request_id = next(self._ids)
        payload = json.dumps({"id": request_id, "op": op})
        future = asyncio.run_coroutine_threadsafe(peer.websocket.send(payload), self._loop)
        try:
            future.result(timeout=5)
        except Exception as exc:
            raise BridgeError(f"could not reach the executor script: {exc}") from exc
        deadline = time.time() + timeout
        with self._cond:
            while request_id not in peer.replies:
                remaining = deadline - time.time()
                if remaining <= 0 or self._peers.get(str(user_id)) is not peer:
                    raise BridgeError(f"no answer to '{op}' from the executor script")
                self._cond.wait(min(0.5, remaining))
            return peer.replies.pop(request_id)


def reward_status(state: dict) -> tuple[bool | None, float | None]:
    """(claimable, seconds until it opens) from a reported state; (None, None) if unreadable."""
    try:
        last = float(state.get("last"))
        now = float(state.get("now"))
        cooldown = float(state.get("cooldown") or 82800)
    except (TypeError, ValueError):
        return None, None
    if last <= 0:
        return True, 0.0
    remaining = cooldown - (now - last)
    return (remaining <= 0), max(0.0, remaining)


# The script

LUA_TEMPLATE = r"""-- NIGHT MANAGER: Daily Claim bridge for Anime Dice. Generated by NIGHT MANAGER;
-- reinstall it from Daily Claim > Settings after changing the port. It only reports this
-- account's daily-reward data and fires DailyRewardService Claim when NIGHT MANAGER asks.
if game.PlaceId ~= __PLACE__ then return end
local G = (getgenv and getgenv()) or _G
if G.__NM_DAILY_BRIDGE then return end
G.__NM_DAILY_BRIDGE = true

local URL, TOKEN = "ws://127.0.0.1:__PORT__/", "__TOKEN__"
if not game:IsLoaded() then game.Loaded:Wait() end
local Players = game:GetService("Players")
local RS = game:GetService("ReplicatedStorage")
local Http = game:GetService("HttpService")
local lp = Players.LocalPlayer
while not lp do task.wait(0.2) lp = Players.LocalPlayer end

local connect = (WebSocket and (WebSocket.connect or WebSocket.Connect))
    or (syn and syn.websocket and syn.websocket.connect)
if not connect then warn("[NIGHT MANAGER] this executor has no WebSocket support") return end

local function mod(path)
    local o = RS:FindFirstChild("Framework")
    for seg in string.gmatch(path, "[^%.]+") do o = o and o:FindFirstChild(seg) end
    if not o then return nil end
    local ok, m = pcall(require, o)
    return ok and m or nil
end
local function read(key)
    local dc = mod("Features.Data.DataController")
    if not dc then return nil end
    local ok, v = pcall(function() return dc[key]() end)
    if ok then return v end
    return nil
end
local function claimRemote()
    local n = RS:FindFirstChild("Network")
    local s = n and n:FindFirstChild("DailyRewardService")
    local re = s and s:FindFirstChild("RE")
    return re and re:FindFirstChild("Claim")
end
local function state()
    local cfg = mod("Features.Rewards.DailyRewardConfig")
    local last = tonumber(read("LastDailyRewardClaim"))
    return {
        userId = tostring(lp.UserId), placeId = tostring(game.PlaceId), jobId = game.JobId,
        loaded = last ~= nil and claimRemote() ~= nil,
        last = last, day = tonumber(read("DailyRewardsClaimed")),
        now = workspace:GetServerTimeNow(),
        cooldown = (cfg and tonumber(cfg.Cooldown)) or 82800,
    }
end

local ws
local function send(t) if ws then pcall(function() ws:Send(Http:JSONEncode(t)) end) end end

local function onMessage(raw)
    local ok, msg = pcall(function() return Http:JSONDecode(raw) end)
    if not ok or type(msg) ~= "table" then return end
    if msg.op == "state" then
        local s = state() s.re = msg.id send(s)
    elseif msg.op == "claim" then
        task.spawn(function()
            local before = tonumber(read("LastDailyRewardClaim"))
            local r = claimRemote()
            local fired = r ~= nil and pcall(function() r:FireServer() end)
            local changed = false
            for _ = 1, 40 do
                task.wait(0.25)
                local now = tonumber(read("LastDailyRewardClaim"))
                if now and now ~= before then changed = true break end
            end
            local s = state() s.re = msg.id s.fired = fired s.changed = changed s.before = before
            send(s)
        end)
    end
end

for _ = 1, 60 do
    local ok, sock = pcall(connect, URL)
    if ok and sock then
        ws = sock
        local closed = false
        sock.OnMessage:Connect(onMessage)
        if sock.OnClose then sock.OnClose:Connect(function() closed = true end) end
        local hello = state() hello.t = "hello" hello.token = TOKEN
        send(hello)
        while not closed do
            task.wait(5)
            local s = state() s.t = "state"
            if not pcall(function() sock:Send(Http:JSONEncode(s)) end) then closed = true end
        end
        ws = nil
    end
    task.wait(5)
end
"""


def build_script(port: int, token: str) -> str:
    return (LUA_TEMPLATE.replace("__PLACE__", str(ANIME_DICE_PLACE_ID))
            .replace("__PORT__", str(int(port))).replace("__TOKEN__", token))


def install_script(folder: str, port: int, token: str) -> str:
    folder = os.path.expandvars(os.path.expanduser(folder or "")).strip()
    if not folder or not os.path.isdir(folder):
        raise BridgeError(f"Auto-execute folder not found: {folder or '(not set)'}")
    path = os.path.join(folder, SCRIPT_NAME)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(build_script(port, token))
    return path


def guess_autoexec_folders() -> list[str]:
    """Common executor auto-execute folders that exist on this PC."""
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local, "Potassium", "autoexec"),
        os.path.join(local, "Potassium", "workspace", "autoexec"),
        r"C:\RONIX\autoexec",
        os.path.join(local, "Wave", "autoexec"),
        os.path.join(local, "Solara", "autoexec"),
    ]
    return [path for path in candidates if os.path.isdir(path)]
