"""
features/daily_claim
Daily Claim Manager: claims one experience's daily login reward for many accounts.

Accounts are worked through a continuously refilled pool of client slots: when one account's
client is done (claimed, already claimed, failed or cancelled) and closed, the next queued
account is launched into that slot while the others keep going. Every state change is written
to SQLite (`store`), so a session interrupted by a crash or restart is reconciled and resumed.

The game-specific part (how a reward is found, claimed and verified) sits behind
`adapters.GameAutomationAdapter`. Anime Dice's claim method is not chosen yet, so its adapter
reports every account as needing review instead of guessing; the simulated adapter drives the
whole pipeline without Roblox for testing.
"""

from features.daily_claim.models import (  # noqa: F401
    ClaimSettings,
    Observation,
    Outcome,
    RewardState,
    TaskStatus,
)
