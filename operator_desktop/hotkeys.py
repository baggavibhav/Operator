from __future__ import annotations

import logging
from collections.abc import Callable

from .platforms import current_platform

log = logging.getLogger(__name__)


class GlobalHotkey:
    """Small wrapper around pynput so hotkey failures never crash the app."""

    def __init__(self, callback: Callable[[], None], hotkey: str | None = None):
        self.callback = callback
        self.hotkey = hotkey or current_platform().default_hotkey
        self._listener = None

    def start(self) -> None:
        try:
            from pynput import keyboard

            self._listener = keyboard.GlobalHotKeys({self.hotkey: self.callback})
            self._listener.start()
            log.info("Registered global hotkey %s", self.hotkey)
        except Exception:
            log.exception("Global hotkey could not be registered")

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                log.exception("Failed to stop hotkey listener")
