"""
SQLite persistence for the Daily Claim Manager: sessions, tasks, attempts, client instances,
game profiles, the schedule and each account's last known claim.

Only account ids and usernames are stored; cookies and passwords stay in the account store.
One connection is shared behind a lock (the orchestrator's workers write from their threads).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from features.daily_claim.models import (
    HOLDS_PERIOD,
    TERMINAL,
    SessionStatus,
    TaskStatus,
    TaskView,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS Meta (
    Key TEXT PRIMARY KEY,
    Value TEXT
);
CREATE TABLE IF NOT EXISTS ClaimSessions (
    SessionId INTEGER PRIMARY KEY AUTOINCREMENT,
    GameIdentifier TEXT NOT NULL,
    Trigger TEXT NOT NULL DEFAULT 'manual',
    AccountGroup TEXT NOT NULL DEFAULT '',
    StartedAt REAL NOT NULL,
    FinishedAt REAL,
    Status TEXT NOT NULL,
    TotalAccounts INTEGER NOT NULL DEFAULT 0,
    CompletedCount INTEGER NOT NULL DEFAULT 0,
    FailedCount INTEGER NOT NULL DEFAULT 0,
    SettingsJson TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS ClaimTasks (
    TaskId INTEGER PRIMARY KEY AUTOINCREMENT,
    SessionId INTEGER NOT NULL REFERENCES ClaimSessions(SessionId),
    AccountId TEXT NOT NULL,
    Username TEXT NOT NULL,
    GameIdentifier TEXT NOT NULL,
    PeriodKey TEXT NOT NULL,
    Status TEXT NOT NULL,
    AttemptCount INTEGER NOT NULL DEFAULT 0,
    NeedsRecheck INTEGER NOT NULL DEFAULT 0,
    NotBefore REAL NOT NULL DEFAULT 0,
    CreatedAt REAL NOT NULL,
    UpdatedAt REAL NOT NULL,
    StartedAt REAL,
    CompletedAt REAL,
    LastError TEXT NOT NULL DEFAULT '',
    Evidence TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS IX_ClaimTasks_Session ON ClaimTasks(SessionId);
CREATE TABLE IF NOT EXISTS ClaimAttempts (
    AttemptId INTEGER PRIMARY KEY AUTOINCREMENT,
    TaskId INTEGER NOT NULL REFERENCES ClaimTasks(TaskId),
    AttemptNumber INTEGER NOT NULL,
    StartedAt REAL NOT NULL,
    FinishedAt REAL,
    Result TEXT NOT NULL DEFAULT '',
    ErrorDetails TEXT NOT NULL DEFAULT '',
    Evidence TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ClientInstances (
    ClientInstanceId INTEGER PRIMARY KEY AUTOINCREMENT,
    TaskId INTEGER NOT NULL REFERENCES ClaimTasks(TaskId),
    ProcessId INTEGER NOT NULL,
    ProcessCreateTime REAL NOT NULL,
    StartedAt REAL NOT NULL,
    FinishedAt REAL,
    Status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS GameProfiles (
    ProfileId INTEGER PRIMARY KEY AUTOINCREMENT,
    GameIdentifier TEXT NOT NULL UNIQUE,
    GameName TEXT NOT NULL,
    AutomationConfiguration TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS ScheduleSettings (
    ScheduleId INTEGER PRIMARY KEY,
    Enabled INTEGER NOT NULL DEFAULT 0,
    LocalStartTime TEXT NOT NULL DEFAULT '09:00',
    TimeZoneId TEXT NOT NULL DEFAULT 'Local',
    AccountGroup TEXT NOT NULL DEFAULT '',
    FollowCooldown INTEGER NOT NULL DEFAULT 1,
    LastExecutionDate TEXT NOT NULL DEFAULT '',
    LastSessionAt REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS AccountClaimState (
    AccountId TEXT NOT NULL,
    GameIdentifier TEXT NOT NULL,
    Generation INTEGER NOT NULL DEFAULT 0,
    LastVerifiedClaimAt REAL,
    NextEligibleAt REAL,
    LastOutcome TEXT NOT NULL DEFAULT '',
    Evidence TEXT NOT NULL DEFAULT '',
    UpdatedAt REAL NOT NULL,
    PRIMARY KEY (AccountId, GameIdentifier)
);
"""


def _holds_period_sql() -> str:
    released = sorted(set(TaskStatus) - HOLDS_PERIOD)
    return ", ".join(f"'{s.value}'" for s in released)


