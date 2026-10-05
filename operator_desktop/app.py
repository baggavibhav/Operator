from __future__ import annotations

import logging
import math
import os
import random
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QTimer, Qt, Signal, QObject, QUrl
from PySide6.QtGui import QAction, QColor, QCursor, QDesktopServices, QIcon, QPainter, QPen, QPixmap, QRadialGradient
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


def configure_logging():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")])


def _make_tray_icon():
    pixmap=QPixmap(64,64); pixmap.fill(Qt.GlobalColor.transparent); p=QPainter(pixmap); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(25,25,28)); p.setPen(QPen(QColor(230,230,235),3)); p.drawEllipse(7,7,50,50); p.setBrush(QColor(240,240,245)); p.setPen(Qt.PenStyle.NoPen); p.drawEllipse(23,27,5,7); p.drawEllipse(36,27,5,7); p.end(); return QIcon(pixmap)


class Bridge(QObject):
    state=Signal(str); plan=Signal(object); done=Signal(object); error=Signal(str); cancelled=Signal(str); clarification=Signal(str); task=Signal(object); approval=Signal(str,object,object); toggle_requested=Signal()


class Companion(QWidget):
    clicked=Signal(); context_requested=Signal(object)
    def __init__(self):
        super().__init__(); self.setFixedSize(116,104); self.setWindowFlags(Qt.WindowType.FramelessWindowHint|Qt.WindowType.WindowStaysOnTopHint|Qt.WindowType.Tool); self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground,True); self.setMouseTracking(True); self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._state=AgentState.IDLE; self._phase=0.; self._hover=0.; self._hover_target=0.; self._press=0.; self._press_target=0.; self._blink=0.; self._blink_progress=-1.; self._next_blink=random.uniform(2.6,5.0); self._drag_start=None; self._window_start=None; self._moved=False
        self._timer=QTimer(self); self._timer.timeout.connect(self._tick); self._timer.start(40)
    def show_top_center(self):
        screen=QApplication.primaryScreen()
        if screen: area=screen.availableGeometry(); self.move(area.left()+(area.width()-self.width())//2,area.top()+2)
        self.show()
    def set_state(self,state): self._state=state; self.update()
    def enterEvent(self,event): self._hover_target=1.; super().enterEvent(event)
    def leaveEvent(self,event): self._hover_target=0.; super().leaveEvent(event)
    def _tick(self):
        dt=.04; self._phase=(self._phase+.065)%(math.pi*200); self._hover+=(self._hover_target-self._hover)*.18; self._press+=(self._press_target-self._press)*.28; self._next_blink-=dt
        if self._blink_progress>=0:
            self._blink_progress+=dt/.18; self._blink=math.sin(min(1.,self._blink_progress)*math.pi)
            if self._blink_progress>=1: self._blink_progress=-1.; self._blink=0.; self._next_blink=random.uniform(2.8,5.8)
        elif self._next_blink<=0: self._blink_progress=0.
        self.update()
    def _eye_offset(self):
        # Track the cursor softly within a generous desktop radius, not only on hover.
        local=self.mapFromGlobal(QCursor.pos()); vx=local.x()-self.width()/2; vy=local.y()-48.; dist=math.hypot(vx,vy)
        if dist<520:
            strength=max(0.,min(1.,(520-dist)/360)); return 3.4*max(-1,min(1,vx/180))*strength,2.5*max(-1,min(1,vy/150))*strength
        if self._state==AgentState.THINKING:return 3*math.sin(self._phase*.55),-2.2+.6*math.cos(self._phase*.8)
        if self._state==AgentState.WORKING:return 2.2*math.sin(self._phase*1.15),.7
        if self._state in {AgentState.WAITING_FOR_INPUT,AgentState.NEEDS_APPROVAL}:return 0.,-1.7
        if self._state==AgentState.ERROR:return -1.8,1.2
        return 1.35*math.sin(self._phase*.23),.65*math.sin(self._phase*.17)
    def _edge_color(self):
        if self._state==AgentState.ERROR:return QColor(231,137,137)
        if self._state==AgentState.DONE:return QColor(163,224,181)
        if self._state in {AgentState.NEEDS_APPROVAL,AgentState.WAITING_FOR_INPUT}:return QColor(239,203,116)
        if self._state in {AgentState.THINKING,AgentState.WORKING}:return QColor(203,208,224)
        return QColor(226,227,233)
    def _attention_hand(self,p,cx,y,rx):
        # Rounded emoji-inspired mitten: palm + thumb + four soft fingers. Only attention states use it.
        wave=math.sin(self._phase*2.2)*2.0; x=cx+rx+9; hy=y-2+wave
        p.setPen(QPen(QColor(238,218,174),1.2)); p.setBrush(QColor(225,194,132)); p.drawRoundedRect(QRectF(x,hy,12,15),5,5)
        for i,h in enumerate((9,11,10,8)):
            p.drawRoundedRect(QRectF(x+1+i*2.7,hy-h+2,2.8,h),1.4,1.4)
        p.drawEllipse(QRectF(x-3,hy+5,7,6))
    def paintEvent(self,event):
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing); cx=self.width()/2; breathe=math.sin(self._phase); y=48+1.25*breathe
        if self._state==AgentState.DONE:y-=3*abs(math.sin(self._phase*1.7))
        elif self._state==AgentState.ERROR:cx+=1.8*math.sin(self._phase*4)
        scale=1+.035*self._hover; rx=(28.5+.65*breathe+1.8*self._press)*scale; ry=(27-.45*breathe-2.2*self._press)*scale
        if self._state==AgentState.THINKING:rx+=1.1*math.sin(self._phase*1.35); ry-=.8*math.sin(self._phase*1.35)
        elif self._state==AgentState.LISTENING:rx+=1.4; ry+=.7
        edge=self._edge_color(); p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(0,0,0,34+int(12*self._hover))); p.drawEllipse(QRectF(cx-rx*.72,y+ry-1.5,rx*1.44,7))
        if self._state in {AgentState.THINKING,AgentState.WORKING,AgentState.NEEDS_APPROVAL,AgentState.WAITING_FOR_INPUT}:
            halo=QColor(edge); halo.setAlpha(125 if self._state in {AgentState.NEEDS_APPROVAL,AgentState.WAITING_FOR_INPUT} else 82); p.setBrush(Qt.BrushStyle.NoBrush); p.setPen(QPen(halo,2,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap)); ring=QRectF(cx-rx-6.5,y-ry-6.5,(rx+6.5)*2,(ry+6.5)*2); p.drawArc(ring,int((-self._phase*70)*16),92*16)
        g=QRadialGradient(QPointF(cx-9,y-11),max(rx,ry)*1.55); g.setColorAt(0,QColor(48+int(5*self._hover),49+int(5*self._hover),57+int(6*self._hover))); g.setColorAt(.55,QColor(30,31,37)); g.setColorAt(1,QColor(19,20,24)); p.setBrush(g); p.setPen(QPen(edge,2+.25*self._hover)); p.drawEllipse(QRectF(cx-rx,y-ry,rx*2,ry*2))
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(255,255,255,14+int(10*self._hover))); p.drawEllipse(QRectF(cx-rx*.48,y-ry*.63,rx*.5,ry*.23))
        dx,dy=self._eye_offset(); ey=y-4+dy; gap=10.3; ew=5.2; eh=max(1.15,7.8*(1-self._blink))
        if self._state==AgentState.DONE:eh=min(eh,3.4)
        elif self._state in {AgentState.NEEDS_APPROVAL,AgentState.WAITING_FOR_INPUT}:eh=max(eh,8.6); ew=5.8
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(246,246,249)); p.drawEllipse(QRectF(cx-gap-ew/2+dx,ey-eh/2,ew,eh)); p.drawEllipse(QRectF(cx+gap-ew/2+dx,ey-eh/2,ew,eh))
        # No arms during idle/thinking/working/done. A hand exists only when Operator needs the user.
        if self._state in {AgentState.NEEDS_APPROVAL,AgentState.WAITING_FOR_INPUT}: self._attention_hand(p,cx,y,rx)
    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:self._press_target=1.; self._drag_start=event.globalPosition().toPoint(); self._window_start=self.pos(); self._moved=False
    def mouseMoveEvent(self,event):
        if self._drag_start is not None:
            d=event.globalPosition().toPoint()-self._drag_start; self._moved=self._moved or d.manhattanLength()>5; self.move(self._window_start+d)
    def mouseReleaseEvent(self,event):
        self._press_target=0.
        if event.button()==Qt.MouseButton.LeftButton:
            if not self._moved:self.clicked.emit()
            self._drag_start=None; self._window_start=None
        elif event.button()==Qt.MouseButton.RightButton:self.context_requested.emit(event.globalPosition().toPoint())


