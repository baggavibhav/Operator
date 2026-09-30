from __future__ import annotations

import logging
import math
import os
import sys
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, QRect, QTimer, Qt, Signal, QObject, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from local_operator.config import APP_DIR, CONFIG_PATH, load_settings
from .formatting import format_plan, format_result
from .hotkeys import GlobalHotkey
from .platforms import current_platform
from .state import AgentState, presentation_for
from .worker import OperatorWorker, TaskCallbacks


LOG_PATH = APP_DIR / "desktop.log"


def configure_logging() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )


def _make_tray_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(25, 25, 28))
    painter.setPen(QPen(QColor(230, 230, 235), 3))
    painter.drawEllipse(7, 7, 50, 50)
    painter.end()
    return QIcon(pixmap)


class Bridge(QObject):
    state = Signal(str)
    plan = Signal(object)
    done = Signal(object)
    error = Signal(str)
    clarification = Signal(str)
    task = Signal(object)
    approval = Signal(str, object, object)
    toggle_requested = Signal()


class ChatWindow(QDialog):
    submitted = Signal(str)

    def __init__(self, display_hotkey: str):
        super().__init__()
        self.setWindowTitle("UNNAMED Operator")
        self.setMinimumSize(440, 500)
        self.resize(500, 550)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        layout = QVBoxLayout(self)
        header = QLabel("UNNAMED Operator")
        header.setFont(QFont("Segoe UI", 15, QFont.Weight.DemiBold))
        layout.addWidget(header)

        self.status = QLabel(f"Ready · {display_hotkey}")
        self.status.setStyleSheet("color: #777;")
        layout.addWidget(self.status)

        self.task_status = QLabel("No active task")
        self.task_status.setWordWrap(True)
        self.task_status.setStyleSheet("color: #8b8b94; font-size: 11px;")
        layout.addWidget(self.task_status)

        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText("Your local operator is ready.")
        layout.addWidget(self.transcript, 1)

        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Tell your computer what to do…")
        self.input.returnPressed.connect(self._submit)
        row.addWidget(self.input, 1)
        self.send = QPushButton("Send")
        self.send.clicked.connect(self._submit)
        row.addWidget(self.send)
        layout.addLayout(row)

    def _submit(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.append_user(text)
        self.input.clear()
        self.submitted.emit(text)

    def append_user(self, text: str) -> None:
        self.transcript.append(f"<b>You</b><br>{self._escape(text)}<br>")

    def append_operator(self, text: str) -> None:
        self.transcript.append(f"<b>Operator</b><br>{self._escape(text).replace(chr(10), '<br>')}<br>")

    @staticmethod
    def _escape(text: str) -> str:
        return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def set_state(self, state: AgentState) -> None:
        p = presentation_for(state)
        self.status.setText(f"{p.symbol} {p.label}")
        busy = state in {AgentState.THINKING, AgentState.WORKING, AgentState.NEEDS_APPROVAL}
        self.send.setEnabled(not busy)
        self.input.setEnabled(not busy)

    def set_task(self, task: dict[str, Any] | None) -> None:
        if not task:
            self.task_status.setText("No active task")
            return
        goal = " ".join(str(task.get("goal", "")).split())
        if len(goal) > 90:
            goal = goal[:87] + "…"
        status = str(task.get("status", "unknown")).replace("_", " ")
        pending = task.get("pending_field")
        suffix = f" · waiting for {pending}" if pending else ""
        self.task_status.setText(f"Task: {goal}\nState: {status}{suffix}")


class FloatingOrb(QWidget):
    clicked = Signal()
    context_requested = Signal(object)

    def __init__(self):
        super().__init__()
        self.setFixedSize(72, 72)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._state = AgentState.IDLE
        self._phase = 0.0
        self._drag_start: QPoint | None = None
        self._window_start: QPoint | None = None
        self._moved = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)

    def show_near_corner(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            self.show()
            return
        area: QRect = screen.availableGeometry()
        self.move(area.right() - self.width() - 24, area.bottom() - self.height() - 24)
        self.show()

    def set_state(self, state: AgentState) -> None:
        self._state = state
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.14) % (math.pi * 2)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pulse = 3 + (math.sin(self._phase) + 1) * 2
        center = self.rect().center()
        radius = 22

        if self._state in {AgentState.THINKING, AgentState.WORKING}:
            painter.setPen(QPen(QColor(90, 90, 100, 120), pulse))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, radius + 7, radius + 7)

        painter.setPen(QPen(QColor(235, 235, 240), 2))
        painter.setBrush(QColor(24, 24, 28))
        painter.drawEllipse(center, radius, radius)

        symbol = presentation_for(self._state).symbol
        painter.setPen(QColor(245, 245, 248))
        painter.setFont(QFont("Segoe UI Symbol", 18, QFont.Weight.DemiBold))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, symbol)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.globalPosition().toPoint()
            self._window_start = self.pos()
            self._moved = False

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_start is not None and self._window_start is not None:
            delta = event.globalPosition().toPoint() - self._drag_start
            if delta.manhattanLength() > 5:
                self._moved = True
            self.move(self._window_start + delta)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._moved:
                self.clicked.emit()
            self._drag_start = None
            self._window_start = None
        elif event.button() == Qt.MouseButton.RightButton:
            self.context_requested.emit(event.globalPosition().toPoint())


