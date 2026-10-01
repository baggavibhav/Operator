from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Settings
from .planner import PlannerError, ollama_available, propose_with_ollama


class ModelBackend(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def propose