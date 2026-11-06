"""
ClaimScheduler: the automatic daily run.

Once a day, after the configured local start time, it runs a session for the chosen account
group. A game whose reward reopens per account (Anime Dice: 23 h after that account's last
claim) does not make every account eligible at one moment, so with "follow cooldown" on it
also starts a short session whenever accounts it has seen claim become eligible again. Each
account is tried automatically at most once per eligibility, and sessions never overlap.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone, tzinfo

from features.daily_claim.models import ClaimAccount, Outcome
from features.daily_claim.store import ClaimStore

TICK_SECONDS = 30.0
# Least time between two cooldown sessions, so accounts that open close together share one.
COOLDOWN_GAP_SECONDS = 15 * 60

_OFFSET = re.compile(r"^UTC([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def resolve_tz(tz_id: str) -> tzinfo:
    """"Local", "UTC", "UTC+08:00" or an IANA name (only when the system has tz data)."""
    tz_id = (tz_id or "Local").strip()
    if tz_id.casefold() == "local":
        return datetime.now().astimezone().tzinfo or UTC
    if tz_id.upper() == "UTC":
        return UTC
    match = _OFFSET.match(tz_id)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        hours, minutes = int(match.group(2)), int(match.group(3) or 0)
        if hours > 14 or minutes > 59:
            raise ValueError(f"Bad UTC offset: {tz_id}")
        return timezone(sign * timedelta(hours=hours, minutes=minutes), tz_id.upper())
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(tz_id)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown time zone: {tz_id}") from exc


def timezone_choices() -> list[str]:
    choices = ["Local", "UTC"]
    for minutes in range(-12 * 60, 14 * 60 + 1, 30):
        if minutes == 0:
            continue
        sign = "+" if minutes > 0 else "-"
        h, m = divmod(abs(minutes), 60)
        if m and abs(minutes) not in (210, 270, 330, 390, 570, 630):
            continue
        choices.append(f"UTC{sign}{h:02d}:{m:02d}")
    return choices


def parse_start_time(text: str) -> tuple[int, int]:
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not match:
        raise ValueError(f"Start time must be HH:MM, got {text!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"Start time out of range: {text!r}")
    return hour, minute


StartFn = Callable[[str, str, list[ClaimAccount]], bool]


class ClaimScheduler:
    def __init__(self, store: ClaimStore, game_id: str, start_fn: StartFn,
                 accounts_fn: Callable[[str], list[ClaimAccount]], is_busy: Callable[[], bool],
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] = print):
        self.store = store
        self.game_id = game_id
        self._start = start_fn
        self._accounts = accounts_fn
        self._busy = is_busy
        self._clock = clock
        self._log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="DailyClaim-schedule")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception as exc:
                self._log(f"[DailyClaim] Scheduler: {type(exc).__name__}: {exc}")

    def next_run_text(self) -> str:
        schedule = self.store.get_schedule()
        if not schedule.get("Enabled"):
            return "Schedule off"
        try:
            tz = resolve_tz(schedule.get("TimeZoneId", "Local"))
            hour, minute = parse_start_time(schedule.get("LocalStartTime", "09:00"))
        except ValueError as exc:
            return str(exc)
        now = datetime.fromtimestamp(self._clock(), tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if schedule.get("LastExecutionDate") == now.date().isoformat() or now >= target:
            if schedule.get("LastExecutionDate") == now.date().isoformat():
                target += timedelta(days=1)
        return f"Next daily run {target.strftime('%a %H:%M')} ({schedule.get('TimeZoneId', 'Local')})"

    def tick(self) -> str:
        """One check. Returns what it did, for logs and tests."""
        schedule = self.store.get_schedule()
        if not schedule.get("Enabled"):
            return "off"
        tz = resolve_tz(schedule.get("TimeZoneId", "Local"))
        hour, minute = parse_start_time(schedule.get("LocalStartTime", "09:00"))
        now_ts = self._clock()
        now = datetime.fromtimestamp(now_ts, tz)
        if (now.hour, now.minute) < (hour, minute):
            return "before start time"
        if self._busy():
            return "busy"
        group = schedule.get("AccountGroup", "") or ""
        today = now.date().isoformat()

        if schedule.get("LastExecutionDate") != today:
            accounts = self._accounts(group)
            self.store.save_schedule(LastExecutionDate=today, LastSessionAt=now_ts)
            if not accounts:
                return "no accounts"
            self._log(f"[DailyClaim] Scheduled daily run: {len(accounts)} accounts")
            self._start("schedule", group, accounts)
            return "daily"

        if schedule.get("FollowCooldown") and now_ts - float(schedule.get("LastSessionAt") or 0) >= COOLDOWN_GAP_SECONDS:
            states = self.store.account_states(self.game_id)
            ready = []
            for account in self._accounts(group):
                state = states.get(account.account_id)
                if not state or not state["NextEligibleAt"]:
                    continue
                opens = float(state["NextEligibleAt"])
                if (opens <= now_ts
                        and state["LastOutcome"] in (Outcome.SUCCESS.value, Outcome.ALREADY_CLAIMED.value)
                        and float(state["UpdatedAt"]) < opens):
                    ready.append(account)
            if ready:
                self.store.save_schedule(LastSessionAt=now_ts)
                self._log(f"[DailyClaim] {len(ready)} account(s) can claim again; starting a session")
                self._start("cooldown", group, ready)
                return "cooldown"
        return "idle"
