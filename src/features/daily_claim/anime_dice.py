"""
Anime Dice's two claim methods, picked in Daily Claim > Settings.

On-screen clicks: the client is shrunk to a 320 x 240 tile; the Offline Money popup is collected,
the Roblox "Join Event" panel is closed, and the Daily Rewards calendar (which Anime Dice opens by
itself only while a reward is ready) is read and the next day's tile clicked. Success is the tile's
check, "Next Reward In" or the "Claimed Daily Reward!" toast. Templates: anime_dice_screen.json,
cut from live captures on 2026-11-06 and 2026-11-13.

When the calendar does not open by itself, it is opened from the game's top-bar Daily button.
At tile size Roblox tucks that button into its overflow menu, so the window is grown to
`open_size` for a moment (other clients' clicks wait), the button clicked and the window shrunk
back; the calendar keeps its layout. That turns "already claimed" from a guess into a reading
of "Next Reward In".

Executor script: the bridge's auto-execute script reports LastDailyRewardClaim and fires
DailyRewardService Claim on request; success is that value changing.
"""

from __future__ import annotations

import contextlib
import threading
import time

from features.daily_claim import bridge as bridge_mod
from features.daily_claim import screen as screen_mod
from features.daily_claim.adapters import (
    ANIME_DICE_PROFILE,
    AdapterError,
    AnimeDiceAdapter,
    ClaimContext,
)
from features.daily_claim.models import Observation, RewardState

# After the Join Event panel shows up with no calendar before it, how long to wait for a
# calendar hiding behind it before opening it from the top-bar button.
EVENT_GRACE_SECONDS = 8.0
# How long the game gets to open the calendar by itself (when it does, it is ~30 s in; on
# 2026-11-13 it mostly did not) before it is opened from the top-bar button.
OPEN_AFTER_SECONDS = 30.0
# Tries at opening the calendar from the top-bar button, and the gap between them.
OPEN_TRIES = 2
OPEN_GAP_SECONDS = 8.0


