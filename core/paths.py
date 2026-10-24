"""Application paths are independent of the launch directory."""

import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USER_ROOT = Path(
    os.environ.get("INVITER_HOME")
    or (
        Path(os.environ.get("LOCALAPPDATA", Path.home())) / "InviterStudio"
        if getattr(sys, "frozen", False)
        else PROJECT_ROOT
    )
).resolve()
DATA_DIR = USER_ROOT / "data"
SESSIONS_DIR = USER_ROOT / "sessions"


def session_name(phone: str, name: str | None = None) -> str:
    candidate = (name or f"session_{phone.lstrip('+')}").strip()
    if candidate.endswith(".session"):
        candidate = candidate[:-8]
    if not candidate.startswith("session_"):
        candidate = f"session_{candidate}"
    if not re.fullmatch(r"session_[A-Za-z0-9_-]+", candidate):
        raise ValueError("Недопустимое имя сессии: используйте буквы, цифры, _ или -")
    return candidate


def session_path(phone: str, name: str | None = None) -> Path:
    path = (SESSIONS_DIR / session_name(phone, name)).resolve()
    if path.parent != SESSIONS_DIR.resolve():
        raise ValueError("Путь сессии находится вне каталога sessions")
    return path
