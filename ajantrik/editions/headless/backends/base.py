"""What every headless backend implements: run one agent, once, on one prompt.

A backend gets the agent's full instructions (its markdown body plus its preloaded skills), the
prompt `step` produced, the model, the tool allowlist and where it may work. It returns when the
agent stops. It never decides what happens next; the engine reads the files the agent wrote.
"""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol


@dataclass
class AgentRun:
    name: str                       # unique per job: builder-web-004-..., used for sessions and logs
    agent: str                      # agent file name: builder-web
    system: str                     # agent body + preloaded skills + factory framing
    prompt: str                     # first message (or the resume message when session_id is set)
    model: str
    tools: list[str]
    root: Path                      # the main checkout (guard rules are relative to it)
    worktree: Path
    job_dir: Path
    session_id: str | None = None   # resume this backend session instead of starting fresh
    log_path: Path | None = None
    timeout_s: float = 3600


@dataclass
class AgentResult:
    ok: bool
    text: str = ""
    session_id: str | None = None
    error: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


class Backend(Protocol):
    name: str
    supports_resume: bool

    async def run(self, run: AgentRun) -> AgentResult: ...


def load_guard(root: Path) -> ModuleType:
    """The repo's rendered guard: the same rule set the Claude Code hooks use."""
    path = root / ".factory" / "security" / "guard.py"
    spec = importlib.util.spec_from_file_location(f"factory_guard_{abs(hash(str(root)))}", path)
    if not spec or not spec.loader:
        raise FileNotFoundError(f"{path} missing; run `factory render`")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SECRETISH = re.compile(r"(?i)(PASSWORD|PASSWD|SECRET|TOKEN|PRIVATE|CREDENTIAL|API_?KEY|ACCESS_?KEY|AUTH|SESSION)")
KEEP_ENV = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TMPDIR", "TZ", "PWD",
            "VIRTUAL_ENV", "NVM_DIR", "NODE_PATH", "PYTHONPATH", "XDG_RUNTIME_DIR", "DISPLAY"}


def agent_env(base: dict[str, str], keep: set[str] | None = None) -> dict[str, str]:
    """The environment an agent's commands see: credentials stripped (CLIs that need their own
    login, like `claude` and `codex`, get theirs from `keep`)."""
    keep = keep or set()
    return {k: v for k, v in base.items() if k in keep or k in KEEP_ENV or not SECRETISH.search(k)}
