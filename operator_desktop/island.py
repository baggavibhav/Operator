from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
)

from .state import AgentState, presentation_for

_ACTIVE_TASK_STATES = {"new", "waiting_for_input", "ready", "planning", "executing", "verifying"}
_BUSY_STATES = {AgentState.THINKING, AgentState.WORKING, AgentState.NEEDS_APPROVAL}


class IslandWindow(QDialog):
    """Adaptive companion surface.

    Idle is deliberately tiny: the character is the product, not a chat window.
    The island grows only when there is work, conversation, or a result to show.
    """

    submitted = Signal(str)
    stop_requested = Signal()

    COMPACT_SIZE = (460, 126)
    ACTIVE_SIZE = (500, 220)
    EXPANDED_SIZE = (520, 390)

    def __init__(self, display_hotkey: str):
        super().__init__()
        self._display_hotkey = display_hotkey
        self._drag_origin = None
        self._window_origin = None
        self._has_conversation = False
        self._pulse_on = True
        self._state = AgentState.IDLE

        self.setWindowTitle("UNNAMED Operator")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(*self.COMPACT_SIZE)
        self.resize(*self.COMPACT_SIZE)

        root = QVBoxLayout(self)
        # No top gutter: visually tucks the island into the companion instead of
        # leaving the old detached-window gap.
        root.setContentsMargins(8, 0, 8, 8)

        self.card = QFrame()
        self.card.setObjectName("islandCard")
        root.addWidget(self.card)

        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(8)

        # Status is intentionally text-only. The companion itself carries the
        # personality and most state feedback.
        self.status_row = QFrame()
        status_layout = QHBoxLayout(self.status_row)
        status_layout.setContentsMargins(2, 0, 2, 0)
        status_layout.setSpacing(6)
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_dot.setFixedWidth(10)
        self.status = QLabel("ready")
        self.status.setObjectName("stateText")
        status_layout.addWidget(self.status_dot)
        status_layout.addWidget(self.status)
        status_layout.addStretch(1)
        self.stop = QPushButton("stop")
        self.stop.setObjectName("stopButton")
        self.stop.setToolTip("Stop at the next safe boundary")
        self.stop.clicked.connect(self.stop_requested.emit)
        self.stop.setVisible(False)
        status_layout.addWidget(self.stop)
        layout.addWidget(self.status_row)

        self.task_status = QLabel("")
        self.task_status.setObjectName("taskStatus")
        self.task_status.setWordWrap(True)
        self.task_status.setVisible(False)
        layout.addWidget(self.task_status)

        self.transcript = QTextEdit()
        self.transcript.setObjectName("transcript")
        self.transcript.setReadOnly(True)
        self.transcript.setFrameShape(QFrame.Shape.NoFrame)
        self.transcript.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.transcript.setVisible(False)
        layout.addWidget(self.transcript, 1)

        composer = QFrame()
        composer.setObjectName("composer")
        composer_row = QHBoxLayout(composer)
        composer_row.setContentsMargins(13, 7, 7, 7)
        composer_row.setSpacing(7)

        self.input = QLineEdit()
        self.input.setObjectName("promptInput")
        self.input.setPlaceholderText("What can I do?")
        self.input.setFrame(False)
        self.input.returnPressed.connect(self._submit)
        composer_row.addWidget(self.input, 1)

        self.send = QPushButton("↑")
        self.send.setObjectName("sendButton")
        self.send.setFixedSize(32, 32)
        self.send.setToolTip("Send")
        self.send.clicked.connect(self._submit)
        composer_row.addWidget(self.send)
        layout.addWidget(composer)

        self.setStyleSheet(
            """
            QFrame#islandCard {
                background: #15161a;
                border: 1px solid #303239;
                border-radius: 22px;
            }
            QFrame#status_row { background: transparent; border: none; }
            QLabel#statusDot {
                color: #858995;
                background: transparent;
                border: none;
                font-size: 8px;
            }
            QLabel#stateText {
                color: #8c909b;
                background: transparent;
                border: none;
                font-size: 10px;
            }
            QLabel#taskStatus {
                color: #e9eaf0;
                background: transparent;
                border: none;
                font-size: 12px;
                padding: 1px 2px 3px 2px;
            }
            QTextEdit#transcript {
                color: #ececf1;
                background: transparent;
                border: none;
                selection-background-color: #3d465c;
                font-size: 12px;
                padding: 0px 2px;
            }
            QFrame#composer {
                background: #202127;
                border: 1px solid #34363e;
                border-radius: 18px;
            }
            QLineEdit#promptInput {
                color: #f4f4f6;
                background: transparent;
                border: none;
                font-size: 13px;
                padding: 3px 1px;
            }
            QLineEdit#promptInput:disabled { color: #777983; }
            QPushButton#sendButton {
                color: #111216;
                background: #f0f1f4;
                border: none;
                border-radius: 16px;
                font-size: 17px;
                font-weight: 700;
            }
            QPushButton#sendButton:hover { background: #ffffff; }
            QPushButton#sendButton:disabled { background: #4b4e56; color: #90939c; }
            QPushButton#stopButton {
                color: #b7bac4;
                background: transparent;
                border: none;
                padding: 2px 4px;
                font-size: 10px;
            }
            QPushButton#stopButton:hover { color: #ffffff; }
            """
        )

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(130)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        # A restrained pulse makes the surface feel responsive without turning
        # it into an animated dashboard. The character remains the focal point.
        self._pulse = QTimer(self)
        self._pulse.setInterval(520)
        self._pulse.timeout.connect(self._pulse_status)
        self._pulse.start()

        self._apply_mode("compact")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.setWindowOpacity(0.0)
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        QTimer.singleShot(80, self.input.setFocus)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 44:
            self._drag_origin = event.globalPosition().toPoint()
            self._window_origin = self.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_origin is not None and self._window_origin is not None:
            self.move(self._window_origin + event.globalPosition().toPoint() - self._drag_origin)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_origin = None
        self._window_origin = None
        super().mouseReleaseEvent(event)

    def _pulse_status(self) -> None:
        self._pulse_on = not self._pulse_on
        if self._state in _BUSY_STATES:
            self.status_dot.setText("●" if self._pulse_on else "·")
        else:
            self.status_dot.setText("●")

    def _apply_mode(self, mode: str) -> None:
        if mode == "compact":
            self.status_row.setVisible(False)
            self.task_status.setVisible(False)
            self.transcript.setVisible(False)
            self.setMinimumSize(*self.COMPACT_SIZE)
            self.resize(*self.COMPACT_SIZE)
            return

        self.status_row.setVisible(True)
        self.task_status.setVisible(bool(self.task_status.text()))
        if mode == "active" and not self._has_conversation:
            self.transcript.setVisible(False)
            self.setMinimumSize(*self.ACTIVE_SIZE)
            self.resize(*self.ACTIVE_SIZE)
        else:
            self.transcript.setVisible(True)
            self.setMinimumSize(*self.EXPANDED_SIZE)
            self.resize(*self.EXPANDED_SIZE)

    def _submit(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.append_user(text)
        self.input.clear()
        self.submitted.emit(text)

    def append_user(self, text: str) -> None:
        self._has_conversation = True
        self._apply_mode("expanded")
        self.transcript.append(
            '<div style="margin:7px 0 5px 52px; color:#bfc6d8;">'
            '<span style="color:#6f7480; font-size:9px;">YOU</span><br>'
            f'{self._escape(text)}</div>'
        )
        self._scroll_to_bottom()

    def append_operator(self, text: str) -> None:
        self._has_conversation = True
        self._apply_mode("expanded")
        body = self._escape(text).replace(chr(10), "<br>")
        self.transcript.append(
            '<div style="margin:7px 44px 7px 0; color:#f1f1f4;">'
            '<span style="color:#777c88; font-size:9px;">OPERATOR</span><br>'
            f'{body}</div>'
        )
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    @staticmethod
    def _escape(text: str) -> str:
        return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def set_state(self, state: AgentState) -> None:
        self._state = state
        p = presentation_for(state)
        self.status.setText(p.label.casefold())

        color = "#858995"
        if state == AgentState.DONE:
            color = "#91cda0"
        elif state == AgentState.ERROR:
            color = "#dc8f8f"
        elif state == AgentState.NEEDS_APPROVAL:
            color = "#d9b96d"
        elif state in {AgentState.THINKING, AgentState.WORKING, AgentState.LISTENING}:
            color = "#aebde8"
        self.status_dot.setStyleSheet(
            f"color:{color}; background:transparent; border:none; font-size:8px;"
        )

        busy = state in _BUSY_STATES
        self.send.setEnabled(not busy)
        self.input.setEnabled(not busy)
        self.stop.setEnabled(busy)
        self.stop.setVisible(busy)

        if state == AgentState.IDLE and not self._has_conversation:
            self._apply_mode("compact")
        elif self._has_conversation:
            self._apply_mode("expanded")
        else:
            self._apply_mode("active")

        if state == AgentState.WAITING_FOR_INPUT:
            self.input.setEnabled(True)
            self.send.setEnabled(True)
            self.input.setFocus()

    def set_task(self, task: dict[str, Any] | None) -> None:
        if not task:
            self.task_status.clear()
            if self._state == AgentState.IDLE and not self._has_conversation:
                self._apply_mode("compact")
            return

        goal = " ".join(str(task.get("goal", "")).split())
        if len(goal) > 104:
            goal = goal[:101] + "…"
        raw_status = str(task.get("status", "unknown"))
        status = raw_status.replace("_", " ")
        pending = task.get("pending_field")
        suffix = f" · waiting for {pending}" if pending else ""

        context_bits: list[str] = []
        context = task.get("context")
        desktop = context.get("desktop_context") if isinstance(context, dict) else None
        if isinstance(desktop, dict):
            app = str(desktop.get("active_app") or "").strip()
            folder = str(desktop.get("current_folder") or "").strip()
            selected = desktop.get("selected_files")
            if app:
                context_bits.append(app)
            if folder:
                context_bits.append(Path(folder).name or folder)
            if isinstance(selected, list) and selected:
                context_bits.append(f"{len(selected)} selected")

        detail = f"{status}{suffix}"
        if context_bits:
            detail += "  ·  " + " · ".join(context_bits)
        self.task_status.setText(
            f"{self._escape(goal)}<br><span style='color:#6f737e; font-size:9px;'>{self._escape(detail)}</span>"
        )
        self.task_status.setTextFormat(Qt.TextFormat.RichText)
        self.task_status.setVisible(True)

        active = raw_status in _ACTIVE_TASK_STATES
        self.stop.setEnabled(active)
        self.stop.setVisible(active)
        self._apply_mode("expanded" if self._has_conversation else "active")
