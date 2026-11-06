"""
DailyClaimOrchestrator: one session's queue, slots, retries, pause/resume/stop and progress.

Slots are refilled one at a time as they free up: with 10 slots, the 11th account launches as
soon as any one of the first ten has finished and its client is closed, while the other nine
keep working. There is no batch barrier. Retries wait in a timed list of their own, so a
backing-off account never holds a slot or blocks the accounts behind it.
"""

from __future__ import annotations

import collections
import heapq
import threading
import time
import traceback
from collections.abc import Callable

import psutil

from features.daily_claim import verification
from features.daily_claim.adapters import (
    AdapterError,
    ClaimContext,
    GameAutomationAdapter,
)
from features.daily_claim.clients import (
    ClientHandle,
    ClientManager,
    LaunchError,
    close_orphans,
)
from features.daily_claim.models import (
    ACTIVE,
    CLAIM_MAY_HAVE_LANDED,
    TERMINAL,
    ClaimAccount,
    ClaimSettings,
    Observation,
    Outcome,
    SessionStatus,
    TaskStatus,
)
from features.daily_claim.store import ClaimStore, DuplicateTaskError

EventFn = Callable[[str, dict], None]


class SessionConflict(Exception):
    """Another claim session is still running or waiting to be recovered."""


def default_resource_probe(settings: ClaimSettings) -> tuple[bool, str]:
    """(busy, why). Busy only delays the next launch; running clients are never closed for it."""
    try:
        cpu = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory().percent
    except (psutil.Error, OSError):
        return False, ""
    if cpu >= settings.max_cpu_percent:
        return True, f"CPU at {cpu:.0f}%"
    if memory >= settings.max_memory_percent:
        return True, f"memory at {memory:.0f}%"
    return False, ""


