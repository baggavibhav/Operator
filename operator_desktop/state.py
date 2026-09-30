from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AgentState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    WAITING_FOR_INPUT = "waiting_for_input"
    WORKING = "working"
    NEEDS_APPROVAL = "needs_approval"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True)
class StatePresentation:
    label: str
    symbol: str


_PRESENTATION = {
    AgentState.IDLE: StatePresentation("Ready", "●"),
    AgentState.LISTENING: StatePresentation("Listening", "◉"),
    AgentState.THINKING: StatePresentation("Thinking", "◌"),
    AgentState.WAITING_FOR_INPUT: StatePresentation("Needs input", "?"),
    AgentState.WORKING: StatePresentation("Working", "↻"),
    AgentState.NEEDS_APPROVAL: StatePresentation("Needs approval", "!"),
    AgentState.DONE: StatePresentation("Done", "✓"),
    AgentState.ERROR: StatePresentation("Something went wrong", "!"),
}


def presentation_for(state: AgentState) -> StatePresentation:
    return _PRESENTATION[state]
