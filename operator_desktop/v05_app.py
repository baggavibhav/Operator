from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QSystemTrayIcon, QTextEdit, QVBoxLayout

from .app import DesktopOperator, configure_logging
from .state import AgentState


class TaskHistoryDialog(QDialog):
    """Small read-only view over the durable task journal."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Operator Task History")
        self.resize(620, 520)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        layout = QVBoxLayout(self)
        title = QLabel("Recent tasks")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        layout.addWidget(title)
        self.body = QTextEdit()
        self.body.setReadOnly(True)
        layout.addWidget(self.body, 1)

    def set_tasks(self, tasks: list[dict[str, Any]]) -> None:
        if not tasks:
            self.body.setPlainText("No task history yet.")
            return
        blocks: list[str] = []
        for task in tasks:
            stamp = task.get("updated_at")
            when = ""
            if isinstance(stamp, (int, float)):
                when = datetime.fromtimestamp(stamp).astimezone().strftime("%Y-%m-%d %H:%M")
            goal = " ".join(str(task.get("goal") or "").split())
            status = str(task.get("status") or "unknown").replace("_", " ")
            error = str(task.get("error") or "").strip()
            lines = [f"{when} · {status}" if when else status, goal]
            if error:
                lines.append("Note: " + error)
            blocks.append("\n".join(lines))
        self.body.setPlainText("\n\n".join(blocks))


class V05DesktopOperator(DesktopOperator):
    """V0.5 desktop shell: persistent runtime UX without changing agent authority."""

    def __init__(self, app: QApplication):
        super().__init__(app)
        self.history = TaskHistoryDialog(self.chat)
        menu = self.tray.contextMenu()
        if menu is not None:
            history_action = QAction("Task History", menu)
            history_action.triggered.connect(self.show_history)
            before = next((action for action in menu.actions() if action.text() == "Open Settings"), None)
            if before is not None:
                menu.insertAction(before, history_action)
            else:
                menu.addAction(history_action)

    def show_history(self) -> None:
        self.history.set_tasks(self.worker.recent_tasks(20))
        self.history.show()
        self.history.raise_()
        self.history.activateWindow()

    def _notify_if_background(self, title: str, message: str,
                              icon: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.MessageIcon.Information) -> None:
        if self.chat.isVisible() or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        compact = " ".join(str(message).split())
        if len(compact) > 180:
            compact = compact[:177] + "…"
        self.tray.showMessage(title, compact, icon, 5000)

    def _on_done(self, results: list[dict[str, Any]]) -> None:
        message = "Task completed."
        if results and isinstance(results[-1], dict):
            result = results[-1]
            if result.get("answer"):
                message = str(result["answer"])
            elif result.get("count") is not None:
                message = f"Task completed · {result.get('count')} item(s)."
        super()._on_done(results)
        self._notify_if_background("Operator finished", message)

    def _on_clarification(self, question: str) -> None:
        super()._on_clarification(question)
        self._notify_if_background("Operator needs input", question)

    def _on_error(self, error: str) -> None:
        super()._on_error(error)
        self._notify_if_background("Operator task failed", error, QSystemTrayIcon.MessageIcon.Warning)


def main() -> int:
    configure_logging()
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication(sys.argv)
    app.setApplicationName("UNNAMED Operator")
    app.setQuitOnLastWindowClosed(False)
    controller = V05DesktopOperator(app)
    controller.start()
    return app.exec()
