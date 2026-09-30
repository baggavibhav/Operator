from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Any

from .config import Settings
from .planner import ollama_available, propose_with_ollama


class ModelBackend(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def propose(self, request: str) -> dict[str, Any]: ...


@dataclass
class OllamaBackend:
    settings: Settings
    name: str = "ollama"

    def available(self) -> tuple[bool, str]:
        return ollama_available(self.settings)

    def propose(self, request: str) -> dict[str, Any]:
        return propose_with_ollama(request, self.settings)
