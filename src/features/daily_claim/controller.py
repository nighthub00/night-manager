"""
The app-facing side of the Daily Claim Manager: settings, which accounts to run, one
orchestrator at a time, start-up recovery and the scheduler.

Simulation runs use their own database file, so made-up results never make a real run skip
an account.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable

from features.daily_claim import bridge as bridge_mod
from features.daily_claim import screen as screen_mod
from features.daily_claim.adapters import AnimeDiceAdapter, SimScript, SimulatedAdapter
from features.daily_claim.anime_dice import AnimeDiceExecutorAdapter, AnimeDiceScreenAdapter
from features.daily_claim.clients import RobloxClientManager, SimulatedClientManager
from features.daily_claim.models import ClaimAccount, ClaimSettings, RewardState
from features.daily_claim.orchestrator import DailyClaimOrchestrator, SessionConflict
from features.daily_claim.scheduler import ClaimScheduler
from features.daily_claim.store import ClaimStore

SETTINGS_KEY = "daily_claim"
TOKEN_KEY = "daily_claim_bridge_token"
ALL_GROUPS = ""


def load_settings() -> ClaimSettings:
    from features.account_actions import get_ui_setting
    return ClaimSettings.from_dict(get_ui_setting(SETTINGS_KEY, {}))


def save_settings(settings: ClaimSettings) -> None:
    from features.account_actions import save_ui_setting
    save_ui_setting(SETTINGS_KEY, settings.normalized().to_dict())


def account_id_for(username: str, data: dict) -> str:
    user_id = str((data or {}).get("user_id", "") or "")
    return user_id if user_id and user_id != "0" else username


def accounts_for(manager, usernames: list[str] | None = None, group: str = ALL_GROUPS) -> list[ClaimAccount]:
    """Accounts in the manager's order; `usernames` narrows to a selection, `group` to a group."""
    from features import groups as groups_mod

    lock = getattr(manager, "_accounts_lock", None)
    if lock is not None:
        with lock:
            items = list(manager.accounts.items())
    else:
        items = list(manager.accounts.items())
    assignments = groups_mod.get_assignments() if group else {}
    wanted = set(usernames) if usernames else None
    result = []
    for username, data in items:
        if wanted is not None and username not in wanted:
            continue
        if group and assignments.get(username) != group:
            continue
        result.append(ClaimAccount(account_id_for(username, data if isinstance(data, dict) else {}), username))
    return result


def _demo_adapter() -> SimulatedAdapter:
    """A believable mix for a dry run: most claim, some were already claimed, a few stumble."""
    import random

    class _Mixed(SimulatedAdapter):
        def _script(self, account_id: str) -> SimScript:
            script = self.scripts.get(account_id)
            if script is None:
                roll = random.Random(account_id).random()
                if roll < 0.15:
                    script = SimScript(initial=[RewardState.CLAIMED], work_seconds=0.8)
                elif roll < 0.25:
                    script = SimScript(load=["timeout", "ok"], work_seconds=0.8)
                elif roll < 0.30:
                    script = SimScript(claim=["silent", "ok"], work_seconds=0.8)
                else:
                    script = SimScript(work_seconds=random.Random(account_id + "w").uniform(0.6, 1.6))
                self.scripts[account_id] = script
            return script

    return _Mixed()


