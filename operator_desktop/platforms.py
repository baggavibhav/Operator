from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlatformProfile:
    name: str
    default_hotkey: str
    display_hotkey: str
    config_dir: Path


def current_platform() -> PlatformProfile:
    system = platform.system()
    home = Path.home()
    if system == "Windows":
        return PlatformProfile(
            name="Windows",
            default_hotkey="<ctrl>+<alt>+<space>",
            display_hotkey="Ctrl + Alt + Space",
            config_dir=home / ".unnamed_operator",
        )
    if system == "Darwin":
        return PlatformProfile(
            name="macOS",
            default_hotkey="<cmd>+<shift>+<space>",
            display_hotkey="⌘ + Shift + Space",
            config_dir=home / ".unnamed_operator",
        )
    return PlatformProfile(
        name=system or "Unknown",
        default_hotkey="<ctrl>+<alt>+<space>",
        display_hotkey="Ctrl + Alt + Space",
        config_dir=home / ".unnamed_operator",
    )
