"""The tools an API-backed agent gets. Same names and arguments as Claude Code's (Read, Grep, Glob,
Edit, Write, Bash), so agent instructions read the same in every runtime, and every call passes
through the repo's guard (strict factory tier) before it runs. With the sandbox on, Bash runs inside
srt, and the file tools keep to the same rules: write only the worktree, the job folder, the temp dir
and `allow_write`, never `deny_write`; never read `deny_read` (unless `allow_read`)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from ajantrik.core import sandbox as sbx

from .base import agent_env, load_guard

SCHEMAS: dict[str, dict[str, Any]] = {
    "Read": {"description": "Read a text file. Returns numbered lines.",
             "parameters": {"type": "object", "properties": {
                 "file_path": {"type": "string", "description": "Absolute path, or relative to the worktree."},
                 "offset": {"type": "integer", "description": "First line (1-based)."},
                 "limit": {"type": "integer", "description": "Number of lines."}},
                 "required": ["file_path"]}},
    "Grep": {"description": "Search file contents with a regular expression. Returns path:line:text.",
             "parameters": {"type": "object", "properties": {
                 "pattern": {"type": "string"}, "path": {"type": "string"},
                 "glob": {"type": "string", "description": "Only files matching this glob, e.g. *.py"}},
                 "required": ["pattern"]}},
    "Glob": {"description": "List files matching a glob pattern, e.g. src/**/*.ts.",
             "parameters": {"type": "object", "properties": {
                 "pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}},
    "Edit": {"description": "Replace an exact string in a file. old_string must occur exactly once unless "
                            "replace_all is true.",
             "parameters": {"type": "object", "properties": {
                 "file_path": {"type": "string"}, "old_string": {"type": "string"},
                 "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}},
                 "required": ["file_path", "old_string", "new_string"]}},
    "Write": {"description": "Create or overwrite a file with the given content.",
              "parameters": {"type": "object", "properties": {
                  "file_path": {"type": "string"}, "content": {"type": "string"}},
                  "required": ["file_path", "content"]}},
    "Bash": {"description": "Run a bash command. Starts in the job worktree; use `cd <dir> && ...` elsewhere.",
             "parameters": {"type": "object", "properties": {
                 "command": {"type": "string"},
                 "timeout": {"type": "integer", "description": "Seconds (default 600)."}},
                 "required": ["command"]}},
}


class ToolError(Exception):
    pass


class Toolbox:
    def __init__(self, root: Path, worktree: Path, agent: str, allowed: list[str], output_limit: int = 20000,
                 command_timeout: int = 600, sandbox: dict[str, Any] | None = None, job_dir: Path | None = None):
        self.root, self.worktree, self.agent = root, worktree, agent
        self.sandbox = sandbox if sbx.enabled(sandbox) else None
        self.job_dir = job_dir
        self.allowed = [t for t in allowed if t in SCHEMAS]
        self.limit = output_limit
        self.command_timeout = command_timeout
        self.guard = load_guard(root)
        self.readable_roots = [root, worktree.parent, Path("/tmp")]
        self.impl: dict[str, Callable[..., str]] = {"Read": self.read, "Grep": self.grep, "Glob": self.glob,
                                                     "Edit": self.edit, "Write": self.write, "Bash": self.bash}

    def specs(self) -> list[dict[str, Any]]:
        """OpenAI-style tool definitions (litellm translates them for every provider)."""
        return [{"type": "function", "function": {"name": n, **SCHEMAS[n]}} for n in self.allowed]

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """(output, is_error). Never raises: errors go back to the model as text."""
        if name not in self.allowed:
            return f"tool {name} is not available to this agent", True
        try:
            self.guard.check_tool(self.root, name, args, str(self.worktree), self.agent)
        except self.guard.Blocked as e:
            return f"factory guard: blocked: {e}", True
        try:
            out = self.impl[name](**args)
        except TypeError as e:
            return f"bad arguments for {name}: {e}", True
        except (ToolError, OSError, UnicodeDecodeError, re.error) as e:
            return str(e), True
        if len(out) > self.limit:
            out = out[: self.limit] + f"\n[truncated: {len(out) - self.limit} more characters]"
        return out, False

    # ---- implementations --------------------------------------------------------------------
    def path(self, p: str, write: bool = False) -> Path:
        path = Path(os.path.normpath(self.worktree / os.path.expanduser(p)))
        if not write and not any(path == r or r in path.parents for r in self.readable_roots):
            raise ToolError(f"{path} is outside the repo, the job worktrees and /tmp")
        if self.sandbox:
            self.sandboxed(path, write)
        return path

    def _resolve(self, entries: list[str] | None) -> list[str]:
        return [os.path.normpath(self.worktree / os.path.expanduser(e)) for e in entries or []]

    def sandboxed(self, path: Path, write: bool) -> None:
        """The sandbox's file rules for the Read/Grep/Glob/Edit/Write tools, which srt does not see."""
        assert self.sandbox is not None
        fs = self.sandbox.get("filesystem") or {}

        def hit(entries: list[str]) -> bool:
            s = str(path)
            return any(s == e or s.startswith(e.rstrip("/") + "/") or fnmatch(s, e) for e in entries)

        if hit(self._resolve(fs.get("deny_read"))) and not hit(self._resolve(fs.get("allow_read"))):
            raise ToolError(f"sandbox: {path} is not readable (sandbox.filesystem.deny_read)")
        if not write:
            return
        if hit(self._resolve(fs.get("deny_write"))):
            raise ToolError(f"sandbox: {path} is not writable (sandbox.filesystem.deny_write)")
        writable = sbx.writable_for(self.worktree, self.job_dir)
        if not hit([*(os.path.normpath(w) for w in writable), *self._resolve(fs.get("allow_write"))]):
            raise ToolError(f"sandbox: {path} is outside the job worktree, the job folder and the temp dir")

    def read(self, file_path: str, offset: int = 1, limit: int = 2000) -> str:
        path = self.path(file_path)
        if path.is_dir():
            raise ToolError(f"{path} is a directory; use Glob or Bash ls")
        lines = path.read_text(errors="replace").splitlines()
        start = max(int(offset or 1), 1)
        chunk = lines[start - 1: start - 1 + int(limit or 2000)]
        return "\n".join(f"{i:6}\t{line}" for i, line in enumerate(chunk, start)) or "(empty file)"

    def grep(self, pattern: str, path: str = ".", glob: str | None = None) -> str:
        base = self.path(path)
        if shutil.which("rg"):
            argv = ["rg", "-n", "--no-heading", "--color=never", "-e", pattern]
            argv += ["-g", glob] if glob else []
            r = subprocess.run([*argv, str(base)], capture_output=True, text=True, timeout=120)
            return r.stdout or "(no matches)"
        rx = re.compile(pattern)
        out = []
        files = [base] if base.is_file() else base.rglob(glob or "*")
        for f in files:
            if not f.is_file() or ".git" in f.parts or "node_modules" in f.parts:
                continue
            try:
                for i, line in enumerate(f.read_text(errors="strict").splitlines(), 1):
                    if rx.search(line):
                        out.append(f"{f}:{i}:{line}")
            except (UnicodeDecodeError, OSError):
                continue
            if len(out) > 2000:
                break
        return "\n".join(out) or "(no matches)"

    def glob(self, pattern: str, path: str = ".") -> str:
        base = self.path(path)
        hits = sorted(str(p) for p in base.glob(pattern) if ".git" not in p.parts and "node_modules" not in p.parts)
        return "\n".join(hits[:1000]) or "(no files)"

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        path = self.path(file_path, write=True)
        text = path.read_text()
        n = text.count(old_string)
        if n == 0:
            raise ToolError("old_string not found in the file")
        if n > 1 and not replace_all:
            raise ToolError(f"old_string occurs {n} times; give more context or set replace_all")
        path.write_text(text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1))
        return f"edited {path}"

    def write(self, file_path: str, content: str) -> str:
        path = self.path(file_path, write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return f"wrote {path} ({len(content)} characters)"

    def bash(self, command: str, timeout: int | None = None) -> str:
        env = agent_env(dict(os.environ))
        env["FACTORY_GUARD"] = "strict"
        try:
            name = f"{self.job_dir.name if self.job_dir else self.worktree.name}-{self.agent}"
            argv = sbx.wrap(["bash", "-c", command], self.sandbox, sbx.settings_path(self.root, name),
                            sbx.writable_for(self.worktree, self.job_dir))
        except sbx.SandboxUnavailable as e:
            raise ToolError(str(e)) from None
        try:
            r = subprocess.run(argv, cwd=self.worktree, capture_output=True, text=True,
                               errors="replace", env=env, timeout=min(int(timeout or self.command_timeout),
                                                                      self.command_timeout))
        except subprocess.TimeoutExpired:
            raise ToolError(f"command timed out after {timeout or self.command_timeout}s") from None
        out = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
        return f"{out}\n[exit {r.returncode}]" if out else f"[exit {r.returncode}]"
