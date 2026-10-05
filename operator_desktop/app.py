from __future__ import annotations

import logging
import math
import os
import sys
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, QRect, QTimer, Qt, Signal, QObject, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon, QWidget

from local_operator.config import APP_DIR, CONFIG_PATH, load_settings
from .context import capture_desktop_context
from .formatting import format_plan, format_result
from .hotkeys import GlobalHotkey
from .island import IslandWindow
from .platforms import current_platform
from .state import AgentState
from .worker import OperatorWorker, TaskCallbacks

LOG_PATH = APP_DIR / "desktop.log"
_ACTIVE_TASK_STATES = {"new", "waiting_for_input", "ready", "planning", "executing", "verifying"}


def configure_logging() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")])


def _make_tray_icon() -> QIcon:
    pixmap = QPixmap(64, 64); pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(25, 25, 28)); painter.setPen(QPen(QColor(230, 230, 235), 3)); painter.drawEllipse(7, 7, 50, 50)
    painter.setBrush(QColor(240, 240, 245)); painter.setPen(Qt.PenStyle.NoPen); painter.drawEllipse(23, 27, 5, 7); painter.drawEllipse(36, 27, 5, 7)
    painter.end(); return QIcon(pixmap)


class Bridge(QObject):
    state = Signal(str); plan = Signal(object); done = Signal(object); error = Signal(str); cancelled = Signal(str)
    clarification = Signal(str); task = Signal(object); approval = Signal(str, object, object); toggle_requested = Signal()


