from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

APP_DIR = Path.home() / ".unnamed_operator"
CONFIG_PATH = APP_DIR / "config.json"
AUDIT_DB_PATH = APP_DIR / "audit.db"
TASK_DB_PATH = APP_DIR / "tasks.db"

DEFAULT_MODEL = os.getenv("OPERATOR_MODEL", "phi4-mini")
LEGACY_DEFAULT_MODEL = "qwen2.5:1.5b"
DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")


def _default_roots() -> list[str]:
    candidates = [
        Path.home() / "Downloads",
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path.cwd(),
    ]
    seen: set[str] = set()
    roots: list[str] = []
    for candidate in candidates:
        try:
            resolved = str(candidate.expanduser().resolve(strict=False))
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            roots.append(resolved)
    return roots


@dataclass(frozen=True)
class Settings:
    model: str
    ollama_url: str
    allowed_roots: tuple[Path, ...]
    max_read_chars: int = 12000
    max_steps: int = 10
    web_enabled: bool = True


def ensure_config() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        return
    data = {
        "model": DEFAULT_MODEL,
        "ollama_url": DEFAULT_OLLAMA_URL,
        "allowed_roots": _default_roots(),
        "max_read_chars": 12000,
        "max_steps": 10,
        "web_enabled": True,
    }
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _clean_roots(values: Iterable[str]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for value in values:
        path = Path(value).expanduser().resolve(strict=False)
        if path not in roots:
            roots.append(path)
    return tuple(roots)


def _migrate_legacy_development_default(data: dict) -> dict:
    """Move only the old stock development default; preserve custom choices."""
    if os.getenv("OPERATOR_MODEL"):
        return data
    if data.get("model") != LEGACY_DEFAULT_MODEL:
        return data
    migrated = dict(data)
    migrated["model"] = DEFAULT_MODEL
    try:
        CONFIG_PATH.write_text(json.dumps(migrated, indent=2), encoding="utf-8")
    except OSError:
        # Settings can still use the new default for this process if the file is
        # temporarily read-only; do not make startup depend on config mutation.
        pass
    return migrated


def load_settings(model_override: str | None = None) -> Settings:
    ensure_config()
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Operator config must contain a JSON object.")
    data = _migrate_legacy_development_default(data)

    env_roots = os.getenv("OPERATOR_ALLOWED_ROOTS")
    roots_raw = env_roots.split(os.pathsep) if env_roots else data.get("allowed_roots", _default_roots())
    roots = _clean_roots(roots_raw)
    if not roots:
        raise RuntimeError("No allowed roots are configured.")

    return Settings(
        model=model_override or os.getenv("OPERATOR_MODEL") or data.get("model", DEFAULT_MODEL),
        ollama_url=os.getenv("OLLAMA_URL") or data.get("ollama_url", DEFAULT_OLLAMA_URL),
        allowed_roots=roots,
        max_read_chars=int(data.get("max_read_chars", 12000)),
        max_steps=int(data.get("max_steps", 10)),
        web_enabled=bool(data.get("web_enabled", True)),
    )
