"""
Game adapters: how one experience's daily reward is found, claimed and verified, and the
optional inspection providers used to learn that.

An adapter never decides the outcome itself; it reports `Observation`s and
`verification.decide` turns them into an `Outcome`. Pressing a button is never proof of a claim.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from features.daily_claim.models import Observation, RewardState


@dataclass
class ClaimContext:
    """What an adapter gets for one attempt on one account."""
    account_id: str
    username: str
    attempt: int
    client: object | None            # clients.ClientHandle (None in simulation)
    cancel: threading.Event
    log: Callable[[str], None] = print
    needs_recheck: bool = False


class AdapterError(Exception):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class GameAutomationAdapter(Protocol):
    game_id: str
    game_name: str
    place_id: str

    def profile(self) -> dict: ...
    def can_claim(self) -> tuple[bool, str]: ...
    def wait_until_ready(self, ctx: ClaimContext, timeout: float) -> Observation: ...
    def detect_reward(self, ctx: ClaimContext) -> Observation: ...
    def claim(self, ctx: ClaimContext) -> Observation: ...
    def verify(self, ctx: ClaimContext, before: Observation, timeout: float) -> Observation: ...


# Anime Dice

ANIME_DICE_PLACE_ID = "113290951185459"

# DailyRewardService/DailyRewardConfig (Potassium decompile, PlaceVersion 713) and live checks on
# 2026-11-06: the claim opens 82,800 s (23 h) after THAT account's last claim ("Next Reward In:
# 23h 0m" right after claiming), there is no shared daily reset, and the 7 days cycle. On join the
# game shows the Offline Money popup first and then the Daily Rewards calendar, but the calendar
# opens by itself only while a reward is ready.
ANIME_DICE_PROFILE = {
    "place_id": ANIME_DICE_PLACE_ID,
    "cooldown_seconds": 82800,
    "cooldown_kind": "rolling_per_account",
    "calendar_days": 7,
    "claim_remote": "ReplicatedStorage.Network.DailyRewardService.RE.Claim (no arguments)",
    "success_indicator": "day tile gets a check, 'Next Reward In: 23h 0m', toast 'Claimed Daily Reward!'",
    "refused_claim_feedback": "none (the server returns silently)",
    "join_popups": ["Offline Money (Welcome Back)", "Daily Rewards (only while claimable)",
                    "Roblox 'Join Event' panel"],
    "evidence": "decompile PlaceVersion 713 + live observation 2026-11-06",
}


class AnimeDiceAdapter:
    """Anime Dice's daily reward. The claim method (visible-UI automation or another path) is not
    chosen yet, so `can_claim` says so and every step reports UNKNOWN: a live run launches and
    closes clients correctly but leaves each account for review instead of guessing."""

    game_id = f"roblox:{ANIME_DICE_PLACE_ID}"
    game_name = "Anime Dice"
    place_id = ANIME_DICE_PLACE_ID

    def profile(self) -> dict:
        return dict(ANIME_DICE_PROFILE)

    def can_claim(self) -> tuple[bool, str]:
        return False, "No claim method is configured for Anime Dice yet."

    def wait_until_ready(self, ctx: ClaimContext, timeout: float) -> Observation:
        # The client manager already waited for the log to show this place joined.
        return Observation(RewardState.UNKNOWN, 0.6, "client log reports the place joined", "log")

    def detect_reward(self, ctx: ClaimContext) -> Observation:
        return Observation(
            RewardState.UNKNOWN, 0.0,
            "reward state not readable: no claim method configured", "none",
        )

    def claim(self, ctx: ClaimContext) -> Observation:
        raise AdapterError("No claim method is configured for Anime Dice.", retryable=False)

    def verify(self, ctx: ClaimContext, before: Observation, timeout: float) -> Observation:
        return Observation(RewardState.UNKNOWN, 0.0, "no verification source configured", "none")


# Simulation

@dataclass
class SimScript:
    """How one simulated account behaves. Each list is consumed one entry per attempt; the last
    entry repeats."""
    initial: list[RewardState] = field(default_factory=lambda: [RewardState.AVAILABLE])
    # "ok", "slow", "timeout" (game never loads), "crash" (client dies)
    load: list[str] = field(default_factory=lambda: ["ok"])
    # "ok" (claim lands, verify sees it), "silent" (lands, verify can't see it: uncertain),
    # "lost" (does not land, verify sees still available), "error" (adapter error)
    claim: list[str] = field(default_factory=lambda: ["ok"])
    work_seconds: float = 0.02


class SimulatedAdapter:
    """Drives the whole pipeline without Roblox. Each account keeps a hidden "server" state, so
    double claims are detectable in tests (`claims_landed`)."""

    game_id = f"roblox:{ANIME_DICE_PLACE_ID}"
    game_name = "Anime Dice (simulated)"
    place_id = ANIME_DICE_PLACE_ID

    def __init__(self, scripts: dict[str, SimScript] | None = None, default: SimScript | None = None,
                 cooldown_seconds: float = 82800, jitter: float = 0.0, seed: int | None = None):
        self.scripts = scripts or {}
        self.default = default or SimScript()
        self.cooldown_seconds = cooldown_seconds
        self.jitter = jitter
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._server: dict[str, RewardState] = {}
        self._claimed_at: dict[str, float] = {}
        self.claims_landed: dict[str, int] = {}
        self.claim_calls: dict[str, int] = {}

    def _script(self, account_id: str) -> SimScript:
        return self.scripts.get(account_id, self.default)

    @staticmethod
    def _pick(seq: list, attempt: int):
        return seq[min(max(attempt, 1), len(seq)) - 1]

    def _sleep(self, ctx: ClaimContext, seconds: float) -> None:
        seconds += self._rng.uniform(0, self.jitter) if self.jitter else 0.0
        if ctx.cancel.wait(seconds):
            raise AdapterError("cancelled", retryable=False)

    def profile(self) -> dict:
        return {**ANIME_DICE_PROFILE, "claim_method": "simulated", "evidence": "simulation"}

    def can_claim(self) -> tuple[bool, str]:
        return True, "simulation"

    def wait_until_ready(self, ctx: ClaimContext, timeout: float) -> Observation:
        script = self._script(ctx.account_id)
        load = self._pick(script.load, ctx.attempt)
        with self._lock:
            if ctx.account_id not in self._server:
                self._server[ctx.account_id] = self._pick(script.initial, 1)
        if load == "timeout":
            self._sleep(ctx, min(timeout, script.work_seconds * 3))
            raise AdapterError("game did not finish loading in time")
        if load == "crash":
            self._sleep(ctx, script.work_seconds)
            raise AdapterError("client exited while loading")
        self._sleep(ctx, script.work_seconds * (4 if load == "slow" else 1))
        return Observation(RewardState.UNKNOWN, 1.0, "simulated game loaded", "simulated")

    def detect_reward(self, ctx: ClaimContext) -> Observation:
        self._sleep(ctx, self._script(ctx.account_id).work_seconds)
        with self._lock:
            state = self._server.get(ctx.account_id, RewardState.UNKNOWN)
            claimed_at = self._claimed_at.get(ctx.account_id)
        nxt = claimed_at + self.cooldown_seconds if claimed_at else None
        return Observation(state, 1.0 if state != RewardState.UNKNOWN else 0.0,
                           f"simulated calendar shows {state}", "simulated", next_eligible_at=nxt)

    def claim(self, ctx: ClaimContext) -> Observation:
        script = self._script(ctx.account_id)
        action = self._pick(script.claim, ctx.attempt)
        with self._lock:
            self.claim_calls[ctx.account_id] = self.claim_calls.get(ctx.account_id, 0) + 1
        self._sleep(ctx, script.work_seconds)
        if action == "error":
            raise AdapterError("claim button not found")
        if action in ("ok", "silent"):
            with self._lock:
                if self._server.get(ctx.account_id) == RewardState.AVAILABLE:
                    self._server[ctx.account_id] = RewardState.CLAIMED
                    self._claimed_at[ctx.account_id] = time.time()
                    self.claims_landed[ctx.account_id] = self.claims_landed.get(ctx.account_id, 0) + 1
        return Observation(RewardState.UNKNOWN, 0.0, f"claim pressed ({action})", "simulated")

    def verify(self, ctx: ClaimContext, before: Observation, timeout: float) -> Observation:
        script = self._script(ctx.account_id)
        action = self._pick(script.claim, ctx.attempt)
        self._sleep(ctx, script.work_seconds)
        if action == "silent":
            return Observation(RewardState.UNKNOWN, 0.2, "no confirmation seen", "simulated")
        return self.detect_reward(ctx)


# Inspection providers (diagnostics only)

class GameInspectionProvider(Protocol):
    name: str

    def connected(self) -> bool: ...
    def capabilities(self) -> list[str]: ...
    def observe(self, target: str) -> dict: ...


class NullInspectionProvider:
    """No inspection source. An MCP server attached to Claude is not reachable from this app, so
    a provider is added here only for a source the app itself can talk to, built from that
    source's real, documented interface."""

    name = "none"

    def connected(self) -> bool:
        return False

    def capabilities(self) -> list[str]:
        return []

    def observe(self, target: str) -> dict:
        return {"ok": False, "reason": "no inspection provider is connected"}


ADAPTERS: dict[str, Callable[[], GameAutomationAdapter]] = {
    AnimeDiceAdapter.game_id: AnimeDiceAdapter,
}
