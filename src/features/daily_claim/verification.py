"""
ClaimVerificationService: turns what the adapter saw into an outcome. A press alone never counts.
"""

from __future__ import annotations

from features.daily_claim.models import Observation, Outcome, RewardState

# Below this an observation is treated as not seen.
MIN_CONFIDENCE = 0.6


def sure(obs: Observation, state: RewardState) -> bool:
    return obs.state == state and obs.confidence >= MIN_CONFIDENCE


def decide_before_claim(detected: Observation, needs_recheck: bool) -> Outcome | None:
    """What the calendar says before any claim. None means go ahead and claim."""
    if sure(detected, RewardState.CLAIMED):
        # After an attempt that may have landed, "claimed" is most likely our own claim.
        return Outcome.SUCCESS if needs_recheck else Outcome.ALREADY_CLAIMED
    if sure(detected, RewardState.UNAVAILABLE):
        return Outcome.ALREADY_CLAIMED
    if sure(detected, RewardState.AVAILABLE):
        return None
    # Can't tell: never press blind, least of all after an attempt that may have landed.
    if needs_recheck or not detected.retry_useful:
        return Outcome.NEEDS_MANUAL_REVIEW
    return Outcome.RETRYABLE_FAILURE


def decide_after_claim(before: Observation, after: Observation) -> Outcome:
    if sure(after, RewardState.CLAIMED) or sure(after, RewardState.UNAVAILABLE):
        return Outcome.SUCCESS
    if sure(after, RewardState.AVAILABLE):
        # Still claimable: the press did not land, so another attempt is safe.
        return Outcome.RETRYABLE_FAILURE
    return Outcome.UNCERTAIN