class Companion(QWidget):
    clicked = Signal(); context_requested = Signal(object)

    def __init__(self):
        super().__init__(); self.setFixedSize(112, 100)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True); self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._state = AgentState.IDLE; self._phase = 0.0; self._blink = 0.0; self._drag_start = None; self._window_start = None; self._moved = False
        self._timer = QTimer(self); self._timer.timeout.connect(self._tick); self._timer.start(40)

    def show_top_center(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None: self.show(); return
        area = screen.availableGeometry(); self.move(area.left() + (area.width() - self.width()) // 2, area.top() + 4); self.show()

    def set_state(self, state: AgentState) -> None: self._state = state; self.update()
    def _tick(self) -> None:
        self._phase = (self._phase + 0.10) % (math.pi * 2); cycle = self._phase % (math.pi * 2)
        self._blink = max(0.0, 1.0 - abs(cycle - 5.65) / 0.16); self.update()
    def _eye_offset(self):
        if self._state == AgentState.THINKING: return 2.5 * math.sin(self._phase * .7), -2.0
        if self._state == AgentState.WORKING: return 2.0 * math.sin(self._phase * 1.4), 1.0
        if self._state == AgentState.WAITING_FOR_INPUT: return 0.0, -1.5
        if self._state == AgentState.ERROR: return -2.0, 1.5
        return 0.0, 0.0

    def paintEvent(self, event) -> None:
        painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing); cx = self.width()/2; y = 49.0 + 1.2*math.sin(self._phase)
        if self._state == AgentState.DONE: y -= 4.0*abs(math.sin(self._phase*1.8))
        elif self._state == AgentState.ERROR: cx += 2.2*math.sin(self._phase*3.3)
        rx=29.0+.8*math.sin(self._phase); ry=27.0-.6*math.sin(self._phase)
        if self._state == AgentState.THINKING: rx += 1.6*math.sin(self._phase*1.7); ry -= 1.2*math.sin(self._phase*1.7)
        elif self._state == AgentState.LISTENING: rx += 2; ry += 1
        if self._state in {AgentState.THINKING,AgentState.WORKING,AgentState.NEEDS_APPROVAL}:
            alpha=65+int(35*(math.sin(self._phase)+1)); ring=QColor(160,170,195,alpha)
            if self._state == AgentState.NEEDS_APPROVAL: ring=QColor(235,190,90,150)
            painter.setPen(QPen(ring,3)); painter.setBrush(Qt.BrushStyle.NoBrush); painter.drawEllipse(int(cx-rx-7),int(y-ry-7),int((rx+7)*2),int((ry+7)*2))
        body=QColor(29,29,34); edge=QColor(225,226,233)
        if self._state == AgentState.ERROR: edge=QColor(230,130,130)
        elif self._state == AgentState.DONE: edge=QColor(155,225,175)
        elif self._state == AgentState.NEEDS_APPROVAL: edge=QColor(240,205,115)
        painter.setPen(QPen(edge,2.2)); painter.setBrush(body); painter.drawEllipse(int(cx-rx),int(y-ry),int(rx*2),int(ry*2))
        dx,dy=self._eye_offset(); eye_y=y-4+dy; gap=10.5; ew=5.5; eh=max(1.3,8.0*(1.0-self._blink))
        if self._state == AgentState.DONE: eh=4.0
        elif self._state == AgentState.NEEDS_APPROVAL: eh=9.0; ew=6.0
        painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(244,244,248))
        painter.drawEllipse(int(cx-gap-ew/2+dx),int(eye_y-eh/2),int(ew),int(eh)); painter.drawEllipse(int(cx+gap-ew/2+dx),int(eye_y-eh/2),int(ew),int(eh))
        if self._state in {AgentState.WORKING,AgentState.NEEDS_APPROVAL,AgentState.DONE}:
            painter.setPen(QPen(edge,3,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap))
            if self._state == AgentState.WORKING:
                wave=3.0*math.sin(self._phase*2); painter.drawLine(int(cx-rx+3),int(y+8),int(cx-rx-12),int(y+15+wave)); painter.drawLine(int(cx+rx-3),int(y+8),int(cx+rx+12),int(y+15-wave))
            elif self._state == AgentState.NEEDS_APPROVAL:
                painter.drawLine(int(cx+rx-4),int(y+6),int(cx+rx+10),int(y-7)); painter.drawEllipse(int(cx+rx+7),int(y-12),5,5)
            else:
                painter.drawLine(int(cx-rx+5),int(y+7),int(cx-rx-8),int(y+1)); painter.drawLine(int(cx+rx-5),int(y+7),int(cx+rx+8),int(y+1))

    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton: self._drag_start=event.globalPosition().toPoint(); self._window_start=self.pos(); self._moved=False
    def mouseMoveEvent(self,event):
        if self._drag_start is not None and self._window_start is not None:
            delta=event.globalPosition().toPoint()-self._drag_start; self._moved=self._moved or delta.manhattanLength()>5; self.move(self._window_start+delta)
    def mouseReleaseEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:
            if not self._moved: self.clicked.emit()
            self._drag_start=None; self._window_start=None
        elif event.button()==Qt.MouseButton.RightButton: self.context_requested.emit(event.globalPosition().toPoint())