class AnimeDiceScreenAdapter(AnimeDiceAdapter):
    method = "screen"

    def __init__(self, backend, profile: screen_mod.ScreenProfile | None = None,
                 popup_timeout: float = 150.0, poll: float = 0.5):
        self.backend = backend
        self.ui = profile or screen_mod.ScreenProfile.load()
        self.popup_timeout = popup_timeout
        self.poll = poll
        self._lock = threading.Lock()
        self._slots: dict[str, int] = {}
        self._state: dict[str, dict] = {}

    def profile(self) -> dict:
        return {**ANIME_DICE_PROFILE, "claim_method": "screen",
                "tile": list(self.ui.window), "screen_observed": self.ui.observed}

    def can_claim(self) -> tuple[bool, str]:
        missing = [k for k in ("calendar", "ready", "next", "toast", "check", "offline", "event_close")
                   if k not in self.ui.variants]
        if missing or not self.ui.tiles:
            return False, f"The on-screen profile is incomplete (missing {', '.join(missing) or 'tiles'})."
        return True, ""

    # helpers

    def _slot(self, key: str) -> int:
        with self._lock:
            if key not in self._slots:
                used = set(self._slots.values())
                self._slots[key] = next(i for i in range(10_000) if i not in used)
            return self._slots[key]

    def release(self, ctx: ClaimContext) -> None:
        with self._lock:
            self._slots.pop(ctx.account_id, None)
            self._state.pop(ctx.account_id, None)

    def _st(self, ctx: ClaimContext) -> dict:
        with self._lock:
            return self._state.setdefault(ctx.account_id, {})

    def _wait(self, ctx: ClaimContext, seconds: float) -> None:
        if ctx.cancel.wait(seconds):
            raise AdapterError("cancelled", retryable=False)

    def _hwnd(self, ctx: ClaimContext, timeout: float = 30.0) -> int:
        deadline = time.time() + timeout
        while time.time() < deadline:
            hwnd = self.backend.hwnd_for(ctx.client)
            if hwnd:
                return hwnd
            self._wait(ctx, 0.5)
        raise AdapterError("the client's window never appeared")

    def _frame(self, ctx: ClaimContext):
        st = self._st(ctx)
        return self.backend.capture(st["hwnd"])

    def _click(self, ctx: ClaimContext, x: int, y: int) -> bool:
        return self.backend.click(self._st(ctx)["hwnd"], x, y)

    def _label(self, frame) -> str | None:
        """"ready" or "next": whichever calendar label matches better."""
        return self.ui.which(frame, ("ready", "next"))

    def _open_calendar(self, ctx: ClaimContext) -> bool:
        """Grow the window, click the game's top-bar Daily button, shrink it back. True when
        the button was found and clicked (the caller checks for the calendar)."""
        if "daily_button" not in self.ui.variants:
            return False
        st = self._st(ctx)
        hwnd, slot = st["hwnd"], self._slot(ctx.account_id)
        width, height = self.ui.window
        big_w, big_h = self.ui.open_size
        st["open_tries"] = st.get("open_tries", 0) + 1
        st["last_open"] = time.time()
        exclusive = getattr(self.backend, "exclusive", None)
        clicked = False
        with (exclusive() if exclusive else contextlib.nullcontext()):
            self.backend.size(hwnd, slot, big_w, big_h)
            try:
                hit = None
                for _ in range(8):
                    self._wait(ctx, 0.5)
                    frame = self._frame(ctx)
                    if frame is None:
                        continue
                    if frame.width < big_w * 0.8:
                        break          # the window did not grow (or this is not a real window)
                    hit = self.ui.seen(frame, "daily_button", radius=40, full=False)
                    if hit:
                        break
                if hit:
                    # The label right of the glyph flips between "Daily" and "Claim".
                    clicked = bool(self._click(ctx, hit[1] + 12, hit[2]))
                    self._wait(ctx, 1.0)
            finally:
                self.backend.size(hwnd, slot, width, height)
        if clicked:
            st["opened"] = True
            self._wait(ctx, 1.5)       # the calendar re-lays out at tile size
        return clicked

    # steps

    def wait_until_ready(self, ctx: ClaimContext, timeout: float) -> Observation:
        st = self._st(ctx)
        st.clear()
        st["hwnd"] = self._hwnd(ctx)
        width, height = self.ui.window
        self.backend.size(st["hwnd"], self._slot(ctx.account_id), width, height)
        st.update(calendar=False, event=False, offline_clicks=0, last_offline=0.0, event_at=0.0,
                  open_tries=0, last_open=0.0, opened=False)
        started = time.time()
        deadline = started + (min(timeout, self.popup_timeout) if timeout else self.popup_timeout)
        frames = 0
        while time.time() < deadline:
            frames += 1
            if frames % 10 == 0:
                # Roblox can grow back after a fullscreen toggle or a crash dialog.
                self.backend.size(st["hwnd"], self._slot(ctx.account_id), width, height)
            frame = self._frame(ctx)
            if frame is None:
                self._wait(ctx, self.poll)
                continue
            if self.ui.seen(frame, "calendar"):
                st["calendar"] = True
                how = " from the top-bar button" if st["opened"] else ""
                return Observation(RewardState.UNKNOWN, 1.0,
                                   f"Daily Rewards calendar opened{how}" + self._extras(st), "ui")
            event = self.ui.seen(frame, "event_close", radius=6, full=False)
            if event:
                if not st["event"]:
                    st["event"], st["event_at"] = True, time.time()
                # Close Roblox's Join Event panel: the calendar may be underneath it.
                self._click(ctx, event[1], event[2])
            elif self.ui.seen(frame, "offline") and self.ui.seen(frame, "offline_title"):
                if time.time() - st["last_offline"] > 4 and st["offline_clicks"] < 4:
                    hit = self.ui.seen(frame, "offline")
                    self._click(ctx, hit[1], hit[2])       # collect the offline cash
                    st["offline_clicks"] += 1
                    st["last_offline"] = time.time()
            now = time.time()
            due = (now - started > OPEN_AFTER_SECONDS
                   or (st["event"] and now - st["event_at"] > EVENT_GRACE_SECONDS))
            if due and st["open_tries"] < OPEN_TRIES and now - st["last_open"] > OPEN_GAP_SECONDS:
                self._open_calendar(ctx)
                continue
            if (st["event"] and now - st["event_at"] > EVENT_GRACE_SECONDS
                    and st["open_tries"] >= OPEN_TRIES and now - st["last_open"] > 4):
                return Observation(RewardState.UNKNOWN, 0.8,
                                   "Join Event panel came with no calendar before it" + self._extras(st), "ui")
            self._wait(ctx, self.poll)
        return Observation(RewardState.UNKNOWN, 0.0, "no popup recognised before the timeout" + self._extras(st), "ui")

    @staticmethod
    def _extras(st: dict) -> str:
        bits = []
        if st.get("offline_clicks"):
            bits.append(f"collected Offline Money ({st['offline_clicks']} click(s))")
        if st.get("event"):
            bits.append("closed Roblox's Join Event panel")
        if st.get("open_tries") and not st.get("opened"):
            bits.append(f"top-bar Daily button not found ({st['open_tries']} tries)")
        return ("; " + ", ".join(bits)) if bits else ""

    def detect_reward(self, ctx: ClaimContext) -> Observation:
        st = self._st(ctx)
        frame = self._frame(ctx)
        if ((frame is None or not self.ui.seen(frame, "calendar")) and st.get("hwnd")
                and st.get("open_tries", 0) < OPEN_TRIES + 1 and self._open_calendar(ctx)):
            frame = self._frame(ctx)
        if frame is not None and self.ui.seen(frame, "calendar"):
            st["calendar"] = True
            days = self.ui.claimed_days(frame)
            next_day = next((i + 1 for i, done in enumerate(days) if not done), 1)
            ticks = "".join("x" if d else "." for d in days)
            label = self._label(frame)
            if label == "ready":
                return Observation(RewardState.AVAILABLE, 0.9,
                                   f"calendar shows 'Reward Ready!' (days {ticks})", "ui", reward_day=next_day)
            if label == "next":
                return Observation(RewardState.CLAIMED, 0.95,
                                   f"calendar shows 'Next Reward In' (days {ticks})", "ui")
            return Observation(RewardState.UNKNOWN, 0.3, f"calendar open, state label not recognised (days {ticks})", "ui")
        if st.get("event") and not st.get("calendar"):
            return Observation(
                RewardState.CLAIMED, 0.7,
                "the calendar did not open by itself before the Join Event panel (Anime Dice opens it "
                "only while a reward is ready)", "ui")
        return Observation(RewardState.UNKNOWN, 0.0, "the Daily Rewards calendar was not seen", "ui",
                           retry_useful=st.get("calendar") is not None)

    def claim(self, ctx: ClaimContext) -> Observation:
        st = self._st(ctx)
        frame = self._frame(ctx)
        if frame is None or not self.ui.seen(frame, "calendar"):
            raise AdapterError("the calendar closed before the claim click")
        days = self.ui.claimed_days(frame)
        index = next((i for i, done in enumerate(days) if not done), 0)
        x, y = self.ui.tiles[index]
        st["clicked_day"] = index
        if not self._click(ctx, x, y):
            raise AdapterError("the click could not be sent (window would not take focus)")
        return Observation(RewardState.UNKNOWN, 0.0, f"clicked Day {index + 1} at ({x},{y})", "ui")

    def verify(self, ctx: ClaimContext, before: Observation, timeout: float) -> Observation:
        st = self._st(ctx)
        index = st.get("clicked_day", 0)
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            frame = self._frame(ctx)
            if frame is not None:
                last = frame
                if self.ui.seen(frame, "toast"):
                    return Observation(RewardState.CLAIMED, 0.95, "'Claimed Daily Reward!' toast", "ui")
                if self.ui.seen(frame, "calendar"):
                    if self._label(frame) == "next":
                        return Observation(RewardState.CLAIMED, 0.95, "calendar now shows 'Next Reward In'", "ui")
                    if self.ui.claimed_days(frame)[index]:
                        return Observation(RewardState.CLAIMED, 0.85, f"Day {index + 1} now has its check", "ui")
            self._wait(ctx, 0.25)
        if last is not None and self.ui.seen(last, "calendar") and self._label(last) == "ready":
            return Observation(RewardState.AVAILABLE, 0.8, "calendar still shows 'Reward Ready!'", "ui")
        return Observation(RewardState.UNKNOWN, 0.2, "no sign of the claim after clicking", "ui")