# One active or claimed task per account, game and reward period.
_PERIOD_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS UX_ClaimTasks_Period
ON ClaimTasks(AccountId, GameIdentifier, PeriodKey)
WHERE Status NOT IN ({_holds_period_sql()});
"""


class DuplicateTaskError(Exception):
    """The account already has an active or claimed task for this reward period."""


class ClaimStore:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.executescript(_PERIOD_INDEX)
            self._conn.execute(
                "INSERT OR IGNORE INTO Meta(Key, Value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
            self._conn.execute("INSERT OR IGNORE INTO ScheduleSettings(ScheduleId) VALUES(1)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _one(self, sql: str, args=()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # game profiles

    def upsert_profile(self, game_id: str, name: str, config: dict) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO GameProfiles(GameIdentifier, GameName, AutomationConfiguration) VALUES(?,?,?) "
                "ON CONFLICT(GameIdentifier) DO UPDATE SET GameName=excluded.GameName, "
                "AutomationConfiguration=excluded.AutomationConfiguration",
                (game_id, name, json.dumps(config, sort_keys=True)),
            )

    def get_profile(self, game_id: str) -> dict | None:
        row = self._one("SELECT * FROM GameProfiles WHERE GameIdentifier=?", (game_id,))
        if row is None:
            return None
        try:
            config = json.loads(row["AutomationConfiguration"] or "{}")
        except ValueError:
            config = {}
        return {"game_id": row["GameIdentifier"], "name": row["GameName"], "config": config}

    # sessions

    def create_session(self, game_id: str, trigger: str, account_group: str, settings: dict) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO ClaimSessions(GameIdentifier, Trigger, AccountGroup, StartedAt, Status, SettingsJson) "
                "VALUES(?,?,?,?,?,?)",
                (game_id, trigger, account_group, time.time(), SessionStatus.RUNNING.value,
                 json.dumps(settings, sort_keys=True)),
            )
            return int(cur.lastrowid)

    def set_session_status(self, session_id: int, status: SessionStatus, finished: bool = False) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE ClaimSessions SET Status=?, FinishedAt=CASE WHEN ? THEN ? ELSE FinishedAt END "
                "WHERE SessionId=?",
                (status.value, 1 if finished else 0, time.time(), session_id),
            )
        self.refresh_session_counts(session_id)

    def refresh_session_counts(self, session_id: int) -> None:
        with self._tx() as c:
            c.execute(
                """UPDATE ClaimSessions SET
                    TotalAccounts=(SELECT COUNT(*) FROM ClaimTasks WHERE SessionId=:s),
                    CompletedCount=(SELECT COUNT(*) FROM ClaimTasks WHERE SessionId=:s AND Status IN (:c, :a)),
                    FailedCount=(SELECT COUNT(*) FROM ClaimTasks WHERE SessionId=:s AND Status IN (:f, :r))
                   WHERE SessionId=:s""",
                {"s": session_id, "c": TaskStatus.COMPLETED.value, "a": TaskStatus.ALREADY_CLAIMED.value,
                 "f": TaskStatus.FAILED.value, "r": TaskStatus.NEEDS_MANUAL_REVIEW.value},
            )

    def get_session(self, session_id: int) -> dict | None:
        row = self._one("SELECT * FROM ClaimSessions WHERE SessionId=?", (session_id,))
        return dict(row) if row else None

    def unfinished_sessions(self) -> list[dict]:
        rows = self._all(
            "SELECT * FROM ClaimSessions WHERE Status IN (?,?,?) ORDER BY SessionId",
            (SessionStatus.RUNNING.value, SessionStatus.PAUSED.value, SessionStatus.STOPPING.value),
        )
        return [dict(r) for r in rows]

    def recent_sessions(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self._all(
            "SELECT * FROM ClaimSessions ORDER BY SessionId DESC LIMIT ?", (limit,))]

    # tasks

    def add_task(self, session_id: int, account_id: str, username: str, game_id: str,
                 period_key: str, status: TaskStatus = TaskStatus.QUEUED,
                 last_error: str = "", evidence: str = "") -> int:
        now = time.time()
        try:
            with self._tx() as c:
                cur = c.execute(
                    "INSERT INTO ClaimTasks(SessionId, AccountId, Username, GameIdentifier, PeriodKey, Status, "
                    "CreatedAt, UpdatedAt, CompletedAt, LastError, Evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (session_id, account_id, username, game_id, period_key, status.value, now, now,
                     now if status in TERMINAL else None, last_error, evidence),
                )
                return int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise DuplicateTaskError(f"{username} already has a task for this reward period") from exc

    def set_task_status(self, task_id: int, status: TaskStatus, *, last_error: str | None = None,
                        evidence: str | None = None, needs_recheck: bool | None = None,
                        not_before: float | None = None) -> None:
        now = time.time()
        sets = ["Status=?", "UpdatedAt=?"]
        args: list = [status.value, now]
        if status in TERMINAL:
            sets.append("CompletedAt=?")
            args.append(now)
        elif status == TaskStatus.LAUNCHING:
            sets.append("StartedAt=COALESCE(StartedAt, ?)")
            args.append(now)
        if last_error is not None:
            sets.append("LastError=?")
            args.append(last_error[:2000])
        if evidence is not None:
            sets.append("Evidence=?")
            args.append(evidence[:4000])
        if needs_recheck is not None:
            sets.append("NeedsRecheck=?")
            args.append(1 if needs_recheck else 0)
        if not_before is not None:
            sets.append("NotBefore=?")
            args.append(not_before)
        args.append(task_id)
        with self._tx() as c:
            c.execute(f"UPDATE ClaimTasks SET {', '.join(sets)} WHERE TaskId=?", args)

    def get_task(self, task_id: int) -> dict | None:
        row = self._one("SELECT * FROM ClaimTasks WHERE TaskId=?", (task_id,))
        return dict(row) if row else None

    def session_tasks(self, session_id: int) -> list[dict]:
        return [dict(r) for r in self._all(
            "SELECT * FROM ClaimTasks WHERE SessionId=? ORDER BY TaskId", (session_id,))]

    def task_views(self, session_id: int) -> list[TaskView]:
        rows = self._all(
            """SELECT t.*, (SELECT ProcessId FROM ClientInstances ci WHERE ci.TaskId=t.TaskId
                             ORDER BY ClientInstanceId DESC LIMIT 1) AS Pid
               FROM ClaimTasks t WHERE SessionId=? ORDER BY TaskId""",
            (session_id,),
        )
        return [
            TaskView(
                task_id=r["TaskId"], session_id=r["SessionId"], account_id=r["AccountId"],
                username=r["Username"], status=r["Status"], attempt_count=r["AttemptCount"],
                created_at=r["CreatedAt"], updated_at=r["UpdatedAt"], completed_at=r["CompletedAt"],
                last_error=r["LastError"], client_ref=f"PID {r['Pid']}" if r["Pid"] else "",
                started_at=r["StartedAt"],
            )
            for r in rows
        ]

    def requeue_failed(self, session_id: int) -> list[int]:
        """Failed and review tasks back to Queued with fresh attempts (Retry Failed). A task
        whose period is now held by another task stays as it is."""
        moved: list[int] = []
        for row in self._all(
            "SELECT TaskId FROM ClaimTasks WHERE SessionId=? AND Status IN (?,?)",
            (session_id, TaskStatus.FAILED.value, TaskStatus.NEEDS_MANUAL_REVIEW.value),
        ):
            try:
                with self._tx() as c:
                    c.execute(
                        "UPDATE ClaimTasks SET Status=?, AttemptCount=0, NotBefore=0, CompletedAt=NULL, "
                        "UpdatedAt=? WHERE TaskId=?",
                        (TaskStatus.QUEUED.value, time.time(), row["TaskId"]),
                    )
                moved.append(int(row["TaskId"]))
            except sqlite3.IntegrityError:
                continue
        return moved

    # attempts

    def begin_attempt(self, task_id: int) -> tuple[int, int]:
        with self._tx() as c:
            c.execute("UPDATE ClaimTasks SET AttemptCount=AttemptCount+1, UpdatedAt=? WHERE TaskId=?",
                      (time.time(), task_id))
            number = int(c.execute("SELECT AttemptCount FROM ClaimTasks WHERE TaskId=?",
                                   (task_id,)).fetchone()[0])
            cur = c.execute(
                "INSERT INTO ClaimAttempts(TaskId, AttemptNumber, StartedAt) VALUES(?,?,?)",
                (task_id, number, time.time()),
            )
            return int(cur.lastrowid), number

    def finish_attempt(self, attempt_id: int, result: str, error: str = "", evidence: str = "") -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE ClaimAttempts SET FinishedAt=?, Result=?, ErrorDetails=?, Evidence=? WHERE AttemptId=?",
                (time.time(), result, error[:2000], evidence[:4000], attempt_id),
            )

    def attempts(self, task_id: int) -> list[dict]:
        return [dict(r) for r in self._all(
            "SELECT * FROM ClaimAttempts WHERE TaskId=? ORDER BY AttemptNumber", (task_id,))]

    # client instances

    def add_client(self, task_id: int, pid: int, create_time: float) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO ClientInstances(TaskId, ProcessId, ProcessCreateTime, StartedAt) VALUES(?,?,?,?)",
                (task_id, pid, create_time, time.time()),
            )
            return int(cur.lastrowid)

    def finish_client(self, client_instance_id: int, status: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE ClientInstances SET FinishedAt=?, Status=? WHERE ClientInstanceId=? AND FinishedAt IS NULL",
                (time.time(), status, client_instance_id),
            )

    def open_clients(self) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM ClientInstances WHERE FinishedAt IS NULL")]

    # per-account claim state and the reward period

    def account_state(self, account_id: str, game_id: str) -> dict | None:
        row = self._one("SELECT * FROM AccountClaimState WHERE AccountId=? AND GameIdentifier=?",
                        (account_id, game_id))
        return dict(row) if row else None

    def period_key(self, account_id: str, game_id: str) -> str:
        """The account's current reward period. It moves on after every claimed result, so a
        new period opens only once the last one is known to be used up."""
        state = self.account_state(account_id, game_id)
        return f"g{int(state['Generation']) if state else 0}"

    def record_claim_result(self, account_id: str, game_id: str, outcome: str, evidence: str,
                            claimed_at: float | None, next_eligible_at: float | None) -> None:
        now = time.time()
        with self._tx() as c:
            c.execute(
                """INSERT INTO AccountClaimState(AccountId, GameIdentifier, Generation, LastVerifiedClaimAt,
                       NextEligibleAt, LastOutcome, Evidence, UpdatedAt)
                   VALUES(?,?,1,?,?,?,?,?)
                   ON CONFLICT(AccountId, GameIdentifier) DO UPDATE SET
                       Generation=Generation+1,
                       LastVerifiedClaimAt=COALESCE(excluded.LastVerifiedClaimAt, LastVerifiedClaimAt),
                       NextEligibleAt=excluded.NextEligibleAt,
                       LastOutcome=excluded.LastOutcome, Evidence=excluded.Evidence,
                       UpdatedAt=excluded.UpdatedAt""",
                (account_id, game_id, claimed_at, next_eligible_at, outcome, evidence[:4000], now),
            )

    def record_outcome_only(self, account_id: str, game_id: str, outcome: str, evidence: str) -> None:
        """A result that does not use up the period (failed, review): keeps the generation."""
        now = time.time()
        with self._tx() as c:
            c.execute(
                """INSERT INTO AccountClaimState(AccountId, GameIdentifier, LastOutcome, Evidence, UpdatedAt)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(AccountId, GameIdentifier) DO UPDATE SET
                       LastOutcome=excluded.LastOutcome, Evidence=excluded.Evidence, UpdatedAt=excluded.UpdatedAt""",
                (account_id, game_id, outcome, evidence[:4000], now),
            )

    def account_states(self, game_id: str) -> dict[str, dict]:
        return {r["AccountId"]: dict(r) for r in self._all(
            "SELECT * FROM AccountClaimState WHERE GameIdentifier=?", (game_id,))}

    def accounts_with_terminal_task_since(self, game_id: str, since: float, trigger: str) -> set[str]:
        rows = self._all(
            """SELECT DISTINCT t.AccountId FROM ClaimTasks t JOIN ClaimSessions s ON s.SessionId=t.SessionId
               WHERE t.GameIdentifier=? AND s.Trigger=? AND t.CreatedAt>=?""",
            (game_id, trigger, since),
        )
        return {r["AccountId"] for r in rows}

    # schedule

    def get_schedule(self) -> dict:
        row = self._one("SELECT * FROM ScheduleSettings WHERE ScheduleId=1")
        return dict(row) if row else {}

    def save_schedule(self, **fields) -> None:
        allowed = {"Enabled", "LocalStartTime", "TimeZoneId", "AccountGroup", "FollowCooldown",
                   "LastExecutionDate", "LastSessionAt"}
        items = [(k, v) for k, v in fields.items() if k in allowed]
        if not items:
            return
        with self._tx() as c:
            c.execute(
                f"UPDATE ScheduleSettings SET {', '.join(f'{k}=?' for k, _ in items)} WHERE ScheduleId=1",
                [v for _, v in items],
            )


_DEFAULT_STORE: ClaimStore | None = None
_DEFAULT_LOCK = threading.Lock()


def default_store() -> ClaimStore:
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        if _DEFAULT_STORE is None:
            from utils.app_paths import get_data_dir
            _DEFAULT_STORE = ClaimStore(os.path.join(get_data_dir(), "daily_claim.sqlite3"))
        return _DEFAULT_STORE