class DesktopOperator(QObject):
    def __init__(self, app: QApplication):
        super().__init__(); self.app=app; self.platform=current_platform(); self.settings=load_settings(); self.bridge=Bridge(); self.chat=IslandWindow(self.platform.display_hotkey); self.orb=Companion(); self.state=AgentState.IDLE
        self._details_requested=False; self._latest_result=""; self._latest_error=""
        callbacks=TaskCallbacks(on_plan=lambda p:self.bridge.plan.emit(p),on_state=lambda v:self.bridge.state.emit(v),on_done=lambda r:self.bridge.done.emit(r),on_clarification=lambda q:self.bridge.clarification.emit(q),on_task=lambda t:self.bridge.task.emit(t),on_error=lambda e:self.bridge.error.emit(e),on_cancelled=lambda m:self.bridge.cancelled.emit(m),request_approval=self._request_approval_from_worker)
        self.worker=OperatorWorker(self.settings,callbacks,context_provider=capture_desktop_context)
        self.bridge.state.connect(self._on_state); self.bridge.plan.connect(self._on_plan); self.bridge.done.connect(self._on_done); self.bridge.clarification.connect(self._on_clarification); self.bridge.task.connect(self._on_task); self.bridge.error.connect(self._on_error); self.bridge.cancelled.connect(self._on_cancelled); self.bridge.approval.connect(self._show_approval); self.bridge.toggle_requested.connect(self.toggle_chat)
        self.chat.submitted.connect(self.submit); self.chat.stop_requested.connect(self.stop_current_task); self.orb.clicked.connect(self.toggle_chat); self.orb.context_requested.connect(self._show_orb_menu)
        self.tray=QSystemTrayIcon(_make_tray_icon(),self.app); self.tray.setToolTip("UNNAMED Operator")
        menu=QMenu(); open_action=QAction("Open Operator",menu); open_action.triggered.connect(self.show_chat); menu.addAction(open_action)
        self.details_action=QAction("Show details",menu); self.details_action.triggered.connect(self.show_details); menu.addAction(self.details_action)
        self.stop_action=QAction("Stop current task",menu); self.stop_action.triggered.connect(self.stop_current_task); menu.addAction(self.stop_action)
        settings_action=QAction("Open Settings",menu); settings_action.triggered.connect(self.open_settings); menu.addAction(settings_action); menu.addSeparator()
        quit_action=QAction("Quit",menu); quit_action.triggered.connect(self.quit); menu.addAction(quit_action); self.tray.setContextMenu(menu); self.tray.activated.connect(self._tray_activated)
        self.hotkey=GlobalHotkey(lambda:self.bridge.toggle_requested.emit())

    def start(self):
        self.tray.show(); self.orb.show_top_center(); self.hotkey.start(); snapshot=self.worker.current_task(); self.chat.set_task(snapshot); self._sync_stop_action(snapshot)
        if snapshot and snapshot.get("status")=="waiting_for_input" and snapshot.get("pending_question"):
            self.set_state(AgentState.WAITING_FOR_INPUT); self.chat.append_operator(f"Resuming pending task. {snapshot['pending_question']}"); self.show_chat()
        elif snapshot and snapshot.get("status")=="interrupted": self._latest_error="A previous task was interrupted. It was not resumed automatically."

    def submit(self,request):
        self._details_requested=False; self._latest_result=""; self._latest_error=""; self.set_state(AgentState.LISTENING)
        if self.worker.submit(request):
            # Hand-off: after accepting a task the UI gets out of the user's way.
            QTimer.singleShot(220, self.chat.hide)
        else:
            self.set_state(AgentState.IDLE); self.chat.append_operator("I'm already working on another task.")

    def stop_current_task(self):
        was_busy=self.worker.busy; snapshot=self.worker.stop_current()
        if snapshot is None: return
        if was_busy: self._latest_result="Stopping at the next safe boundary…"
        self._sync_stop_action(snapshot)

    def _on_state(self,value):
        try: state=AgentState(value)
        except ValueError: state=AgentState.ERROR
        self.set_state(state)

    def _on_plan(self,plan):
        # Plans are diagnostic information. Keep them out of the normal UX.
        logging.getLogger(__name__).info("Agent plan: %s", format_plan(plan))
        if self._details_requested: self.chat.append_operator("Plan\n"+format_plan(plan))

    def _on_task(self,task): self.chat.set_task(task); self._sync_stop_action(task)
    def _sync_stop_action(self,task):
        status=str(task.get("status","")) if isinstance(task,dict) else ""; self.stop_action.setEnabled(status in _ACTIVE_TASK_STATES or self.worker.busy)

    def _on_clarification(self,question):
        self.set_state(AgentState.WAITING_FOR_INPUT); self.chat.append_operator(question); self._notify("Operator needs you", question); self.show_chat()

    def _on_done(self,results):
        self.set_state(AgentState.DONE); self._latest_result=format_result(results[-1]) if results else "Done. No actions were required."; self.chat.append_operator(self._latest_result); self._sync_stop_action(None)
        # Never steal focus when background work completes.
        self._notify("Operator finished", self._notification_preview(self._latest_result)); QTimer.singleShot(3500,lambda:self.set_state(AgentState.IDLE))

    def _on_cancelled(self,message):
        self.set_state(AgentState.IDLE); self._sync_stop_action(None); self._latest_result=message; self._notify("Operator stopped", message)

    def _on_error(self,error):
        self.set_state(AgentState.ERROR); self._sync_stop_action(None); self._latest_error=error; self.chat.append_operator(f"Error: {error}"); self._notify("Operator needs attention", self._notification_preview(error)); QTimer.singleShot(5000,lambda:self.set_state(AgentState.IDLE))

    def _notification_preview(self,text):
        clean=" ".join(str(text).split()); return clean if len(clean)<=180 else clean[:177]+"…"
    def _notify(self,title,message):
        if QSystemTrayIcon.isSystemTrayAvailable(): self.tray.showMessage(title,message,QSystemTrayIcon.MessageIcon.Information,5000)

    def set_state(self,state): self.state=state; self.chat.set_state(state); self.orb.set_state(state)
    def _request_approval_from_worker(self,tool,args):
        event=threading.Event(); answer={"approved":False}; self.bridge.approval.emit(tool,args,(event,answer)); event.wait(); return answer["approved"]
    def _show_approval(self,tool,args,token):
        event,answer=token; self.set_state(AgentState.NEEDS_APPROVAL); self._notify("Operator needs approval", f"Approve {tool}?"); self.show_chat(); details="\n".join(f"{k}: {v}" for k,v in args.items())
        response=QMessageBox.question(self.chat,"Approve action",f"Operator wants to run a write action:\n\n{tool}\n\n{details}\n\nApprove?",QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
        answer["approved"]=response==QMessageBox.StandardButton.Yes; event.set()
    def _tray_activated(self,reason):
        if reason in {QSystemTrayIcon.ActivationReason.Trigger,QSystemTrayIcon.ActivationReason.DoubleClick}: self.toggle_chat()
    def _show_orb_menu(self,global_pos):
        menu=QMenu(self.orb); open_action=menu.addAction("Open"); details=menu.addAction("Show details"); stop=menu.addAction("Stop current task"); stop.setEnabled(self.worker.busy or self.worker.current_task() is not None); settings=menu.addAction("Settings"); menu.addSeparator(); quit_action=menu.addAction("Quit")
        selected=menu.exec(global_pos)
        if selected==open_action:self.show_chat()
        elif selected==details:self.show_details()
        elif selected==stop:self.stop_current_task()
        elif selected==settings:self.open_settings()
        elif selected==quit_action:self.quit()
    def show_details(self):
        self._details_requested=True; self.show_chat()
        snapshot=self.worker.current_task()
        if snapshot and snapshot.get("plan"): self.chat.append_operator("Details\n"+format_plan(snapshot["plan"]))
        elif self._latest_error: self.chat.append_operator("Error: "+self._latest_error)
        elif self._latest_result: self.chat.append_operator(self._latest_result)
    def open_settings(self): QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(CONFIG_PATH))))
    def toggle_chat(self):
        if self.chat.isVisible(): self.chat.hide()
        else: self.show_chat()
    def show_chat(self):
        screen=self.orb.screen() or QApplication.primaryScreen()
        if screen is not None:
            area=screen.availableGeometry(); x=self.orb.x()+(self.orb.width()-self.chat.width())//2; x=max(area.left()+8,min(x,area.right()-self.chat.width()-8)); y=self.orb.y()+self.orb.height()-6
            if y+self.chat.height()>area.bottom(): y=max(area.top()+8,self.orb.y()-self.chat.height()+6)
            self.chat.move(x,y)
        self.chat.show(); self.chat.raise_(); self.chat.activateWindow(); self.chat.input.setFocus()
    def quit(self): self.hotkey.stop(); self.tray.hide(); self.worker.close(); self.app.quit()


def main()->int:
    configure_logging(); os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING","1"); app=QApplication(sys.argv); app.setApplicationName("UNNAMED Operator"); app.setQuitOnLastWindowClosed(False); controller=DesktopOperator(app); controller.start(); return app.exec()