class DesktopOperator(QObject):
    def __init__(self,app):
        super().__init__(); self.app=app; self.platform=current_platform(); self.settings=load_settings(); self.bridge=Bridge(); self.chat=IslandWindow(self.platform.display_hotkey); self.orb=Companion(); self.state=AgentState.IDLE; self._details_requested=False; self._latest_result=""; self._latest_error=""
        cb=TaskCallbacks(on_plan=lambda p:self.bridge.plan.emit(p),on_state=lambda v:self.bridge.state.emit(v),on_done=lambda r:self.bridge.done.emit(r),on_clarification=lambda q:self.bridge.clarification.emit(q),on_task=lambda t:self.bridge.task.emit(t),on_error=lambda e:self.bridge.error.emit(e),on_cancelled=lambda m:self.bridge.cancelled.emit(m),request_approval=self._request_approval_from_worker); self.worker=OperatorWorker(self.settings,cb,context_provider=capture_desktop_context)
        self.bridge.state.connect(self._on_state); self.bridge.plan.connect(self._on_plan); self.bridge.done.connect(self._on_done); self.bridge.clarification.connect(self._on_clarification); self.bridge.task.connect(self._on_task); self.bridge.error.connect(self._on_error); self.bridge.cancelled.connect(self._on_cancelled); self.bridge.approval.connect(self._show_approval); self.bridge.toggle_requested.connect(self.toggle_chat); self.chat.submitted.connect(self.submit); self.chat.stop_requested.connect(self.stop_current_task); self.orb.clicked.connect(self.toggle_chat); self.orb.context_requested.connect(self._show_orb_menu)
        self.tray=QSystemTrayIcon(_make_tray_icon(),self.app); self.tray.setToolTip("UNNAMED Operator"); menu=QMenu(); a=QAction("Open Operator",menu); a.triggered.connect(self.show_chat); menu.addAction(a); self.details_action=QAction("Show details",menu); self.details_action.triggered.connect(self.show_details); menu.addAction(self.details_action); self.stop_action=QAction("Stop current task",menu); self.stop_action.triggered.connect(self.stop_current_task); menu.addAction(self.stop_action); s=QAction("Open Settings",menu); s.triggered.connect(self.open_settings); menu.addAction(s); menu.addSeparator(); q=QAction("Quit",menu); q.triggered.connect(self.quit); menu.addAction(q); self.tray.setContextMenu(menu); self.tray.activated.connect(self._tray_activated); self.hotkey=GlobalHotkey(lambda:self.bridge.toggle_requested.emit())
    def start(self):
        self.tray.show(); self.orb.show_top_center(); self.hotkey.start(); snap=self.worker.current_task(); self.chat.set_task(snap); self._sync_stop_action(snap)
        if snap and snap.get("status")=="waiting_for_input" and snap.get("pending_question"):self.set_state(AgentState.WAITING_FOR_INPUT); self.chat.append_operator("Resuming pending task. "+snap["pending_question"]); self.show_chat()
    def submit(self,request):
        self._details_requested=False; self._latest_result=""; self._latest_error=""; self.set_state(AgentState.LISTENING)
        if self.worker.submit(request):QTimer.singleShot(220,self.chat.hide)
        else:self.set_state(AgentState.IDLE); self.chat.append_operator("I'm already working on another task.")
    def stop_current_task(self):
        busy=self.worker.busy; snap=self.worker.stop_current();
        if snap is None:return
        if busy:self._latest_result="Stopping at the next safe boundary…"
        self._sync_stop_action(snap)
    def _on_state(self,value):
        try:self.set_state(AgentState(value))
        except ValueError:self.set_state(AgentState.ERROR)
    def _on_plan(self,plan):
        logging.getLogger(__name__).info("Agent plan: %s",format_plan(plan))
        if self._details_requested:self.chat.append_operator("Plan\n"+format_plan(plan))
    def _on_task(self,task):self.chat.set_task(task);self._sync_stop_action(task)
    def _sync_stop_action(self,task):self.stop_action.setEnabled((str(task.get("status","")) in _ACTIVE_TASK_STATES if isinstance(task,dict) else False) or self.worker.busy)
    def _on_clarification(self,q):self.set_state(AgentState.WAITING_FOR_INPUT);self.chat.append_operator(q);self._notify("Operator needs you",q);self.show_chat()
    def _on_done(self,results):
        self.set_state(AgentState.DONE); self._latest_result=format_result(results[-1]) if results else "Done. No actions were required."; self.chat.append_operator(self._latest_result); self._sync_stop_action(None); self._notify("Operator finished",self._notification_preview(self._latest_result)); QTimer.singleShot(3500,lambda:self.set_state(AgentState.IDLE))
    def _on_cancelled(self,m):self.set_state(AgentState.IDLE);self._sync_stop_action(None);self._latest_result=m;self._notify("Operator stopped",m)
    def _on_error(self,e):self.set_state(AgentState.ERROR);self._sync_stop_action(None);self._latest_error=e;self.chat.append_operator("Error: "+e);self._notify("Operator needs attention",self._notification_preview(e));QTimer.singleShot(5000,lambda:self.set_state(AgentState.IDLE))
    def _notification_preview(self,t):
        c=" ".join(str(t).split());return c if len(c)<=180 else c[:177]+"…"
    def _notify(self,t,m):
        if QSystemTrayIcon.isSystemTrayAvailable():self.tray.showMessage(t,m,QSystemTrayIcon.MessageIcon.Information,5000)
    def set_state(self,s):self.state=s;self.chat.set_state(s);self.orb.set_state(s)
    def _request_approval_from_worker(self,tool,args):event=threading.Event();ans={"approved":False};self.bridge.approval.emit(tool,args,(event,ans));event.wait();return ans["approved"]
    def _show_approval(self,tool,args,token):
        event,ans=token;self.set_state(AgentState.NEEDS_APPROVAL);self._notify("Operator needs approval",f"Approve {tool}?");self.show_chat();details="\n".join(f"{k}: {v}" for k,v in args.items());r=QMessageBox.question(self.chat,"Approve action",f"Operator wants to run a write action:\n\n{tool}\n\n{details}\n\nApprove?",QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No);ans["approved"]=r==QMessageBox.StandardButton.Yes;event.set()
    def _tray_activated(self,r):
        if r in {QSystemTrayIcon.ActivationReason.Trigger,QSystemTrayIcon.ActivationReason.DoubleClick}:self.toggle_chat()
    def _show_orb_menu(self,pos):
        m=QMenu(self.orb);o=m.addAction("Open");d=m.addAction("Show details");st=m.addAction("Stop current task");st.setEnabled(self.worker.busy or self.worker.current_task() is not None);se=m.addAction("Settings");m.addSeparator();q=m.addAction("Quit");x=m.exec(pos)
        if x==o:self.show_chat()
        elif x==d:self.show_details()
        elif x==st:self.stop_current_task()
        elif x==se:self.open_settings()
        elif x==q:self.quit()
    def show_details(self):
        self._details_requested=True;self.show_chat();s=self.worker.current_task()
        if s and s.get("plan"):self.chat.append_operator("Details\n"+format_plan(s["plan"]))
        elif self._latest_error:self.chat.append_operator("Error: "+self._latest_error)
        elif self._latest_result:self.chat.append_operator(self._latest_result)
    def open_settings(self):QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(CONFIG_PATH))))
    def toggle_chat(self):
        if self.chat.isVisible():self.chat.hide()
        else:self.show_chat()
    def show_chat(self):
        # Island grows from the companion's side. Prefer right; flip left near screen edge.
        screen=self.orb.screen() or QApplication.primaryScreen()
        if screen:
            area=screen.availableGeometry(); overlap=18; right_x=self.orb.x()+self.orb.width()-overlap; left_x=self.orb.x()-self.chat.width()+overlap
            x=right_x if right_x+self.chat.width()<=area.right()-8 else left_x
            x=max(area.left()+8,min(x,area.right()-self.chat.width()-8)); orb_center_y=self.orb.y()+self.orb.height()//2; y=orb_center_y-self.chat.height()//2; y=max(area.top()+8,min(y,area.bottom()-self.chat.height()-8)); self.chat.move(x,y)
        self.chat.show();self.chat.raise_();self.orb.raise_();self.chat.activateWindow();self.chat.input.setFocus()
    def quit(self):self.hotkey.stop();self.tray.hide();self.worker.close();self.app.quit()


def main():
    configure_logging();os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING","1");app=QApplication(sys.argv);app.setApplicationName("UNNAMED Operator");app.setQuitOnLastWindowClosed(False);c=DesktopOperator(app);c.start();return app.exec()
