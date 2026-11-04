"""
features/discord_alerts.py
What happens to the clients, posted to the user's own Discord webhook (opt-in, after RoALT's
discord.rs, MIT): crashes, disconnects, restarts, finished batches and, if wanted, a client
getting into a game.

Events are coalesced: at most one message every 2 seconds, carrying everything queued since,
and a 429 is waited out as Discord says (`retry_after`). Messages name accounts only, never a
cookie, ticket, launch link or log line, and go only to a discord.com webhook URL.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable

import requests

import features.settings_store as settings_store

_WEBHOOK = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_\-]+$"
)
_FLUSH_SECONDS = 2.0
_FOOTER = "NIGHT MANAGER"
# event kind -> (setting key, default on, title, colour)
KINDS = {
    "crashed": ("alert_crash", True, "Client crashed", 0xE74C3C),
    "hung": ("alert_crash", True, "Client stopped responding", 0xE74C3C),
    "disconnected": ("alert_disconnect", True, "Client disconnected", 0xF39C12),
    "restarted": ("alert_restart", True, "Client restarted", 0x3498DB),
    "batch": ("alert_batch", True, "Batch finished", 0x2ECC71),
    "joined": ("alert_joined", False, "Joined a game", 0x2ECC71),
    "breaker": ("alert_crash", True, "Auto-rejoin gave up", 0xE74C3C),
}
_url_provider: Callable[[], str] = lambda: ""


def is_webhook_url(url: str) -> bool:
    return bool(_WEBHOOK.match((url or "").strip()))


def set_url_provider(provider: Callable[[], str]) -> None:
    """Where the webhook URL comes from (it holds the webhook's token, so it is kept with
    the encrypted account data, not in the plain settings file)."""
    global _url_provider
    _url_provider = provider


class _Poster:
    def __init__(self):
        self._lock = threading.Lock()
        self._queue: list[dict] = []
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def push(self, embed: dict) -> None:
        with self._lock:
            self._queue.append(embed)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True, name="DiscordAlerts")
                self._thread.start()
        self._wake.set()

    def _run(self) -> None:
        while True:
            self._wake.wait(60)
            self._wake.clear()
            time.sleep(_FLUSH_SECONDS)
            with self._lock:
                batch, self._queue = self._queue[:10], self._queue[10:]
                more = bool(self._queue)
            if not batch:
                continue
            self._send(batch)
            if more:
                self._wake.set()

    def _send(self, embeds: list[dict]) -> None:
        url = (_url_provider() or "").strip()
        if not is_webhook_url(url):
            return
        config = settings_store.get("discord_webhook", {}) or {}
        ping = str(config.get("ping_user_id", "") or "").strip() if config.get("enable_ping") else ""
        urgent = any(embed.get("color") == 0xE74C3C for embed in embeds)
        payload = {
            "content": f"<@{ping}>" if ping and ping.isdigit() and urgent else "",
            "embeds": embeds,
            "allowed_mentions": {"users": [ping]} if ping and ping.isdigit() and urgent else {"parse": []},
        }
        for _ in range(3):
            try:
                response = requests.post(url, json=payload, timeout=10)
            except requests.RequestException:
                return
            if response.status_code != 429:
                return
            try:
                wait = float(response.json().get("retry_after", 2.0))
            except (ValueError, AttributeError):
                wait = 2.0
            time.sleep(min(60.0, max(0.5, wait)))


_POSTER = _Poster()


def notify(kind: str, account: str, text: str = "") -> None:
    """Queue an alert, if the user wants this kind and hasn't muted the account."""
    if kind not in KINDS:
        return
    settings = settings_store.load()
    config = settings.get("discord_webhook", {}) or {}
    if not config.get("enabled") or not settings.get("discord_alerts_enabled", False):
        return
    key, default, title, colour = KINDS[kind]
    if not settings.get(key, default):
        return
    if account and account in (settings.get("discord_alert_muted") or []):
        return
    _POSTER.push({
        "title": title,
        "description": f"**{account}**\n{text}".strip() if account else text,
        "color": colour,
        "footer": {"text": _FOOTER},
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    })


def send_test() -> None:
    _POSTER.push({
        "title": "Test alert",
        "description": "NIGHT MANAGER can post client alerts here.",
        "color": 0x3498DB,
        "footer": {"text": _FOOTER},
    })
