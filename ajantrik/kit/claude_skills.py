"""Skills Claude Code already has, outside the factory's own (.factory/config/skills/):

    <repo>/.claude/skills/*/SKILL.md, <repo>/.claude/commands/*.md     the repo's own (not rendered by the factory)
    ~/.claude/skills/*/SKILL.md, ~/.claude/commands/*.md               the user's
    <plugin>/skills/*/SKILL.md, <plugin>/commands/*.md                 each enabled plugin's, named plugin:skill

An agent can preload any of them by name. They are referenced, not copied: Claude Code resolves the name
itself, and the headless runner reads the file when it builds the agent's prompt. ~/.claude is
$CLAUDE_CONFIG_DIR when set, as in Claude Code.

The MCP servers Claude Code would start in the repo are found the same way (discover_mcp), from the repo's
.mcp.json, ~/.claude.json (global and this project's) and each enabled plugin's .mcp.json, as the tool prefix
an agent's `mcp` list takes (mcp__<server>, mcp__plugin_<plugin>_<server>). Only server names are read.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SOURCES = ("repo", "user", "plugin")


@dataclass
class ExternalSkill:
    name: str
    description: str
    source: str  # repo | user | plugin
    origin: str  # what to show: ".claude", "~/.claude" or the plugin's name
    path: Path

    def body(self) -> str:
        from ajantrik.kit.project import split_frontmatter

        return split_frontmatter(self.path.read_text())[1].strip()

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "source": self.source, "origin": self.origin}


@dataclass
class ExternalMcp:
    name: str    # the tool prefix, e.g. mcp__plugin_playwright_playwright
    server: str
    source: str  # repo | user | local | plugin
    origin: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "server": self.server, "source": self.source, "origin": self.origin}


def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def global_config() -> Path:
    """Claude Code's ~/.claude.json (inside $CLAUDE_CONFIG_DIR when that is set)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) / ".claude.json" if env else Path.home() / ".claude.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _describe(path: Path) -> str | None:
    """The skill's description, or None when the file can't be read or parsed."""
    from ajantrik.kit.project import split_frontmatter

    try:
        meta, body = split_frontmatter(path.read_text())
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
        return None
    desc = str(meta.get("description") or "").strip()
    if desc:
        return desc
    lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    return lines[0] if lines else ""


def _files(base: Path) -> list[tuple[str, Path]]:
    """(name, file) for every skill and command under one .claude-like directory."""
    out = [(f.parent.name, f) for f in sorted(base.glob("skills/*/SKILL.md"))]
    out += [(f.stem, f) for f in sorted(base.glob("commands/*.md"))]
    return out


def enabled_plugins(root: Path, cfg: Path | None = None) -> dict[str, Path]:
    """{plugin name: install path} for each plugin enabled in the user's or the repo's settings."""
    cfg = cfg or config_dir()
    enabled: dict[str, bool] = {}
    for settings in (cfg / "settings.json", root / ".claude" / "settings.json", root / ".claude" / "settings.local.json"):
        plugins = _read_json(settings).get("enabledPlugins")
        if isinstance(plugins, dict):
            enabled.update({str(k): bool(v) for k, v in plugins.items()})
    installed = _read_json(cfg / "plugins" / "installed_plugins.json").get("plugins")
    out: dict[str, Path] = {}
    for key, on in enabled.items():
        entries = installed.get(key) if on and isinstance(installed, dict) else None
        if not isinstance(entries, list):
            continue
        entries = [e for e in entries if isinstance(e, dict) and e.get("installPath")]
        if entries:
            newest = max(entries, key=lambda e: str(e.get("lastUpdated") or e.get("installedAt") or ""))
            out[key.split("@", 1)[0]] = Path(newest["installPath"])
    return out


def discover(root: Path, exclude: set[str] | None = None) -> dict[str, ExternalSkill]:
    """Every skill Claude Code would offer in this repo, except the factory's (`exclude` and whatever
    render wrote). On a name clash the repo's wins over the user's, the user's over a plugin's."""
    exclude = {*(exclude or ()), "factory"}
    rendered = set(_read_json(root / ".factory" / ".rendered.json"))
    cfg = config_dir()
    found: list[tuple[str, str, str, Path]] = []
    for name, f in _files(root / ".claude"):
        if f.relative_to(root).as_posix() not in rendered:
            found.append(("repo", ".claude", name, f))
    found += [("user", "~/.claude", name, f) for name, f in _files(cfg)]
    for plugin, path in sorted(enabled_plugins(root, cfg).items()):
        found += [("plugin", plugin, f"{plugin}:{name}", f) for name, f in _files(path)]
    out: dict[str, ExternalSkill] = {}
    for source, origin, name, f in found:
        if name in exclude or name in out:
            continue
        desc = _describe(f)
        if desc is not None:
            out[name] = ExternalSkill(name, desc, source, origin, f)
    return out


def _servers(data: Any, flat: bool = False) -> list[str]:
    """Server names from an mcpServers mapping (a plugin's .mcp.json may hold the servers at the top level)."""
    if not isinstance(data, dict):
        return []
    servers = data.get("mcpServers", data if flat else None)
    return sorted(str(k) for k, v in servers.items() if isinstance(v, dict)) if isinstance(servers, dict) else []


def _tool_name(*parts: str) -> str:
    return "mcp__" + "_".join(re.sub(r"[^A-Za-z0-9_-]", "_", p) for p in parts)


def discover_mcp(root: Path) -> dict[str, ExternalMcp]:
    """Every MCP server Claude Code would start in this repo, keyed by the tool prefix agents list."""
    found = [("repo", ".mcp.json", s, _tool_name(s)) for s in _servers(_read_json(root / ".mcp.json"))]
    gcfg = _read_json(global_config())
    found += [("user", "~/.claude.json", s, _tool_name(s)) for s in _servers(gcfg)]
    projects = gcfg.get("projects")
    project = projects.get(str(root.resolve())) if isinstance(projects, dict) else None
    found += [("local", "~/.claude.json (this repo)", s, _tool_name(s)) for s in _servers(project)]
    for plugin, path in sorted(enabled_plugins(root).items()):
        names = _servers(_read_json(path / ".mcp.json"), flat=True)
        names += _servers(_read_json(path / ".claude-plugin" / "plugin.json"))
        found += [("plugin", plugin, s, _tool_name("plugin", plugin, s)) for s in sorted(set(names))]
    out: dict[str, ExternalMcp] = {}
    for source, origin, server, name in found:
        out.setdefault(name, ExternalMcp(name, server, source, origin))
    return out