class DailyClaimController:
    def __init__(self, manager, on_event: Callable[[str, dict], None] | None = None,
                 log: Callable[[str], None] = print):
        self.manager = manager
        self._on_event = on_event or (lambda *_: None)
        self._log = log
        self._lock = threading.RLock()
        self._stores: dict[bool, ClaimStore] = {}
        self.orchestrator: DailyClaimOrchestrator | None = None
        self.settings = load_settings()
        self.scheduler: ClaimScheduler | None = None
        self.bridge: bridge_mod.ExecutorBridge | None = None
        self.apply_settings(self.settings)

    # claim method

    def _token(self) -> str:
        from features.account_actions import get_ui_setting, save_ui_setting
        token = str(get_ui_setting(TOKEN_KEY, "") or "")
        if len(token) < 16:
            import secrets
            token = secrets.token_hex(16)
            save_ui_setting(TOKEN_KEY, token)
        return token

    def apply_settings(self, settings: ClaimSettings) -> None:
        """Starts or stops the executor bridge to match the chosen method and port."""
        self.settings = settings.normalized()
        want = self.settings.claim_method == "executor"
        with self._lock:
            if self.bridge and (not want or self.bridge.port != self.settings.bridge_port):
                if not self.busy:
                    self.bridge.stop()
                    self.bridge = None
            if want and self.bridge is None:
                self.bridge = bridge_mod.ExecutorBridge(self.settings.bridge_port, self._token())
                if not self.bridge.start():
                    self._log(f"[DailyClaim] Executor bridge could not start: {self.bridge.error}")

    def install_script(self) -> str:
        return bridge_mod.install_script(self.settings.autoexec_dir, self.settings.bridge_port, self._token())

    def script_text(self) -> str:
        return bridge_mod.build_script(self.settings.bridge_port, self._token())

    def live_adapter(self, settings: ClaimSettings | None = None):
        settings = settings or self.settings
        if settings.claim_method == "executor":
            if self.bridge is None:
                self.apply_settings(settings)
            return AnimeDiceExecutorAdapter(self.bridge)
        from features.client_tracker import get_tracker
        return AnimeDiceScreenAdapter(
            screen_mod.WindowsScreen(lambda: get_tracker(self.manager)),
            popup_timeout=settings.popup_timeout,
        )

    def store(self, simulation: bool) -> ClaimStore:
        with self._lock:
            if simulation not in self._stores:
                from utils.app_paths import get_data_dir
                name = "daily_claim_simulation.sqlite3" if simulation else "daily_claim.sqlite3"
                self._stores[simulation] = ClaimStore(os.path.join(get_data_dir(), name))
            return self._stores[simulation]

    def live_store(self) -> ClaimStore:
        return self.store(False)

    def _build(self, settings: ClaimSettings) -> DailyClaimOrchestrator:
        if settings.simulation:
            adapter, clients = _demo_adapter(), SimulatedClientManager(launch_seconds=0.4)
        else:
            adapter, clients = self.live_adapter(settings), RobloxClientManager(self.manager)
        return DailyClaimOrchestrator(self.store(settings.simulation), adapter, clients, settings,
                                      on_event=self._on_event, log=self._log)

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self.orchestrator and self.orchestrator.running)

    def claim_method_note(self) -> str:
        ok, why = self.live_adapter().can_claim()
        return "" if ok else why

    def start(self, accounts: list[ClaimAccount], trigger: str = "manual", group: str = "",
              settings: ClaimSettings | None = None) -> int:
        with self._lock:
            if self.busy:
                raise SessionConflict("A claim session is already running.")
            if settings is not None:
                self.settings = settings.normalized()
            self.orchestrator = self._build(self.settings)
            return self.orchestrator.start(accounts, trigger, group)

    def recover(self) -> int | None:
        """At start-up: live sessions a crash or restart cut short."""
        with self._lock:
            if self.busy:
                return None
            settings = ClaimSettings.from_dict({**self.settings.to_dict(), "simulation": False})
            orchestrator = DailyClaimOrchestrator(
                self.live_store(), self.live_adapter(settings), RobloxClientManager(self.manager), settings,
                on_event=self._on_event, log=self._log)
            resumed = orchestrator.recover()
            if resumed is not None:
                self.orchestrator = orchestrator
            # Simulation sessions are never resumed, just closed.
            sim = self.store(True)
            if sim.unfinished_sessions():
                DailyClaimOrchestrator(
                    sim, SimulatedAdapter(), SimulatedClientManager(),
                    ClaimSettings.from_dict({"recover_after_restart": False}), log=self._log,
                ).recover()
            return resumed

    def start_scheduler(self) -> ClaimScheduler:
        def start_fn(trigger: str, group: str, accounts: list[ClaimAccount]) -> bool:
            note = self.claim_method_note()
            if note:
                # Launching every account just to mark it for review would only cost resources.
                self._log(f"[DailyClaim] Scheduled run skipped: {note}")
                return False
            try:
                live = ClaimSettings.from_dict({**self.settings.to_dict(), "simulation": False})
                self.start(accounts, trigger, group, settings=live)
                self._on_event("started", {"trigger": trigger})
                return True
            except SessionConflict as exc:
                self._log(f"[DailyClaim] Scheduled run skipped: {exc}")
                return False

        with self._lock:
            if self.scheduler is None:
                self.scheduler = ClaimScheduler(
                    self.live_store(), AnimeDiceAdapter.game_id, start_fn,
                    lambda group: accounts_for(self.manager, group=group),
                    lambda: self.busy, log=self._log,
                )
            self.scheduler.start()
            return self.scheduler

    def shutdown(self) -> None:
        if self.scheduler:
            self.scheduler.stop()
        if self.bridge:
            self.bridge.stop()
        # A running session is left as Running on purpose: next start-up reconciles it.
