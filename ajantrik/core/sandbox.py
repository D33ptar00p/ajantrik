"""The OS sandbox around everything that runs a job's code: Anthropic's sandbox-runtime (`srt`).

`sandbox` in factory.json (snake_case) is mapped to two shapes:

    srt_settings()     the srt settings file (camelCase), for commands the factory starts itself:
                       the api runtime's Bash tool, worktree setup, gate steps and the codex process
    claude_settings()  the `sandbox` block of .claude/settings.json; Claude Code runs its Bash
                       through the same runtime

`wrap()` turns an argv into `srt --settings <file> <argv>`. When the sandbox is enabled but cannot
run, it raises SandboxUnavailable (fail_if_unavailable, the default) or runs the command as it is.
Stdlib only: ajantrik/core is copied into each repo's .factory/lib.
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .util import write_json

# What srt itself needs next to it, per platform.
PLATFORM_DEPS = {"linux": ["bwrap", "socat", "rg"], "darwin": ["sandbox-exec", "rg"]}
INSTALL = {"linux": "npm i -g @anthropic-ai/sandbox-runtime; apt install bubblewrap socat ripgrep",
           "darwin": "npm i -g @anthropic-ai/sandbox-runtime; brew install ripgrep"}
USERNS_HINT = ("On Ubuntu 24.04+ bubblewrap may also need `sysctl kernel.apparmor_restrict_unprivileged_userns=0` "
               "(or an AppArmor profile for bwrap).")


class SandboxUnavailable(Exception):
    """The sandbox is on and must be used, but it cannot run on this machine."""


def enabled(sb: dict[str, Any] | None) -> bool:
    return bool(sb and sb.get("enabled", True))


def platform() -> str:
    return "darwin" if sys.platform == "darwin" else "win32" if sys.platform.startswith("win") else "linux"


def missing(sb: dict[str, Any]) -> list[str]:
    """Binaries the sandbox needs that are not on PATH (srt first)."""
    need = [sb.get("binary") or "srt", *PLATFORM_DEPS.get(platform(), [])]
    return [b for b in need if not shutil.which(b)]


def available(sb: dict[str, Any]) -> tuple[bool, str]:
    gone = missing(sb)
    if not gone:
        return True, f"{sb.get('binary') or 'srt'} at {shutil.which(sb.get('binary') or 'srt')}" + (
            "; Windows support in sandbox-runtime is alpha" if platform() == "win32" else "")
    hint = INSTALL.get(platform(), "npx @anthropic-ai/sandbox-runtime windows-install (elevated)")
    return False, f"not found on PATH: {', '.join(gone)}. Install: {hint}." + (
        f" {USERNS_HINT}" if platform() == "linux" else "")


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    """Drop unset keys and empty lists so the files stay readable."""
    return {k: v for k, v in d.items() if v is not None and v != [] and v != {}}


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(i) for i in items if i))


def srt_settings(sb: dict[str, Any], writable: Iterable[str | Path] = (),
                 extra_domains: Iterable[str] = ()) -> dict[str, Any]:
    net, fs = sb.get("network") or {}, sb.get("filesystem") or {}
    data: dict[str, Any] = {
        # srt refuses a settings file without these lists, even empty ones: they are always written.
        "network": {
            "allowedDomains": _unique([*(net.get("allowed_domains") or []), *extra_domains]),
            "deniedDomains": net.get("denied_domains") or [],
            **_clean({"allowUnixSockets": net.get("allow_unix_sockets") or [],
                      "allowAllUnixSockets": net.get("allow_all_unix_sockets") or None}),
            "allowLocalBinding": bool(net.get("allow_local_binding")),
        },
        "filesystem": {
            "denyRead": _paths(fs.get("deny_read")),
            "allowRead": _paths(fs.get("allow_read")),
            "allowWrite": _unique([*map(str, writable), *_paths(fs.get("allow_write"))]),
            "denyWrite": _paths(fs.get("deny_write")),
        },
        "ignoreViolations": sb.get("ignore_violations") or {},
        "enableWeakerNestedSandbox": bool(sb.get("enable_weaker_nested_sandbox")),
        "enableWeakerNetworkIsolation": bool(sb.get("enable_weaker_network_isolation")),
        "allowNestedUserNamespaces": bool(sb.get("allow_nested_user_namespaces")),
    }
    return _clean(data)


def _paths(v: list[str] | None) -> list[str]:
    return list(v or [])


# The factory's own CLI runs outside Claude Code's sandbox: the guard vets it (agents never approve or
# deliver), it sandboxes worktree setup and gate steps itself with srt (no sandbox inside a sandbox), and
# delivery has to reach the git host and the ticket system.
CLAUDE_EXCLUDED = ["python3 .factory/factory.py *"]


def claude_settings(sb: dict[str, Any], writable: Iterable[str | Path] = ()) -> dict[str, Any]:
    """The `sandbox` block of .claude/settings.json (Claude Code's own keys)."""
    if not enabled(sb):
        return {"enabled": False}
    srt = srt_settings(sb, writable)
    net = sb.get("network") or {}
    network = {**srt.get("network", {}),
               **_clean({"httpProxyPort": net.get("http_proxy_port"), "socksProxyPort": net.get("socks_proxy_port")})}
    return _clean({
        "enabled": True,
        "failIfUnavailable": bool(sb.get("fail_if_unavailable", True)),
        "autoAllowBashIfSandboxed": bool(sb.get("auto_allow_bash", True)),
        "allowUnsandboxedCommands": bool(sb.get("allow_unsandboxed_commands")),
        "excludedCommands": _unique([*CLAUDE_EXCLUDED, *(sb.get("excluded_commands") or [])]),
        "network": network,
        "filesystem": srt.get("filesystem", {}),
        "ignoreViolations": srt.get("ignoreViolations"),
        "enableWeakerNestedSandbox": srt.get("enableWeakerNestedSandbox"),
        "enableWeakerNetworkIsolation": srt.get("enableWeakerNetworkIsolation"),
        "allowNestedUserNamespaces": srt.get("allowNestedUserNamespaces"),
    })


def settings_path(root: Path, name: str) -> Path:
    """Where a run's srt settings go: under .factory/.sandbox/, which no sandboxed command can write, so a
    process left over from an earlier command cannot swap the rules before the next one starts."""
    return root / ".factory" / ".sandbox" / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}.json"


def writable_for(worktree: Path | str | None = None, job_dir: Path | str | None = None) -> list[str]:
    """What a job's commands may always write: its own worktree, its job folder and the temp dir."""
    return _unique([str(worktree or ""), str(job_dir or ""), tempfile.gettempdir()])


def wrap(argv: list[str], sb: dict[str, Any] | None, settings_path: Path, writable: Iterable[str | Path] = (),
         extra_domains: Iterable[str] = (), say: Any = None) -> list[str]:
    """argv run inside the sandbox, or argv itself when the sandbox is off (or unavailable and allowed to be)."""
    if not sb or not enabled(sb):
        return argv
    ok, detail = available(sb)
    if not ok:
        if sb.get("fail_if_unavailable", True):
            raise SandboxUnavailable(f"sandbox unavailable ({detail}); fail_if_unavailable is on, so this did not "
                                     "run. Install it, or turn the sandbox off in the studio's Sandbox tab.")
        if say:
            say(f"warning: running without the sandbox: {detail}")
        return argv
    write_json(settings_path, srt_settings(sb, writable, extra_domains))
    binary = shutil.which(sb.get("binary") or "srt") or (sb.get("binary") or "srt")
    return [binary, "--settings", str(settings_path), *argv]
