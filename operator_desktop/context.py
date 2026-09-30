from __future__ import annotations

import ctypes
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any


def _clean_paths(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            try:
                result.append(str(Path(value).expanduser().resolve(strict=False)))
            except OSError:
                continue
    return result


def _windows_foreground() -> tuple[int, str, str]:
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = int(user32.GetForegroundWindow())
        length = int(user32.GetWindowTextLengthW(hwnd))
        title_buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process_name = ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if handle:
            try:
                size = ctypes.c_ulong(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    process_name = Path(buffer.value).name
            finally:
                kernel32.CloseHandle(handle)
        return hwnd, title_buffer.value, process_name
    except Exception:
        return 0, "", ""


def _windows_explorer(hwnd: int) -> dict[str, Any]:
    if not hwnd:
        return {}
    script = r'''
$ErrorActionPreference = 'SilentlyContinue'
$targetHwnd = [int64]$env:OPERATOR_FOREGROUND_HWND
$shell = New-Object -ComObject Shell.Application
$window = @($shell.Windows()) | Where-Object { [int64]$_.HWND -eq $targetHwnd } | Select-Object -First 1
if ($null -eq $window) { exit 0 }
$current = $null
$selected = @()
try { $current = $window.Document.Folder.Self.Path } catch {}
try { $selected = @($window.Document.SelectedItems() | ForEach-Object { $_.Path }) } catch {}
@{ current_folder = $current; selected_files = $selected } | ConvertTo-Json -Compress -Depth 3
'''
    env = os.environ.copy()
    env["OPERATOR_FOREGROUND_HWND"] = str(hwnd)
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=2.5, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = completed.stdout.strip()
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _capture_windows() -> dict[str, Any]:
    hwnd, title, process = _windows_foreground()
    explorer = _windows_explorer(hwnd)
    current = explorer.get("current_folder")
    current_folder = ""
    if isinstance(current, str) and current.strip():
        try:
            current_folder = str(Path(current).expanduser().resolve(strict=False))
        except OSError:
            current_folder = ""
    return {"platform": "Windows", "active_app": process, "window_title": title,
            "current_folder": current_folder, "selected_files": _clean_paths(explorer.get("selected_files"))}


def _capture_macos() -> dict[str, Any]:
    script = r'''
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
end tell
set currentFolder to ""
set selectedPaths to {}
if frontApp is "Finder" then
    tell application "Finder"
        try
            set currentFolder to POSIX path of (target of front window as alias)
        end try
        try
            repeat with anItem in selection
                set end of selectedPaths to POSIX path of (anItem as alias)
            end repeat
        end try
    end tell
end if
return frontApp & linefeed & currentFolder & linefeed & (selectedPaths as string)
'''
    try:
        completed = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=2.5)
        lines = completed.stdout.splitlines()
        active = lines[0].strip() if lines else ""
        current = lines[1].strip() if len(lines) > 1 else ""
        selected = [part.strip() for part in (lines[2].split(",") if len(lines) > 2 else []) if part.strip()]
        return {"platform": "macOS", "active_app": active, "window_title": "",
                "current_folder": str(Path(current).resolve(strict=False)) if current else "",
                "selected_files": _clean_paths(selected)}
    except Exception:
        return {"platform": "macOS", "active_app": "", "window_title": "", "current_folder": "", "selected_files": []}


def capture_desktop_context() -> dict[str, Any]:
    system = platform.system()
    if system == "Windows":
        return _capture_windows()
    if system == "Darwin":
        return _capture_macos()
    return {"platform": system or "Unknown", "active_app": "", "window_title": "", "current_folder": "", "selected_files": []}