class AnimeDiceExecutorAdapter(AnimeDiceAdapter):
    method = "executor"

    def __init__(self, bridge: bridge_mod.ExecutorBridge):
        self.bridge = bridge
        self._results: dict[str, dict] = {}

    def profile(self) -> dict:
        return {**ANIME_DICE_PROFILE, "claim_method": "executor"}

    def can_claim(self) -> tuple[bool, str]:
        if not self.bridge.running:
            return False, f"The executor bridge is not running ({self.bridge.error or 'not started'})."
        return True, ""

    @staticmethod
    def _uid(ctx: ClaimContext) -> str:
        if not str(ctx.account_id).isdigit():
            raise AdapterError(f"{ctx.username}'s Roblox user id is unknown", retryable=False)
        return str(ctx.account_id)

    def _observe(self, state: dict, source: str) -> Observation:
        claimable, remaining = bridge_mod.reward_status(state)
        day = state.get("day")
        evidence = f"LastDailyRewardClaim={state.get('last')}, DailyRewardsClaimed={day}"
        if claimable is None:
            return Observation(RewardState.UNKNOWN, 0.0, "game data not readable: " + evidence, source)
        if claimable:
            next_day = (int(day or 0) % 7) + 1
            return Observation(RewardState.AVAILABLE, 0.98, evidence, source, reward_day=next_day)
        return Observation(RewardState.CLAIMED, 0.98, f"{evidence}, opens in {remaining / 3600:.1f} h",
                           source, next_eligible_at=time.time() + (remaining or 0))

    def wait_until_ready(self, ctx: ClaimContext, timeout: float) -> Observation:
        try:
            state = self.bridge.wait_for(self._uid(ctx), timeout, ctx.cancel)
        except bridge_mod.BridgeError as exc:
            raise AdapterError(str(exc), retryable="cancelled" not in str(exc)) from exc
        return Observation(RewardState.UNKNOWN, 1.0, f"executor script connected (job {str(state.get('jobId'))[:8]})", "executor")

    def detect_reward(self, ctx: ClaimContext) -> Observation:
        try:
            state = self.bridge.request(self._uid(ctx), "state", 10)
        except bridge_mod.BridgeError as exc:
            raise AdapterError(str(exc)) from exc
        return self._observe(state, "executor")

    def claim(self, ctx: ClaimContext) -> Observation:
        try:
            result = self.bridge.request(self._uid(ctx), "claim", 20)
        except bridge_mod.BridgeError as exc:
            raise AdapterError(str(exc)) from exc
        self._results[ctx.account_id] = result
        return Observation(RewardState.UNKNOWN, 0.0, f"Claim fired={result.get('fired')}", "executor")

    def verify(self, ctx: ClaimContext, before: Observation, timeout: float) -> Observation:
        result = self._results.pop(ctx.account_id, {})
        if result.get("changed"):
            obs = self._observe(result, "executor")
            if obs.state == RewardState.CLAIMED:
                obs.confidence = 0.99
                obs.evidence = f"LastDailyRewardClaim {result.get('before')} -> {result.get('last')}; " + obs.evidence
                return obs
        try:
            state = self.bridge.request(self._uid(ctx), "state", 10)
        except bridge_mod.BridgeError:
            return Observation(RewardState.UNKNOWN, 0.2, "no answer after the claim", "executor")
        return self._observe(state, "executor")
