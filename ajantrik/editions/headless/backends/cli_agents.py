"""Coding-agent CLIs as backends: Claude Code (`claude -p`) and Codex (`codex exec`).

Both run non-interactively, stream JSON events we log, and report a session id so a builder can
be resumed with the next round's findings instead of re-reading the whole repo.

Claude Code runs in the main checkout, so the repo's .claude/settings.json hooks (the factory
guard) vet every tool call, with FACTORY_GUARD=strict forcing the factory tier. Codex has no tool
hooks: its sandbox is the boundary (workspace-write: the worktree and the job folder only, no
network), which `factory validate` points out. With the factory's sandbox on (sandbox.apply.codex),
the whole codex process also runs inside srt, with the OpenAI hosts allowed so it can reach its model.
On Linux Codex's own sandbox (Landlock, seccomp) stacks inside it. On macOS a Seatbelt profile cannot
be applied inside another, so there srt is the boundary and Codex runs with danger-full-access.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ajantrik.core import sandbox as sbx

from .base import AgentResult, AgentRun, agent_env

CODEX_HOSTS = ["api.openai.com", "chatgpt.com", "*.chatgpt.com", "auth.openai.com"]


async def _stream(argv: list[str], cwd: Path, env: dict[str, str], log_path: Path | None, timeout: float,
                  stdin: str | None = None) -> tuple[int, list[dict[str, Any]], str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
    events: list[dict[str, Any]] = []
    log = log_path.open("a") if log_path else None

    async def pump() -> None:
        assert proc.stdout is not None
        if stdin is not None and proc.stdin is not None:
            proc.stdin.write(stdin.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            if log:
                log.write(line + "\n")
                log.flush()
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                events.append(ev)

    try:
        await asyncio.wait_for(pump(), timeout=timeout)
        rc = await proc.wait()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, events, f"timed out after {timeout:.0f}s"
    finally:
        if log:
            log.close()
    stderr = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
    return rc, events, stderr


class ClaudeCliBackend:
    name = "claude-cli"
    supports_resume = True

    def __init__(self, binary: str = "claude", permission_mode: str = "acceptEdits",
                 extra_args: list[str] | None = None):
        self.binary = binary
        self.permission_mode = permission_mode
        self.extra_args = extra_args or []

    def argv(self, run: AgentRun) -> list[str]:
        argv = [self.binary, "-p", run.prompt, "--output-format", "stream-json", "--verbose",
                "--append-system-prompt", run.system, "--permission-mode", self.permission_mode,
                "--allowedTools", ",".join(run.tools), "--add-dir", str(run.worktree)]
        if run.model:
            argv += ["--model", run.model]
        if run.session_id:
            argv += ["--resume", run.session_id]
        return argv + self.extra_args

    async def run(self, run: AgentRun) -> AgentResult:
        if not shutil.which(self.binary):
            return AgentResult(False, error=f"{self.binary} not found on PATH")
        env = agent_env(dict(os.environ), keep={"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                                                "CLAUDE_CODE_USE_BEDROCK", "AWS_PROFILE", "AWS_REGION"})
        env.update({"FACTORY_GUARD": "strict", "CLAUDE_PROJECT_DIR": str(run.root)})
        rc, events, stderr = await _stream(self.argv(run), run.root, env, run.log_path, run.timeout_s)
        result: dict[str, Any] = next((e for e in reversed(events) if e.get("type") == "result"), {})
        session = result.get("session_id") or next((e.get("session_id") for e in events if e.get("session_id")), None)
        ok = rc == 0 and not result.get("is_error", False)
        usage: dict[str, Any] = {k: result[k] for k in ("total_cost_usd", "num_turns", "duration_ms") if k in result}
        return AgentResult(ok, text=str(result.get("result", "")), session_id=session,
                           error="" if ok else (stderr.strip()[-500:] or str(result.get("result", ""))[-500:]),
                           usage=usage)


class CodexCliBackend:
    name = "codex-cli"
    supports_resume = True

    def __init__(self, binary: str = "codex", sandbox: str = "workspace-write", extra_args: list[str] | None = None,
                 os_sandbox: dict[str, Any] | None = None):
        self.binary = binary
        self.os_sandbox = os_sandbox if sbx.enabled(os_sandbox) else None
        # Seatbelt does not nest: inside srt on macOS, srt is the boundary.
        self.sandbox = "danger-full-access" if self.os_sandbox and sbx.platform() == "darwin" else sandbox
        self.extra_args = extra_args or []

    def hosts(self) -> list[str]:
        base = os.environ.get("OPENAI_BASE_URL", "")
        host = urlparse(base).hostname if base else None
        return [*CODEX_HOSTS, *([host] if host else [])]

    def argv(self, run: AgentRun) -> list[str]:
        # Codex has no system-prompt flag: the agent's instructions lead the first message. A resumed
        # session already has them.
        common = ["--json", "--skip-git-repo-check", "--sandbox", self.sandbox, "-C", str(run.worktree),
                  "--add-dir", str(run.job_dir)]
        if run.model:
            common += ["--model", run.model]
        if run.session_id:
            return [self.binary, "exec", *common, *self.extra_args, "resume", run.session_id, "-"]
        return [self.binary, "exec", *common, *self.extra_args, "-"]

    def stdin(self, run: AgentRun) -> str:
        return run.prompt if run.session_id else f"{run.system}\n\n---\n\n{run.prompt}"

    async def run(self, run: AgentRun) -> AgentResult:
        if not shutil.which(self.binary):
            return AgentResult(False, error=f"{self.binary} not found on PATH")
        env = agent_env(dict(os.environ), keep={"OPENAI_API_KEY", "CODEX_HOME", "OPENAI_BASE_URL"})
        env["FACTORY_GUARD"] = "strict"
        try:
            # codex keeps its sessions and login in CODEX_HOME (~/.codex): it must be able to write there.
            home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
            argv = sbx.wrap(self.argv(run), self.os_sandbox, sbx.settings_path(run.root, f"{run.job_dir.name}-{run.name}"),
                            [*sbx.writable_for(run.worktree, run.job_dir), home], self.hosts())
        except sbx.SandboxUnavailable as err:
            return AgentResult(False, error=str(err))
        rc, events, stderr = await _stream(argv, run.worktree, env, run.log_path, run.timeout_s,
                                           stdin=self.stdin(run))
        session = None
        text = ""
        for e in events:
            session = session or e.get("thread_id") or e.get("session_id") or (e.get("msg") or {}).get("session_id")
            item = e.get("item") or {}
            if item.get("type") in ("agent_message", "assistant_message") and item.get("text"):
                text = item["text"]
            msg = e.get("msg") or {}
            if msg.get("type") == "agent_message" and msg.get("message"):
                text = msg["message"]
        failed = any(e.get("type") in ("turn.failed", "error") for e in events)
        ok = rc == 0 and not failed
        return AgentResult(ok, text=text, session_id=session, error="" if ok else stderr.strip()[-500:])
