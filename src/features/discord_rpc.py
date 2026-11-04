"""
features/discord_rpc.py
Discord rich presence: "Using NIGHT MANAGER", with the game and how many clients run.

Spoken to the Discord app on this PC over its local pipe \\\\.\\pipe\\discord-ipc-N (after
RoALT's rpc.rs, MIT): each frame is an opcode and a length (u32 LE each), then JSON. Nothing goes
over the network from here, and nothing happens while Discord isn't running. It needs the ID
of a Discord application (discord.com/developers), set in Settings.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
import uuid

_HANDSHAKE = 0
_FRAME = 1
_CLOSE = 2


class DiscordPresence:
    def __init__(self):
        self._lock = threading.Lock()
        self._pipe = None
        self._client_id = ""
        self._activity: dict | None = None
        self._sent: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = int(time.time())

    def start(self, client_id: str) -> None:
        client_id = "".join(ch for ch in str(client_id or "") if ch.isdigit())
        if not client_id:
            return
        with self._lock:
            if client_id != self._client_id:
                self._close()
                self._client_id = client_id
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="DiscordPresence")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._pipe is not None:
                try:
                    self._send(_FRAME, self._command(None))
                except OSError:
                    pass
            self._close()

    def update(self, details: str, state: str) -> None:
        self._activity = {
            "details": details[:128],
            "state": state[:128],
            "timestamps": {"start": self._started_at},
            "buttons": [{"label": "Get NIGHT MANAGER", "url": "https://web.nightshub.app/#manager"}],
        }

    def _run(self) -> None:
        while not self._stop.wait(5.0):
            with self._lock:
                try:
                    if self._pipe is None:
                        self._connect()
                    if self._pipe is not None and self._activity != self._sent:
                        self._send(_FRAME, self._command(self._activity))
                        self._sent = dict(self._activity) if self._activity else None
                except OSError:
                    self._close()

    def _connect(self) -> None:
        for index in range(10):
            try:
                pipe = open(rf"\\.\pipe\discord-ipc-{index}", "r+b", buffering=0)
            except OSError:
                continue
            self._pipe = pipe
            try:
                self._send(_HANDSHAKE, {"v": 1, "client_id": self._client_id})
                self._sent = None
                return
            except OSError:
                self._close()
        # Discord isn't running: try again next time round.

    def _command(self, activity: dict | None) -> dict:
        return {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(uuid.uuid4()),
        }

    def _send(self, opcode: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._pipe.write(struct.pack("<II", opcode, len(body)) + body)
        # Discord answers every frame; reading it keeps the pipe from filling up.
        header = self._pipe.read(8)
        if len(header) < 8:
            raise OSError("Discord closed the pipe")
        reply_op, length = struct.unpack("<II", header)
        self._pipe.read(length)
        if reply_op == _CLOSE:
            raise OSError("Discord refused the presence (check the application ID)")

    def _close(self) -> None:
        if self._pipe is not None:
            try:
                self._pipe.close()
            except OSError:
                pass
        self._pipe = None
        self._sent = None


PRESENCE = DiscordPresence()