def suggested_max_clients(per_client_mb: float = 800.0) -> int:
    """A rough ceiling from free memory, shown next to the custom limit."""
    try:
        available = psutil.virtual_memory().available / 1048576
    except (psutil.Error, OSError):
        return ClaimSettings.HARD_MAX_CLIENTS
    return max(1, min(ClaimSettings.HARD_MAX_CLIENTS, int(available // per_client_mb)))


class DailyClaimOrchestrator:
    def __init__(
        self,
        store: ClaimStore,
        adapter: GameAutomationAdapter,
        clients: ClientManager,
        settings: ClaimSettings,
        on_event: EventFn | None = None,
        resource_probe: Callable[[ClaimSettings], tuple[bool, str]] | None = None,
        log: Callable[[str], None] = print,
    ):
        self.store = store
        self.adapter = adapter
        self.clients = clients
        self.settings = settings.normalized()
        self._on_event = on_event or (lambda *_: None)
        self._probe = resource_probe or default_resource_probe
        self._log = log

        self.session_id: int | None = None
        self._cond = threading.Condition()
        self._queue: collections.deque[int] = collections.deque()
        self._retry: list[tuple[float, int]] = []
        self._active: dict[int, threading.Thread] = {}
        self._handles: dict[int, ClientHandle] = {}
        self._cancel = threading.Event()
        self._paused = False
        self._stopping = False
        self._dispatcher: threading.Thread | None = None
        self._last_launch = 0.0
        self._throttle_reason = ""
        self.max_active_seen = 0

        profile = adapter.profile()
        self._cooldown = float(profile.get("cooldown_seconds") or 0) or None
        store.upsert_profile(adapter.game_id, adapter.game_name, profile)

    # public controls

    @property
    def running(self) -> bool:
        return bool(self._dispatcher and self._dispatcher.is_alive())

    @property
    def paused(self) -> bool:
        return self._paused

    def start(self, accounts: list[ClaimAccount], trigger: str = "manual", account_group: str = "") -> int:
        if self.running:
            raise SessionConflict("A claim session is already running.")
        if self.store.unfinished_sessions():
            raise SessionConflict("An interrupted session has to be recovered or closed first.")
        game_id = self.adapter.game_id
        self.session_id = self.store.create_session(
            game_id, trigger, account_group, self.settings.to_dict())
        self._log(f"[DailyClaim] Session {self.session_id} ({trigger}): {len(accounts)} accounts, "
                  f"up to {self.settings.max_clients} clients at once")
        now = time.time()
        seen: set[str] = set()
        for account in accounts:
            if account.account_id in seen:
                continue
            seen.add(account.account_id)
            skip = self._skip_reason(account, now)
            if skip:
                self.store.add_task(
                    self.session_id, account.account_id, account.username, game_id,
                    f"skip:{self.session_id}:{account.account_id}", TaskStatus.ALREADY_CLAIMED,
                    evidence=skip,
                )
                continue
            try:
                task_id = self.store.add_task(
                    self.session_id, account.account_id, account.username, game_id,
                    self.store.period_key(account.account_id, game_id),
                )
                self._queue.append(task_id)
            except DuplicateTaskError as exc:
                self._log(f"[DailyClaim] Skipped {account.username}: {exc}")
        self.store.refresh_session_counts(self.session_id)
        self._launch_dispatcher()
        return self.session_id

    def pause(self) -> None:
        with self._cond:
            if self.session_id is None or self._stopping:
                return
            self._paused = True
            self._cond.notify_all()
        self.store.set_session_status(self.session_id, SessionStatus.PAUSED)
        self._emit_progress()

    def resume(self) -> None:
        with self._cond:
            if self.session_id is None or self._stopping:
                return
            self._paused = False
            self._cond.notify_all()
        self.store.set_session_status(self.session_id, SessionStatus.RUNNING)
        self._emit_progress()

    def stop(self) -> None:
        """Cancel queued accounts; running ones stop at their next step and close their client."""
        with self._cond:
            if self.session_id is None:
                return
            self._stopping = True
            self._paused = False
            self._cancel.set()
            queued = list(self._queue) + [tid for _, tid in self._retry]
            self._queue.clear()
            self._retry.clear()
            self._cond.notify_all()
        for task_id in queued:
            self.store.set_task_status(task_id, TaskStatus.CANCELLED, last_error="stopped by the user")
        self.store.set_session_status(self.session_id, SessionStatus.STOPPING)
        self._emit_progress()

    def retry_failed(self) -> int:
        if self.session_id is None or self._stopping:
            return 0
        moved = self.store.requeue_failed(self.session_id)
        if not moved:
            return 0
        with self._cond:
            self._queue.extend(moved)
            self._cond.notify_all()
        if not self.running:
            self.store.set_session_status(self.session_id, SessionStatus.RUNNING)
            self._launch_dispatcher()
        self._emit_progress()
        return len(moved)

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._dispatcher
        if thread:
            thread.join(timeout)
        return not self.running

    def progress(self) -> dict:
        tasks = self.store.session_tasks(self.session_id) if self.session_id else []
        counts = collections.Counter(t["Status"] for t in tasks)
        session = self.store.get_session(self.session_id) if self.session_id else None
        started = session["StartedAt"] if session else None
        finished = session["FinishedAt"] if session else None
        processing = sum(counts[s.value] for s in ACTIVE)
        terminal = sum(counts[s.value] for s in TERMINAL)
        return {
            "session_id": self.session_id,
            "status": session["Status"] if session else "",
            "total": len(tasks),
            "success": counts[TaskStatus.COMPLETED.value],
            "already_claimed": counts[TaskStatus.ALREADY_CLAIMED.value],
            "failed": counts[TaskStatus.FAILED.value],
            "review": counts[TaskStatus.NEEDS_MANUAL_REVIEW.value],
            "cancelled": counts[TaskStatus.CANCELLED.value],
            "processing": processing,
            "remaining": len(tasks) - terminal,
            "active_clients": len(self._active),
            "max_clients": self.settings.max_clients,
            "duration": ((finished or time.time()) - started) if started else 0.0,
            "paused": self._paused,
            "throttle": self._throttle_reason,
        }

    # recovery

    def recover(self) -> int | None:
        """Close clients a crashed run left behind, then take up its unfinished session (or close
        it, when recovery is off). Returns the resumed session id."""
        closed = close_orphans(self.store, getattr(self.clients, "close_key", None))
        if closed:
            self._log(f"[DailyClaim] Closed {closed} client(s) left over from an interrupted run")
        sessions = self.store.unfinished_sessions()
        resumed = None
        for session in sessions:
            sid = int(session["SessionId"])
            resume_this = (
                self.settings.recover_after_restart and resumed is None
                and session["Status"] != SessionStatus.STOPPING.value
                and session["GameIdentifier"] == self.adapter.game_id
            )
            for task in self.store.session_tasks(sid):
                status = TaskStatus(task["Status"])
                if status in TERMINAL:
                    continue
                landed = status in CLAIM_MAY_HAVE_LANDED or bool(task["NeedsRecheck"])
                if not resume_this:
                    self.store.set_task_status(
                        task["TaskId"],
                        TaskStatus.NEEDS_MANUAL_REVIEW if landed else TaskStatus.CANCELLED,
                        last_error="interrupted by an app restart" + (
                            " after the claim was pressed" if landed else ""),
                        needs_recheck=landed,
                    )
                    continue
                # Requeue; a claim that may have landed is checked again before anything else.
                self.store.set_task_status(
                    task["TaskId"], TaskStatus.QUEUED, needs_recheck=landed,
                    last_error="resumed after an app restart",
                )
                if task["AttemptCount"] >= self.settings.max_attempts:
                    with self.store._tx() as c:
                        c.execute("UPDATE ClaimTasks SET AttemptCount=? WHERE TaskId=?",
                                  (self.settings.max_attempts - 1, task["TaskId"]))
            if resume_this:
                resumed = sid
            else:
                self.store.set_session_status(sid, SessionStatus.INTERRUPTED, finished=True)
        if resumed is not None:
            self.session_id = resumed
            self.store.set_session_status(resumed, SessionStatus.RUNNING)
            for task in self.store.session_tasks(resumed):
                if task["Status"] == TaskStatus.QUEUED.value:
                    self._queue.append(task["TaskId"])
                elif task["Status"] == TaskStatus.RETRY_PENDING.value:
                    heapq.heappush(self._retry, (float(task["NotBefore"] or 0), task["TaskId"]))
            self._log(f"[DailyClaim] Resuming interrupted session {resumed}")
            self._launch_dispatcher()
        return resumed

    # internals

    def _skip_reason(self, account: ClaimAccount, now: float) -> str:
        state = self.store.account_state(account.account_id, self.adapter.game_id)
        if not state or not state["NextEligibleAt"] or float(state["NextEligibleAt"]) <= now:
            return ""
        verified = state["LastOutcome"] == Outcome.SUCCESS.value
        if verified or self.settings.skip_already_claimed:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(state["NextEligibleAt"])))
            kind = "claimed here" if verified else "seen as claimed"
            return f"local record: {kind}, next claim opens {when}"
        return ""

    def _emit(self, event: str, data: dict) -> None:
        try:
            self._on_event(event, data)
        except Exception as exc:
            self._log(f"[DailyClaim] event handler: {type(exc).__name__}: {exc}")

    def _emit_progress(self) -> None:
        if self.session_id is not None:
            self._emit("progress", self.progress())

    def _launch_dispatcher(self) -> None:
        self._cancel.clear()
        self._stopping = False
        self._dispatcher = threading.Thread(target=self._dispatch, daemon=True, name="DailyClaim-dispatch")
        self._dispatcher.start()

    def _dispatch(self) -> None:
        settings = self.settings
        self._emit_progress()
        while True:
            with self._cond:
                now = time.time()
                while self._retry and self._retry[0][0] <= now:
                    self._queue.append(heapq.heappop(self._retry)[1])
                if self._stopping and not self._active:
                    break
                if not self._queue and not self._retry and not self._active:
                    break
                wait = 0.5
                can_launch = (
                    self._queue and not self._paused and not self._stopping
                    and len(self._active) < settings.max_clients
                    and self.clients.active_count() < settings.max_clients
                )
                if can_launch:
                    gap = settings.launch_gap_seconds - (now - self._last_launch)
                    if gap > 0:
                        can_launch, wait = False, gap
                if can_launch:
                    busy, why = self._probe(settings)
                    if busy and self._active:
                        # With nothing running, launch anyway so the session can't stall forever.
                        if why != self._throttle_reason:
                            self._log(f"[DailyClaim] Holding the next launch: {why}")
                        self._throttle_reason = why
                        can_launch, wait = False, 2.0
                    else:
                        self._throttle_reason = ""
                if can_launch:
                    task_id = self._queue.popleft()
                    thread = threading.Thread(
                        target=self._run_task, args=(task_id,), daemon=True, name=f"DailyClaim-{task_id}")
                    self._active[task_id] = thread
                    self.max_active_seen = max(self.max_active_seen, len(self._active))
                    self._last_launch = now
                    thread.start()
                    continue
                if self._retry:
                    wait = min(wait if self._queue else 5.0, max(0.05, self._retry[0][0] - now))
                self._cond.wait(timeout=wait)

        status = SessionStatus.CANCELLED if self._stopping else SessionStatus.FINISHED
        if self.session_id is not None:
            self.store.set_session_status(self.session_id, status, finished=True)
            p = self.progress()
            self._log(f"[DailyClaim] Session {self.session_id} {status.value.lower()}: "
                      f"{p['success']} claimed, {p['already_claimed']} already claimed, "
                      f"{p['failed']} failed, {p['review']} need review, {p['cancelled']} cancelled")
        self._emit_progress()
        self._emit("finished", {"session_id": self.session_id, "status": status.value})

    def _set(self, task_id: int, status: TaskStatus, **kw) -> None:
        self.store.set_task_status(task_id, status, **kw)
        self._emit("task", {"task_id": task_id, "status": status.value})

    def _run_task(self, task_id: int) -> None:
        settings = self.settings
        task = self.store.get_task(task_id)
        attempt_id, number = self.store.begin_attempt(task_id)
        ctx = ClaimContext(
            account_id=task["AccountId"], username=task["Username"], attempt=number, client=None,
            cancel=self._cancel, log=self._log, needs_recheck=bool(task["NeedsRecheck"]),
        )
        evidence: list[str] = []
        outcome = Outcome.RETRYABLE_FAILURE
        error = ""
        last_obs: Observation | None = None
        handle: ClientHandle | None = None
        client_row: int | None = None
        pressed = False

        try:
            self._set(task_id, TaskStatus.LAUNCHING)
            handle = self.clients.launch(ctx.username, self.adapter.place_id, self._cancel,
                                         settings.game_load_timeout)
            ctx.client = handle
            with self._cond:
                self._handles[task_id] = handle
            client_row = self.store.add_client(task_id, handle.pid, handle.create_time)

            self._set(task_id, TaskStatus.WAITING_FOR_GAME)
            evidence.append(self.clients.wait_in_game(handle, self.adapter.place_id, self._cancel,
                                                      settings.game_load_timeout))
            evidence.append(self.adapter.wait_until_ready(ctx, settings.game_load_timeout).describe())

            self._set(task_id, TaskStatus.DETECTING_REWARD)
            detected = last_obs = self.adapter.detect_reward(ctx)
            evidence.append("before: " + detected.describe())
            pre = verification.decide_before_claim(detected, ctx.needs_recheck)
            claimable, why_not = self.adapter.can_claim()
            if pre is not None:
                outcome = pre
                if pre == Outcome.NEEDS_MANUAL_REVIEW:
                    error = "reward state unclear after an attempt that may have claimed it"
                elif pre == Outcome.RETRYABLE_FAILURE and not claimable:
                    # Another launch would read nothing more.
                    outcome, error = Outcome.NEEDS_MANUAL_REVIEW, why_not
                elif pre == Outcome.RETRYABLE_FAILURE:
                    error = "could not read the reward state"
            else:
                if not claimable:
                    outcome, error = Outcome.NEEDS_MANUAL_REVIEW, why_not
                else:
                    # Written down before pressing, so a crash mid-claim is re-checked, never re-pressed.
                    self._set(task_id, TaskStatus.CLAIMING, needs_recheck=True)
                    pressed = True
                    evidence.append("claim: " + self.adapter.claim(ctx).describe())
                    self._set(task_id, TaskStatus.VERIFYING)
                    after = last_obs = self.adapter.verify(ctx, detected, settings.verification_timeout)
                    evidence.append("after: " + after.describe())
                    outcome = verification.decide_after_claim(detected, after)
                    if outcome == Outcome.UNCERTAIN:
                        again = last_obs = self.adapter.detect_reward(ctx)
                        evidence.append("re-check: " + again.describe())
                        decided = verification.decide_before_claim(again, True)
                        if decided is None:
                            outcome, error = Outcome.RETRYABLE_FAILURE, "claim did not land (still available)"
                        elif decided == Outcome.SUCCESS:
                            outcome = Outcome.SUCCESS
                        else:
                            error = "no reliable sign that the claim landed"
                    elif outcome == Outcome.RETRYABLE_FAILURE:
                        error = "claim did not land (still available)"
                    if outcome in (Outcome.RETRYABLE_FAILURE, Outcome.SUCCESS):
                        self.store.set_task_status(task_id, TaskStatus.VERIFYING, needs_recheck=False)
        except LaunchError as exc:
            error = str(exc)
            outcome = (Outcome.NEEDS_MANUAL_REVIEW if exc.review
                       else Outcome.RETRYABLE_FAILURE if exc.retryable else Outcome.FINAL_FAILURE)
        except AdapterError as exc:
            error = str(exc)
            outcome = Outcome.RETRYABLE_FAILURE if exc.retryable else Outcome.FINAL_FAILURE
            if pressed:
                outcome = Outcome.UNCERTAIN
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            evidence.append(traceback.format_exc(limit=4))
            outcome = Outcome.UNCERTAIN if pressed else Outcome.RETRYABLE_FAILURE

        if self._cancel.is_set() and outcome not in (Outcome.SUCCESS, Outcome.ALREADY_CLAIMED):
            outcome = Outcome.UNCERTAIN if pressed else Outcome.CANCELLED
            error = error or "stopped by the user"

        if handle is not None:
            self._set(task_id, TaskStatus.CLOSING)
            closed = False
            try:
                closed = self.clients.close(handle)
            except Exception as exc:
                self._log(f"[DailyClaim] Closing {ctx.username}'s client failed: {exc}")
            if client_row is not None:
                self.store.finish_client(client_row, "closed" if closed else "close-failed")
        release = getattr(self.adapter, "release", None)
        if release is not None:
            try:
                release(ctx)
            except Exception as exc:
                self._log(f"[DailyClaim] Adapter cleanup for {ctx.username}: {exc}")

        final = self._resolve(task_id, ctx, number, outcome, error, last_obs, "\n".join(evidence))
        self.store.finish_attempt(attempt_id, outcome.value, error, "\n".join(evidence))
        self.store.refresh_session_counts(self.session_id)
        self._log(f"[DailyClaim] {ctx.username}: {final.value}"
                  f" (attempt {number}/{settings.max_attempts}){' - ' + error if error else ''}")
        with self._cond:
            self._active.pop(task_id, None)
            self._handles.pop(task_id, None)
            self._cond.notify_all()
        self._emit_progress()

    def _resolve(self, task_id: int, ctx: ClaimContext, number: int, outcome: Outcome, error: str,
                 obs: Observation | None, evidence: str) -> TaskStatus:
        game_id = self.adapter.game_id
        now = time.time()
        if outcome == Outcome.SUCCESS:
            nxt = (obs.next_eligible_at if obs and obs.next_eligible_at
                   else now + self._cooldown if self._cooldown else None)
            self.store.record_claim_result(ctx.account_id, game_id, outcome.value, evidence, now, nxt)
            self._set(task_id, TaskStatus.COMPLETED, evidence=evidence, last_error="")
            return TaskStatus.COMPLETED
        if outcome == Outcome.ALREADY_CLAIMED:
            nxt = obs.next_eligible_at if obs else None
            self.store.record_claim_result(ctx.account_id, game_id, outcome.value, evidence, None, nxt)
            self._set(task_id, TaskStatus.ALREADY_CLAIMED, evidence=evidence, last_error="")
            return TaskStatus.ALREADY_CLAIMED
        if outcome == Outcome.CANCELLED:
            self._set(task_id, TaskStatus.CANCELLED, evidence=evidence, last_error=error)
            return TaskStatus.CANCELLED

        retryable = outcome in (Outcome.RETRYABLE_FAILURE, Outcome.UNCERTAIN)
        if retryable and number < self.settings.max_attempts and not self._stopping:
            delay = self.settings.retry_delay_seconds * (2 ** (number - 1))
            not_before = now + delay
            self._set(task_id, TaskStatus.RETRY_PENDING, evidence=evidence, last_error=error,
                      not_before=not_before, needs_recheck=(outcome == Outcome.UNCERTAIN) or None)
            with self._cond:
                heapq.heappush(self._retry, (not_before, task_id))
            return TaskStatus.RETRY_PENDING

        status = (TaskStatus.NEEDS_MANUAL_REVIEW
                  if outcome in (Outcome.UNCERTAIN, Outcome.NEEDS_MANUAL_REVIEW) else TaskStatus.FAILED)
        self.store.record_outcome_only(ctx.account_id, game_id, outcome.value, evidence)
        self._set(task_id, status, evidence=evidence, last_error=error)
        return status
