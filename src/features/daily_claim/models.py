"""
States, outcomes and settings shared by the Daily Claim Manager.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum


class TaskStatus(StrEnum):
    QUEUED = "Queued"
    LAUNCHING = "Launching"
    WAITING_FOR_GAME = "WaitingForGame"
    DETECTING_REWARD = "DetectingReward"
    CLAIMING = "Claiming"
    VERIFYING = "Verifying"
    CLOSING = "Closing"
    RETRY_PENDING = "RetryPending"
    # Terminal
    COMPLETED = "Completed"
    ALREADY_CLAIMED = "AlreadyClaimed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"
    NEEDS_MANUAL_REVIEW = "NeedsManualReview"


TERMINAL = frozenset({
    TaskStatus.COMPLETED,
    TaskStatus.ALREADY_CLAIMED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.NEEDS_MANUAL_REVIEW,
})

# A client may be running for the task (anything between launch and close).
ACTIVE = frozenset({
    TaskStatus.LAUNCHING,
    TaskStatus.WAITING_FOR_GAME,
    TaskStatus.DETECTING_REWARD,
    TaskStatus.CLAIMING,
    TaskStatus.VERIFYING,
    TaskStatus.CLOSING,
})

# Statuses in which a claim interaction may already have reached the game.
CLAIM_MAY_HAVE_LANDED = frozenset({TaskStatus.CLAIMING, TaskStatus.VERIFYING})

# Statuses that hold an account's slot for its reward period (the idempotency key).
HOLDS_PERIOD = frozenset(set(TaskStatus) - {
    TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.NEEDS_MANUAL_REVIEW,
})


class SessionStatus(StrEnum):
    RUNNING = "Running"
    PAUSED = "Paused"
    STOPPING = "Stopping"
    FINISHED = "Finished"
    CANCELLED = "Cancelled"
    INTERRUPTED = "Interrupted"


class Outcome(StrEnum):
    SUCCESS = "Success"
    ALREADY_CLAIMED = "AlreadyClaimed"
    RETRYABLE_FAILURE = "RetryableFailure"
    FINAL_FAILURE = "FinalFailure"
    UNCERTAIN = "Uncertain"
    NEEDS_MANUAL_REVIEW = "NeedsManualReview"
    CANCELLED = "Cancelled"


class RewardState(StrEnum):
    AVAILABLE = "available"
    CLAIMED = "claimed"          # already claimed this period
    UNAVAILABLE = "unavailable"  # e.g. the countdown is still running
    UNKNOWN = "unknown"


@dataclass
class Observation:
    """One thing the adapter saw, with how sure it is and where it came from."""
    state: RewardState = RewardState.UNKNOWN
    confidence: float = 0.0
    evidence: str = ""
    source: str = ""                  # "simulated", "ui", "mcp", "log", ...
    next_eligible_at: float | None = None
    reward_day: int | None = None
    # False when trying again cannot read more (send to review instead of relaunching).
    retry_useful: bool = True
    at: float = field(default_factory=time.time)

    def describe(self) -> str:
        parts = [f"{self.state} ({self.confidence:.0%}, {self.source or 'n/a'})"]
        if self.evidence:
            parts.append(self.evidence)
        return ": ".join(parts)


CLAIM_METHODS = {"screen": "On-screen clicks", "executor": "Executor script"}


@dataclass
class ClaimSettings:
    max_clients: int = 3
    max_attempts: int = 3
    retry_delay_seconds: float = 20.0
    launch_gap_seconds: float = 2.0
    game_load_timeout: float = 120.0
    verification_timeout: float = 20.0
    skip_already_claimed: bool = True
    recover_after_restart: bool = True
    # Launches wait (healthy clients are never closed for load) while either is over its limit.
    max_cpu_percent: float = 90.0
    max_memory_percent: float = 90.0
    simulation: bool = True
    # "screen" (visible clicks on a 320x240 tile) or "executor" (the auto-execute script)
    claim_method: str = "screen"
    popup_timeout: float = 150.0
    bridge_port: int = 38917
    autoexec_dir: str = ""

    HARD_MAX_CLIENTS = 50

    def normalized(self) -> ClaimSettings:
        def clamp(value, low, high, default):
            try:
                value = float(value)
            except (TypeError, ValueError):
                return default
            if value != value:
                return default
            return max(low, min(high, value))

        return ClaimSettings(
            max_clients=int(clamp(self.max_clients, 1, self.HARD_MAX_CLIENTS, 3)),
            max_attempts=int(clamp(self.max_attempts, 1, 10, 3)),
            retry_delay_seconds=clamp(self.retry_delay_seconds, 0, 3600, 20.0),
            launch_gap_seconds=clamp(self.launch_gap_seconds, 0, 300, 2.0),
            game_load_timeout=clamp(self.game_load_timeout, 10, 1800, 120.0),
            verification_timeout=clamp(self.verification_timeout, 1, 600, 20.0),
            skip_already_claimed=bool(self.skip_already_claimed),
            recover_after_restart=bool(self.recover_after_restart),
            max_cpu_percent=clamp(self.max_cpu_percent, 10, 100, 90.0),
            max_memory_percent=clamp(self.max_memory_percent, 10, 100, 90.0),
            simulation=bool(self.simulation),
            claim_method=self.claim_method if self.claim_method in CLAIM_METHODS else "screen",
            popup_timeout=clamp(self.popup_timeout, 20, 900, 150.0),
            bridge_port=int(clamp(self.bridge_port, 1024, 65535, 38917)),
            autoexec_dir=str(self.autoexec_dir or ""),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> ClaimSettings:
        data = data if isinstance(data, dict) else {}
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known).normalized()


@dataclass
class ClaimAccount:
    account_id: str     # Roblox user id when known, else the username
    username: str


@dataclass
class TaskView:
    """A task row as the dashboard shows it."""
    task_id: int
    session_id: int
    account_id: str
    username: str
    status: str
    attempt_count: int
    created_at: float
    updated_at: float
    completed_at: float | None
    last_error: str
    client_ref: str = ""
    started_at: float | None = None
