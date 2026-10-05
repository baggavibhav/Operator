from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QFont
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


class IslandWindow(QDialog):
    """Compact frameless surface summoned by the companion.

    The companion remains the persistent visual identity; this panel is only the
    workspace it opens when the user wants to talk, inspect progress, or approve
    work. Keep the public surface compatible with the former ChatWindow so the
    agent runtime remains independent from presentation.
    """

    submitted = Signal(str)
    stop_requested = Signal()

    def __init__(self, display_hotkey: str):
        super().__init__()
        self._display_hotkey = display_hotkey
        self._drag_origin = None
        self._window_origin = None

        self.setWindowTitle("UNNAMED Operator")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(470, 310)
        self.resize(560, 410)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)

        self.card = QFrame()
        self.card.setObjectName("islandCard")
        root.addWidget(self.card)

        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.identity = QLabel("operator")
        self.identity.setObjectName("identity")
        self.identity.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        top.addWidget(self.identity)
        top.addStretch(1)
        self.status = QLabel(f"● Ready  ·  {display_hotkey}")
        self.status.setObjectName("statePill")
        top.addWidget(self.status)
        self.collapse = QPushButton("—")
        self.collapse.setObjectName("chromeButton")
        self.collapse.setFixedSize(30, 26)
        self.collapse.setToolTip("Collapse back to the companion")
        self.collapse.clicked.connect(self.hide)
        top.addWidget(self.collapse)
        layout.addLayout(top)

        self.task_status = QLabel("Ask me to do something on this computer.")
        self.task_status.setObjectName("taskStatus")
        self.task_status.setWordWrap(True)
        self.task_status.setMaximumHeight(54)
        layout.addWidget(self.task_status)

        self.transcript = QTextEdit()
        self.transcript.setObjectName("transcript")
        self.transcript.setReadOnly(True)
        self.transcript.setFrameShape(QFrame.Shape.NoFrame)
        self.transcript.setPlaceholderText("Your local operator is ready.")
        self.transcript.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.transcript, 1)

        composer = QFrame()
        composer.setObjectName("composer")
        composer_row = QHBoxLayout(composer)
        composer_row.setContentsMargins(12, 8, 8, 8)
        composer_row.setSpacing(8)

        self.input = QLineEdit()
        self.input.setObjectName("promptInput")
        self.input.setPlaceholderText("What should I do?")
        self.input.setFrame(False)
        self.input.returnPressed.connect(self._submit)
        composer_row.addWidget(self.input, 1)

        self.stop = QPushButton("■")
        self.stop.setObjectName("stopButton")
        self.stop.setFixedSize(34, 34)
        self.stop.setEnabled(False)
        self.stop.setVisible(False)
        self.stop.setToolTip("Stop at the next safe boundary")
        self.stop.clicked.connect(self.stop_requested.emit)
        composer_row.addWidget(self.stop)

        self.send = QPushButton("↑")
        self.send.setObjectName("sendButton")
        self.send.setFixedSize(34, 34)
        self.send.setToolTip("Send")
        self.send.clicked.connect(self._submit)
        composer_row.addWidget(self.send)
        layout.addWidget(composer)

        self.hint = QLabel("Enter to send  ·  click the companion to collapse")
        self.hint.setObjectName("hint")
        layout.addWidget(self.hint)

        self.setStyleSheet(
            """
            QFrame#islandCard {
                background: #15161a;
                border: 1px solid #34363d;
                border-radius: 24px;
            }
            QLabel#identity {
                color: #a9abb5;
                letter-spacing: 1px;
                padding-left: 2px;
            }
            QLabel#statePill {
                color: #c9cbd3;
                background: #202127;
                border: 1px solid #34363d;
                border-radius: 11px;
                padding: 4px 9px;
                font-size: 11px;
            }
            QLabel#taskStatus {
                color: #f1f1f4;
                font-size: 12px;
                padding: 2px 2px 4px 2px;
            }
            QTextEdit#transcript {
                color: #ececf1;
                background: transparent;
                border: none;
                selection-background-color: #3d465c;
                font-size: 13px;
                padding: 2px;
            }
            QFrame#composer {
                background: #202127;
                border: 1px solid #373941;
                border-radius: 18px;
            }
            QLineEdit#promptInput {
                color: #f4f4f6;
                background: transparent;
                border: none;
                font-size: 13px;
                padding: 4px 2px;
            }
            QLineEdit#promptInput:disabled { color: #777983; }
            QPushButton#sendButton {
                color: #111216;
                background: #f0f1f4;
                border: none;
                border-radius: 17px;
                font-size: 18px;
                font-weight: 700;
            }
            QPushButton#sendButton:hover { background: #ffffff; }
            QPushButton#sendButton:disabled { background: #555861; color: #a0a2aa; }
            QPushButton#stopButton {
                color: #ffd6d6;
                background: #4a2528;
                border: 1px solid #744044;
                border-radius: 17px;
                font-size: 11px;
            }
            QPushButton#stopButton:hover { background: #5b2d31; }
            QPushButton#chromeButton {
                color: #9a9ca5;
                background: transparent;
                border: none;
                border-radius: 10px;
                font-size: 15px;
            }
            QPushButton#chromeButton:hover { background: #25262c; color: #ffffff; }
            QLabel#hint {
                color: #686b75;
                font-size: 10px;
                padding-left: 3px;
            }
            """
        )

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(150)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.setWindowOpacity(0.0)
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 58:
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

    def _submit(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.append_user(text)
        self.input.clear()
        self.submitted.emit(text)

    def append_user(self, text: str) -> None:
        self.transcript.append(
            '<div style="margin:8px 0 4px 44px; color:#bfc6d8;">'
            '<span style="color:#7f8492; font-size:10px;">YOU</span><br>'
            f'{self._escape(text)}</div>'
        )
        self._scroll_to_bottom()

    def append_operator(self, text: str) -> None:
        body = self._escape(text).replace(chr(10), "<br>")
        self.transcript.append(
            '<div style="margin:8px 38px 8px 0; color:#f1f1f4;">'
            '<span style="color:#8e93a2; font-size:10px;">OPERATOR</span><br>'
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
        p = presentation_for(state)
        label = p.label
        color = "#c9cbd3"
        if state == AgentState.DONE:
            color = "#9fe0ae"
        elif state == AgentState.ERROR:
            color = "#ef9a9a"
        elif state == AgentState.NEEDS_APPROVAL:
            color = "#f0cd73"
        elif state in {AgentState.THINKING, AgentState.WORKING, AgentState.LISTENING}:
            color = "#b9c8f5"
        self.status.setText(f"● {label}")
        self.status.setStyleSheet(
            f"color:{color}; background:#202127; border:1px solid #34363d; "
            "border-radius:11px; padding:4px 9px; font-size:11px;"
        )
        busy = state in {AgentState.THINKING, AgentState.WORKING, AgentState.NEEDS_APPROVAL}
        self.send.setEnabled(not busy)
        self.input.setEnabled(not busy)
        self.stop.setEnabled(busy)
        self.stop.setVisible(busy)
        if state == AgentState.WAITING_FOR_INPUT:
            self.input.setEnabled(True)
            self.send.setEnabled(True)
            self.input.setFocus()

    def set_task(self, task: dict[str, Any] | None) -> None:
        if not task:
            self.task_status.setText("Ask me to do something on this computer.")
            self.stop.setEnabled(False)
            self.stop.setVisible(False)
            return
        goal = " ".join(str(task.get("goal", "")).split())
        if len(goal) > 118:
            goal = goal[:115] + "…"
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
        self.task_status.setText(f"{goal}<br><span style='color:#777b86; font-size:10px;'>{detail}</span>")
        self.task_status.setTextFormat(Qt.TextFormat.RichText)

        active = raw_status in _ACTIVE_TASK_STATES
        self.stop.setEnabled(active)
        self.stop.setVisible(active)
