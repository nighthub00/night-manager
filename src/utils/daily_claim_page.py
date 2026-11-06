"""
Daily Claim page: start/pause/stop a claim session, watch every account move through it, and
set the limits and the daily schedule. The look comes from the main window's stylesheet
(object names card / primary / ghost / danger / sectionTitle).
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Qt, QTime, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
    QApplication,
    QFileDialog,
)

from features.daily_claim import bridge as bridge_mod
from features.daily_claim import controller as dc
from features.daily_claim.models import CLAIM_METHODS, ClaimSettings, TaskStatus
from features.daily_claim.orchestrator import SessionConflict, suggested_max_clients
from features.daily_claim.scheduler import timezone_choices

MUTED = "#8A94B0"
WARN = "#F5B544"

STATUS_COLORS = {
    TaskStatus.COMPLETED.value: "#4ADE80",
    TaskStatus.ALREADY_CLAIMED.value: "#7DD3FC",
    TaskStatus.FAILED.value: "#F87171",
    TaskStatus.NEEDS_MANUAL_REVIEW.value: WARN,
    TaskStatus.CANCELLED.value: MUTED,
    TaskStatus.RETRY_PENDING.value: "#C4B5FD",
}

STATUS_LABELS = {
    "WaitingForGame": "Waiting for game",
    "DetectingReward": "Checking reward",
    "RetryPending": "Retry pending",
    "AlreadyClaimed": "Already claimed",
    "NeedsManualReview": "Needs review",
    "Completed": "Claimed",
}


class DailyClaimBridge(QObject):
    """Created with the main window, before the page: carries orchestrator events to the GUI."""
    event = Signal(str, object)


def _clock(ts: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""


def _duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class _Metric(QWidget):
    """One cell of the metrics strip: value over caption."""

    def __init__(self, title: str):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(0)
        self.value = QLabel("0")
        self.value.setStyleSheet("font-size: 15px; font-weight: 700; background: transparent;")
        caption = QLabel(title)
        caption.setStyleSheet(f"color: {MUTED}; font-size: 10px; background: transparent;")
        lay.addWidget(self.value)
        lay.addWidget(caption)


class DailyClaimPage(QWidget):
    COLUMNS = ("Account", "State", "Client", "Attempts", "Started", "Ended", "Last error")

    def __init__(self, controller: dc.DailyClaimController, bridge: DailyClaimBridge,
                 selected_usernames, group_names):
        super().__init__()
        self.controller = controller
        self._selected = selected_usernames
        self._group_names = group_names
        self._dirty = True
        self._shown_session: int | None = None
        self._shown_simulation: bool | None = None
        bridge.event.connect(self._on_event)
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        QTimer.singleShot(0, self.refresh)

    # layout

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 4, 10, 10)
        outer.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self._title = QLabel("Anime Dice · Daily Rewards")
        self._title.setObjectName("sectionTitle")
        bar.addWidget(self._title)
        self._mode = QLabel()
        bar.addWidget(self._mode)
        bar.addStretch(1)
        self._source = QComboBox()
        self._source.setToolTip("Which accounts the next Start runs")
        self._source.setMinimumWidth(170)
        bar.addWidget(self._source)

        def button(text, name, slot, tip=""):
            b = QPushButton(text)
            b.setObjectName(name)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            bar.addWidget(b)
            return b

        self._start_btn = button("Start", "primary", self._on_start, "Start a claim session")
        self._pause_btn = button("Pause", "ghost", self._on_pause,
                                 "Stop launching new clients; running ones finish")
        self._stop_btn = button("Stop", "danger", self._on_stop,
                                "Cancel queued accounts and close this session's clients")
        self._retry_btn = button("Retry Failed", "ghost", self._on_retry,
                                 "Queue failed and review accounts again")
        button("Settings", "ghost", self._on_settings)
        button("View Logs", "ghost", self._on_logs, "Attempts and evidence for the selected account")
        outer.addLayout(bar)

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet(
            f"color: {WARN}; background: rgba(245, 181, 68, 0.08); border: 1px solid rgba(245, 181, 68, 0.35);"
            " border-radius: 8px; padding: 6px 10px; font-size: 11px;")
        outer.addWidget(self._banner)

        strip = QFrame()
        strip.setObjectName("card")
        strip_lay = QHBoxLayout(strip)
        strip_lay.setContentsMargins(4, 2, 4, 2)
        strip_lay.setSpacing(0)
        self._metrics: dict[str, _Metric] = {}
        for i, (key, title) in enumerate((
            ("total", "Accounts"), ("success", "Claimed"), ("already_claimed", "Already claimed"),
            ("failed", "Failed"), ("review", "Needs review"), ("processing", "Processing"),
            ("remaining", "Remaining"), ("clients", "Clients"), ("duration", "Duration"),
        )):
            if i:
                rule = QFrame()
                rule.setFixedWidth(1)
                rule.setStyleSheet("background: rgba(138, 148, 179, 0.18); border: 0;")
                strip_lay.addWidget(rule)
            metric = _Metric(title)
            self._metrics[key] = metric
            strip_lay.addWidget(metric, 1)
        outer.addWidget(strip)

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(False)
        self._table.setShowGrid(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(self.COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self._table.doubleClicked.connect(lambda _i: self._on_logs())
        outer.addWidget(self._table, 1)

        self._footer = QLabel()
        self._footer.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        outer.addWidget(self._footer)
        self._fill_sources()

    def _fill_sources(self) -> None:
        current = self._source.currentData()
        self._source.blockSignals(True)
        self._source.clear()
        self._source.addItem("All accounts", ("all", ""))
        self._source.addItem("Selected on Accounts page", ("selected", ""))
        for name in self._group_names():
            self._source.addItem(f"Group: {name}", ("group", name))
        index = self._source.findData(current) if current else 0
        self._source.setCurrentIndex(max(0, index))
        self._source.blockSignals(False)

    # data

    def _on_event(self, event: str, data) -> None:
        self._dirty = True
        if event in ("finished", "started"):
            self.refresh()

    def _tick(self) -> None:
        if self.isVisible() and (self._dirty or self.controller.busy):
            self.refresh()

    def refresh(self) -> None:
        self._dirty = False
        controller = self.controller
        settings = controller.settings
        orchestrator = controller.orchestrator
        simulation = orchestrator.settings.simulation if orchestrator else settings.simulation
        store = controller.store(simulation)
        if orchestrator and orchestrator.session_id is not None:
            progress = orchestrator.progress()
            session_id = orchestrator.session_id
        else:
            recent = store.recent_sessions(1)
            session_id = recent[0]["SessionId"] if recent else None
            progress = None

        method = CLAIM_METHODS.get(settings.claim_method, settings.claim_method)
        self._mode.setText("  SIMULATION  " if simulation else f"  LIVE · {method.upper()}  ")
        self._mode.setStyleSheet(
            f"color: {'#C4B5FD' if simulation else '#4ADE80'}; font-weight: 700; font-size: 11px;"
            f" border: 1px solid; border-radius: 6px; padding: 1px 4px;")

        note = controller.claim_method_note()
        if settings.simulation:
            banner = ("Simulation mode: nothing is launched. Accounts go through a made-up game so "
                      "the queue, retries and recovery can be watched safely.")
        elif note:
            banner = f"{note} Live runs and the schedule are held until this is fixed."
        elif settings.claim_method == "screen":
            banner = ("On-screen clicks: each client is shrunk to a 320×240 tile. Every click briefly "
                      "takes the mouse and focus (one window at a time), so leave the PC alone while it runs.")
        else:
            peers = len(controller.bridge.connected()) if controller.bridge else 0
            banner = (f"Executor script: bridge on port {settings.bridge_port}, {peers} script(s) "
                      "connected. The script must be in your executor's auto-execute folder "
                      "(Settings > Install script).")
        self._banner.setText(banner)
        self._banner.setVisible(bool(banner))

        tasks = store.task_views(session_id) if session_id else []
        if progress is None:
            progress = self._progress_from(tasks, store.get_session(session_id) if session_id else None)
        for key, metric in self._metrics.items():
            if key == "clients":
                metric.value.setText(f"{progress.get('active_clients', 0)}/{progress.get('max_clients', settings.max_clients)}")
            elif key == "duration":
                metric.value.setText(_duration(progress.get("duration", 0)))
            else:
                metric.value.setText(str(progress.get(key, 0)))

        running = controller.busy
        paused = bool(orchestrator and orchestrator.paused)
        self._start_btn.setEnabled(not running)
        self._pause_btn.setEnabled(running)
        self._pause_btn.setText("Resume" if paused else "Pause")
        self._stop_btn.setEnabled(running)
        self._retry_btn.setEnabled(bool(orchestrator) and progress.get("failed", 0) + progress.get("review", 0) > 0)

        self._fill_table(tasks)
        status = progress.get("status") or "no session yet"
        throttle = progress.get("throttle")
        bits = [f"Session {session_id}: {status}" if session_id else "No session yet"]
        if throttle:
            bits.append(f"launches held ({throttle})")
        if controller.scheduler:
            bits.append(controller.scheduler.next_run_text())
        self._footer.setText("   ·   ".join(bits))

    @staticmethod
    def _progress_from(tasks, session) -> dict:
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t.status] = counts.get(t.status, 0) + 1
        terminal = sum(counts.get(s, 0) for s in (
            "Completed", "AlreadyClaimed", "Failed", "Cancelled", "NeedsManualReview"))
        started = session["StartedAt"] if session else None
        finished = session["FinishedAt"] if session else None
        return {
            "status": session["Status"] if session else "",
            "total": len(tasks), "success": counts.get("Completed", 0),
            "already_claimed": counts.get("AlreadyClaimed", 0), "failed": counts.get("Failed", 0),
            "review": counts.get("NeedsManualReview", 0), "processing": 0,
            "remaining": len(tasks) - terminal, "active_clients": 0,
            "duration": ((finished or time.time()) - started) if started else 0,
        }

    def _fill_table(self, tasks) -> None:
        selected = self._selected_task_id()
        self._table.setRowCount(len(tasks))
        for row, t in enumerate(tasks):
            values = (
                t.username, STATUS_LABELS.get(t.status, t.status), t.client_ref,
                str(t.attempt_count), _clock(t.started_at), _clock(t.completed_at), t.last_error,
            )
            for col, value in enumerate(values):
                item = self._table.item(row, col)
                if item is None:
                    item = QTableWidgetItem()
                    self._table.setItem(row, col, item)
                if item.text() != value:
                    item.setText(value)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, t.task_id)
                if col == 1:
                    item.setForeground(Qt.GlobalColor.white)
                    color = STATUS_COLORS.get(t.status)
                    if color:
                        from PySide6.QtGui import QColor
                        item.setForeground(QColor(color))
                if col == len(values) - 1:
                    item.setToolTip(value)
            if selected is not None and t.task_id == selected:
                self._table.selectRow(row)

    def _selected_task_id(self) -> int | None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows:
            return None
        item = self._table.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    # actions

    def _accounts_for_start(self):
        kind, value = self._source.currentData() or ("all", "")
        manager = self.controller.manager
        if kind == "selected":
            names = self._selected()
            if not names:
                QMessageBox.information(self, "Daily Claim", "Select accounts on the Accounts page first.")
                return None, ""
            return dc.accounts_for(manager, usernames=names), ""
        if kind == "group":
            return dc.accounts_for(manager, group=value), value
        return dc.accounts_for(manager), ""

    def _on_start(self) -> None:
        self._fill_sources()
        accounts, group = self._accounts_for_start()
        if accounts is None:
            return
        if not accounts:
            QMessageBox.information(self, "Daily Claim", "There are no accounts to run.")
            return
        settings = self.controller.settings
        if not settings.simulation:
            note = self.controller.claim_method_note()
            text = f"Launch {len(accounts)} account(s), up to {settings.max_clients} at a time?"
            if note:
                QMessageBox.warning(self, "Daily Claim", note)
                return
            from features.account_actions import get_ui_setting
            if settings.max_clients > 1 and not get_ui_setting("multi_roblox_enabled", False):
                text += ("\n\nMulti Roblox is off, so only one client can run at a time; "
                         "turn it on in Multi Roblox or set Max clients to 1.")
            if QMessageBox.question(self, "Daily Claim", text) != QMessageBox.StandardButton.Yes:
                return
        try:
            self.controller.start(accounts, "manual", group)
        except SessionConflict as exc:
            QMessageBox.warning(self, "Daily Claim", str(exc))
        self.refresh()

    def _on_pause(self) -> None:
        orchestrator = self.controller.orchestrator
        if not orchestrator:
            return
        if orchestrator.paused:
            orchestrator.resume()
        else:
            orchestrator.pause()
        self.refresh()

    def _on_stop(self) -> None:
        orchestrator = self.controller.orchestrator
        if not orchestrator or not self.controller.busy:
            return
        if QMessageBox.question(
            self, "Daily Claim",
            "Stop this session? Queued accounts are cancelled and this session's clients are closed.",
        ) != QMessageBox.StandardButton.Yes:
            return
        threading.Thread(target=orchestrator.stop, daemon=True, name="DailyClaim-stop").start()

    def _on_retry(self) -> None:
        orchestrator = self.controller.orchestrator
        if orchestrator:
            count = orchestrator.retry_failed()
            if not count:
                QMessageBox.information(self, "Daily Claim", "Nothing to retry.")
        self.refresh()

    def _on_logs(self) -> None:
        orchestrator = self.controller.orchestrator
        simulation = orchestrator.settings.simulation if orchestrator else self.controller.settings.simulation
        store = self.controller.store(simulation)
        task_id = self._selected_task_id()
        lines: list[str] = []
        if task_id is not None:
            task = store.get_task(task_id)
            if task:
                lines.append(f"{task['Username']} ({task['AccountId']}) · {task['Status']} · "
                             f"period {task['PeriodKey']}")
                if task["LastError"]:
                    lines.append(f"Last error: {task['LastError']}")
                for a in store.attempts(task_id):
                    lines.append("")
                    lines.append(f"Attempt {a['AttemptNumber']}: {a['Result'] or 'running'}  "
                                 f"{_clock(a['StartedAt'])} - {_clock(a['FinishedAt'])}")
                    if a["ErrorDetails"]:
                        lines.append(f"  error: {a['ErrorDetails']}")
                    for ev in (a["Evidence"] or "").splitlines():
                        lines.append(f"  {ev}")
                state = store.account_state(task["AccountId"], task["GameIdentifier"])
                if state and state.get("NextEligibleAt"):
                    lines.append("")
                    lines.append("Next claim opens " + time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(float(state["NextEligibleAt"]))))
        else:
            lines.append("Recent sessions (select an account row for its attempts):")
            for s in store.recent_sessions(20):
                lines.append(
                    f"#{s['SessionId']}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(s['StartedAt']))}"
                    f"  {s['Trigger']:<8} {s['Status']:<11} accounts {s['TotalAccounts']}, "
                    f"done {s['CompletedCount']}, failed/review {s['FailedCount']}")
        dialog = QDialog(self)
        dialog.setWindowTitle("Daily Claim log")
        dialog.resize(720, 460)
        lay = QVBoxLayout(dialog)
        text = QPlainTextEdit("\n".join(lines))
        text.setReadOnly(True)
        lay.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        lay.addWidget(buttons)
        dialog.exec()

    def _on_settings(self) -> None:
        current = self.controller.settings
        store = self.controller.live_store()
        schedule = store.get_schedule()
        dialog = QDialog(self)
        dialog.setWindowTitle("Daily Claim settings")
        form = QFormLayout(dialog)

        def spin(value, low, high, suffix="", decimals=None):
            box = QDoubleSpinBox() if decimals is not None else QSpinBox()
            if decimals is not None:
                box.setDecimals(decimals)
            box.setRange(low, high)
            box.setValue(value)
            if suffix:
                box.setSuffix(suffix)
            return box

        simulation = QCheckBox("Simulation (launch nothing)")
        simulation.setChecked(current.simulation)
        suggest = suggested_max_clients()
        max_clients = spin(current.max_clients, 1, ClaimSettings.HARD_MAX_CLIENTS)
        max_clients.setToolTip(f"About {suggest} fit in the memory free right now")
        attempts = spin(current.max_attempts, 1, 10)
        retry_delay = spin(current.retry_delay_seconds, 0, 3600, " s", 0)
        retry_delay.setToolTip("Doubles after each failed attempt")
        gap = spin(current.launch_gap_seconds, 0, 300, " s", 1)
        load_timeout = spin(current.game_load_timeout, 10, 1800, " s", 0)
        verify_timeout = spin(current.verification_timeout, 1, 600, " s", 0)
        cpu = spin(current.max_cpu_percent, 10, 100, " %", 0)
        memory = spin(current.max_memory_percent, 10, 100, " %", 0)
        skip = QCheckBox("Skip accounts already seen as claimed for this period")
        skip.setChecked(current.skip_already_claimed)
        recover = QCheckBox("Resume an interrupted session when the app starts")
        recover.setChecked(current.recover_after_restart)

        form.addRow(simulation)
        method = QComboBox()
        for key, label in CLAIM_METHODS.items():
            method.addItem(label, key)
        method.setCurrentIndex(max(0, method.findData(current.claim_method)))
        popup = spin(current.popup_timeout, 20, 900, " s", 0)
        popup.setToolTip("How long to wait for the Offline Money popup and the Daily Rewards calendar")
        port = spin(current.bridge_port, 1024, 65535)
        folder = QLineEdit(current.autoexec_dir or next(iter(bridge_mod.guess_autoexec_folders()), ""))
        folder.setPlaceholderText(r"e.g. %LOCALAPPDATA%\Potassium\autoexec")
        browse = QPushButton("…")
        browse.setObjectName("ghost")
        browse.clicked.connect(lambda: folder.setText(
            QFileDialog.getExistingDirectory(dialog, "Executor auto-execute folder", folder.text())
            or folder.text()))
        folder_row = QHBoxLayout()
        folder_row.addWidget(folder, 1)
        folder_row.addWidget(browse)
        install = QPushButton("Install script")
        install.setObjectName("ghost")
        copy = QPushButton("Copy script")
        copy.setObjectName("ghost")
        script_row = QHBoxLayout()
        script_row.addWidget(install)
        script_row.addWidget(copy)
        script_row.addStretch(1)

        def _pending() -> ClaimSettings:
            return ClaimSettings.from_dict({**current.to_dict(), "bridge_port": port.value(),
                                            "autoexec_dir": folder.text().strip(),
                                            "claim_method": method.currentData()})

        def _install():
            self.controller.settings = _pending()
            try:
                path = self.controller.install_script()
                QMessageBox.information(dialog, "Daily Claim", f"Installed:\n{path}\n\nSave these settings, "
                                        "then rejoin Anime Dice (or re-attach the executor) for it to start.")
            except Exception as exc:
                QMessageBox.warning(dialog, "Daily Claim", str(exc))
            finally:
                self.controller.settings = current

        def _copy():
            self.controller.settings = _pending()
            QApplication.clipboard().setText(self.controller.script_text())
            self.controller.settings = current

        install.clicked.connect(_install)
        copy.clicked.connect(_copy)

        def _toggle_method():
            is_exec = method.currentData() == "executor"
            for widget in (port, folder, browse, install, copy):
                widget.setEnabled(is_exec)
            popup.setEnabled(not is_exec)

        method.currentIndexChanged.connect(lambda _i: _toggle_method())
        form.addRow("Claim method", method)
        form.addRow("Popup wait (on-screen)", popup)
        form.addRow("Bridge port (executor)", port)
        form.addRow("Auto-execute folder", folder_row)
        form.addRow("", script_row)
        _toggle_method()
        form.addRow(f"Max clients (≈{suggest} fit now)", max_clients)
        form.addRow("Attempts per account", attempts)
        form.addRow("Retry delay", retry_delay)
        form.addRow("Gap between launches", gap)
        form.addRow("Game load timeout", load_timeout)
        form.addRow("Verification timeout", verify_timeout)
        form.addRow("Hold launches above CPU", cpu)
        form.addRow("Hold launches above memory", memory)
        form.addRow(skip)
        form.addRow(recover)

        sched_on = QCheckBox("Run automatically every day (live mode)")
        sched_on.setChecked(bool(schedule.get("Enabled")))
        start_time = QTimeEdit()
        start_time.setDisplayFormat("HH:mm")
        hh, mm = (schedule.get("LocalStartTime") or "09:00").split(":")
        start_time.setTime(QTime(int(hh), int(mm)))
        tz = QComboBox()
        tz.addItems(timezone_choices())
        tz.setCurrentText(schedule.get("TimeZoneId") or "Local")
        group = QComboBox()
        group.addItem("All accounts", "")
        for name in self._group_names():
            group.addItem(name, name)
        group.setCurrentIndex(max(0, group.findData(schedule.get("AccountGroup") or "")))
        follow = QCheckBox("Also run accounts as their 23 h cooldown ends")
        follow.setChecked(bool(schedule.get("FollowCooldown", 1)))
        form.addRow(QLabel(""))
        form.addRow(sched_on)
        form.addRow("Start time", start_time)
        form.addRow("Time zone", tz)
        form.addRow("Accounts", group)
        form.addRow(follow)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        new = ClaimSettings(
            max_clients=max_clients.value(), max_attempts=attempts.value(),
            retry_delay_seconds=retry_delay.value(), launch_gap_seconds=gap.value(),
            game_load_timeout=load_timeout.value(), verification_timeout=verify_timeout.value(),
            skip_already_claimed=skip.isChecked(), recover_after_restart=recover.isChecked(),
            max_cpu_percent=cpu.value(), max_memory_percent=memory.value(),
            simulation=simulation.isChecked(), claim_method=method.currentData(),
            popup_timeout=popup.value(), bridge_port=port.value(), autoexec_dir=folder.text().strip(),
        ).normalized()
        dc.save_settings(new)
        self.controller.apply_settings(new)
        store.save_schedule(
            Enabled=1 if sched_on.isChecked() else 0,
            LocalStartTime=start_time.time().toString("HH:mm"),
            TimeZoneId=tz.currentText(), AccountGroup=group.currentData() or "",
            FollowCooldown=1 if follow.isChecked() else 0,
        )
        if self.controller.busy:
            QMessageBox.information(self, "Daily Claim", "Saved. The running session keeps its settings; "
                                                         "the next one uses these.")
        self.refresh()