class DesktopOperator(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.platform = current_platform()
        self.settings = load_settings()
        self.bridge = Bridge()
        self.chat = ChatWindow(self.platform.display_hotkey)
        self.orb = FloatingOrb()
        self.state = AgentState.IDLE

        callbacks = TaskCallbacks(
            on_plan=lambda plan: self.bridge.plan.emit(plan),
            on_state=lambda value: self.bridge.state.emit(value),
            on_done=lambda results: self.bridge.done.emit(results),
            on_clarification=lambda question: self.bridge.clarification.emit(question),
            on_task=lambda task: self.bridge.task.emit(task),
            on_error=lambda error: self.bridge.error.emit(error),
            request_approval=self._request_approval_from_worker,
        )
        self.worker = OperatorWorker(self.settings, callbacks)

        self.bridge.state.connect(self._on_state)
        self.bridge.plan.connect(self._on_plan)
        self.bridge.done.connect(self._on_done)
        self.bridge.clarification.connect(self._on_clarification)
        self.bridge.task.connect(self._on_task)
        self.bridge.error.connect(self._on_error)
        self.bridge.approval.connect(self._show_approval)
        self.bridge.toggle_requested.connect(self.toggle_chat)
        self.chat.submitted.connect(self.submit)
        self.orb.clicked.connect(self.toggle_chat)
        self.orb.context_requested.connect(self._show_orb_menu)

        self.tray = QSystemTrayIcon(_make_tray_icon(), self.app)
        self.tray.setToolTip("UNNAMED Operator")
        menu = QMenu()
        open_action = QAction("Open Operator", menu)
        open_action.triggered.connect(self.show_chat)
        menu.addAction(open_action)
        settings_action = QAction("Open Settings", menu)
        settings_action.triggered.connect(self.open_settings)
        menu.addAction(settings_action)
        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.quit)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)

        self.hotkey = GlobalHotkey(lambda: self.bridge.toggle_requested.emit())

    def start(self) -> None:
        self.tray.show()
        self.orb.show_near_corner()
        self.hotkey.start()
        snapshot = self.worker.current_task()
        self.chat.set_task(snapshot)
        if snapshot and snapshot.get("status") == "waiting_for_input" and snapshot.get("pending_question"):
            self.set_state(AgentState.WAITING_FOR_INPUT)
            self.chat.append_operator(f"Resuming pending task. {snapshot['pending_question']}")
        elif snapshot and snapshot.get("status") == "interrupted":
            self.chat.append_operator("A previous task was interrupted. It was not resumed automatically because write actions may have partially completed.")
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.chat.append_operator("System tray is not available on this desktop session.")

    def submit(self, request: str) -> None:
        if not self.worker.submit(request):
            self.chat.append_operator("I'm already working on another task.")

    def _on_state(self, value: str) -> None:
        try:
            state = AgentState(value)
        except ValueError:
            state = AgentState.ERROR
        self.set_state(state)

    def _on_plan(self, plan: dict[str, Any]) -> None:
        self.chat.append_operator("Plan:\n" + format_plan(plan))

    def _on_task(self, task: dict[str, Any]) -> None:
        self.chat.set_task(task)

    def _on_clarification(self, question: str) -> None:
        self.set_state(AgentState.WAITING_FOR_INPUT)
        self.chat.append_operator(question)

    def _on_done(self, results: list[dict[str, Any]]) -> None:
        self.set_state(AgentState.DONE)
        if results:
            self.chat.append_operator(format_result(results[-1]))
        else:
            self.chat.append_operator("Done. No actions were required.")
        QTimer.singleShot(1800, lambda: self.set_state(AgentState.IDLE))

    def _on_error(self, error: str) -> None:
        self.set_state(AgentState.ERROR)
        self.chat.append_operator(f"Error: {error}")
        QTimer.singleShot(2500, lambda: self.set_state(AgentState.IDLE))

    def set_state(self, state: AgentState) -> None:
        self.state = state
        self.chat.set_state(state)
        self.orb.set_state(state)

    def _request_approval_from_worker(self, tool: str, args: dict[str, Any]) -> bool:
        event = threading.Event()
        answer: dict[str, bool] = {"approved": False}
        self.bridge.approval.emit(tool, args, (event, answer))
        event.wait()
        return answer["approved"]

    def _show_approval(self, tool: str, args: dict[str, Any], token: object) -> None:
        event, answer = token
        self.set_state(AgentState.NEEDS_APPROVAL)
        details = "\n".join(f"{key}: {value}" for key, value in args.items())
        response = QMessageBox.question(
            self.chat,
            "Approve action",
            f"Operator wants to run a write action:\n\n{tool}\n\n{details}\n\nApprove?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        answer["approved"] = response == QMessageBox.StandardButton.Yes
        event.set()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick}:
            self.toggle_chat()

    def _show_orb_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self.orb)
        open_action = menu.addAction("Open")
        settings_action = menu.addAction("Settings")
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        selected = menu.exec(global_pos)
        if selected == open_action:
            self.show_chat()
        elif selected == settings_action:
            self.open_settings()
        elif selected == quit_action:
            self.quit()

    def open_settings(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(CONFIG_PATH))))

    def toggle_chat(self) -> None:
        if self.chat.isVisible():
            self.chat.hide()
        else:
            self.show_chat()

    def show_chat(self) -> None:
        self.chat.show()
        self.chat.raise_()
        self.chat.activateWindow()
        self.chat.input.setFocus()

    def quit(self) -> None:
        self.hotkey.stop()
        self.tray.hide()
        self.worker.close()
        self.app.quit()


def main() -> int:
    configure_logging()
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication(sys.argv)
    app.setApplicationName("UNNAMED Operator")
    app.setQuitOnLastWindowClosed(False)
    controller = DesktopOperator(app)
    controller.start()
    return app.exec()
