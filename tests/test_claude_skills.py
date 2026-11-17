"""Claude Code's own skills (repo, user, plugin) offered to factory agents."""

from __future__ import annotations

import json
from pathlib import Path

from ajantrik.editions.headless.runner import system_prompt
from ajantrik.kit.claude_skills import discover, discover_mcp
from ajantrik.kit.render import render
from ajantrik.studio.server import Studio


def skill(base: Path, name: str, description: str, body: str = "Do the thing.") -> Path:
    f = base / "skills" / name / "SKILL.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")
    return f


def plugin(home: Path, key: str, enabled: bool = True) -> Path:
    name = key.split("@")[0]
    old, new = home / "plugins" / "cache" / name / "1.0", home / "plugins" / "cache" / name / "2.0"
    installed = home / "plugins" / "installed_plugins.json"
    data = json.loads(installed.read_text()) if installed.is_file() else {"version": 2, "plugins": {}}
    data["plugins"][key] = [{"installPath": str(old), "lastUpdated": "2026-01-01T00:00:00Z"},
                            {"installPath": str(new), "lastUpdated": "2026-06-01T00:00:00Z"}]
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(json.dumps(data))
    settings = home / "settings.json"
    s = json.loads(settings.read_text()) if settings.is_file() else {}
    s.setdefault("enabledPlugins", {})[key] = enabled
    settings.write_text(json.dumps(s))
    return new


def test_discovers_repo_user_and_enabled_plugin_skills(tmp_path, claude_home):
    repo = tmp_path / "repo"
    skill(repo / ".claude", "repo-rules", "the repo's rules")
    (repo / ".claude" / "commands").mkdir(parents=True)
    (repo / ".claude" / "commands" / "ship.md").write_text("# Ship\n\nShip the release.\n")
    skill(claude_home, "my-style", "my style")
    (claude_home / "commands").mkdir()
    (claude_home / "commands" / "pr-description.md").write_text("---\ndescription: write a PR body\n---\nBody\n")
    skill(plugin(claude_home, "caveman@caveman"), "caveman", "talk short")
    skill(claude_home / "plugins" / "cache" / "caveman" / "1.0", "stale", "older install")
    skill(plugin(claude_home, "figma@official", enabled=False), "figma-use", "figma")

    found = discover(repo)
    assert {n: (s.source, s.origin) for n, s in found.items()} == {
        "repo-rules": ("repo", ".claude"), "ship": ("repo", ".claude"), "my-style": ("user", "~/.claude"),
        "pr-description": ("user", "~/.claude"), "caveman:caveman": ("plugin", "caveman")}
    assert found["ship"].description == "Ship the release."
    assert found["pr-description"].description == "write a PR body"
    assert found["caveman:caveman"].body() == "Do the thing."


def test_repo_settings_enable_plugins_and_repo_beats_user(tmp_path, claude_home):
    repo = tmp_path / "repo"
    path = plugin(claude_home, "kit@market", enabled=False)
    skill(path, "lint", "lint it")
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": {"kit@market": True}}))
    skill(repo / ".claude", "dup", "the repo's")
    skill(claude_home, "dup", "the user's")
    found = discover(repo)
    assert "kit:lint" in found
    assert found["dup"].description == "the repo's"


def test_factory_skills_rendered_copies_and_broken_files_are_left_out(tmp_path, claude_home):
    repo = tmp_path / "repo"
    skill(repo / ".claude", "test-first", "factory copy")
    skill(repo / ".claude", "mine", "kept")
    skill(repo / ".claude", "rendered", "written by render")
    (repo / ".claude" / "skills" / "factory").mkdir()
    (repo / ".claude" / "skills" / "factory" / "SKILL.md").write_text("orchestrator")
    (repo / ".factory").mkdir()
    (repo / ".factory" / ".rendered.json").write_text(json.dumps({".claude/skills/rendered/SKILL.md": "x"}))
    bad = claude_home / "skills" / "broken" / "SKILL.md"
    bad.parent.mkdir(parents=True)
    bad.write_text("---\n- not: a mapping\n---\nbody\n")
    (claude_home / "plugins").mkdir()
    (claude_home / "plugins" / "installed_plugins.json").write_text("not json")
    assert set(discover(repo, exclude={"test-first"})) == {"mine"}


def test_agents_can_preload_claude_code_skills(factory, claude_home):
    p = factory()
    skill(plugin(claude_home, "caveman@caveman"), "caveman", "talk short", body="Few words.")
    agent = p.agents()["architect"]
    agent.skills = [*agent.skills, "caveman:caveman"]
    p.save_agent(agent)
    loaded = p.load()
    assert loaded.ok, loaded.errors
    assert not render(p).errors
    assert "- caveman:caveman" in (p.root / ".claude" / "agents" / "architect.md").read_text()
    # Not copied: Claude Code resolves the name; render owns only the factory's skills.
    assert not (p.root / ".claude" / "skills" / "caveman:caveman").exists()
    prompt = system_prompt(loaded.cfg, loaded.agents["architect"], loaded.skills, loaded.external_skills)
    assert "## Skill: caveman:caveman" in prompt and "Few words." in prompt

    state = Studio(p.root).state()
    assert {"name": "caveman:caveman", "description": "talk short", "source": "plugin",
            "origin": "caveman"} in state["external_skills"]
    assert not any(s["name"] == "caveman:caveman" for s in state["skills"])

    agent.skills = [*agent.skills, "nowhere"]
    p.save_agent(agent)
    assert "agent architect: unknown skill 'nowhere'" in p.load().errors


def test_discovers_mcp_servers_as_tool_prefixes(tmp_path, claude_home):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"move-api": {"command": "x"}}}))
    (claude_home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"notes": {"command": "y", "env": {"TOKEN": "secret"}}},
        "projects": {str(repo.resolve()): {"mcpServers": {"figma": {"type": "http"}}},
                     "/elsewhere": {"mcpServers": {"other": {"type": "http"}}}}}))
    (plugin(claude_home, "playwright@official") / ".mcp.json").parent.mkdir(parents=True)
    (plugin(claude_home, "playwright@official") / ".mcp.json").write_text(json.dumps({"playwright": {"command": "npx"}}))
    atl = plugin(claude_home, "atlassian@official")
    atl.mkdir(parents=True)
    (atl / ".mcp.json").write_text(json.dumps({"mcpServers": {"atlassian": {"type": "http"}}}))
    off = plugin(claude_home, "github@official", enabled=False)
    off.mkdir(parents=True)
    (off / ".mcp.json").write_text(json.dumps({"github": {"type": "http"}}))

    found = discover_mcp(repo)
    assert {n: m.source for n, m in found.items()} == {
        "mcp__move-api": "repo", "mcp__notes": "user", "mcp__figma": "local",
        "mcp__plugin_atlassian_atlassian": "plugin", "mcp__plugin_playwright_playwright": "plugin"}
    assert "secret" not in json.dumps([m.to_json() for m in found.values()])


def test_studio_offers_mcp_servers_and_saves_the_choice(factory, claude_home):
    p = factory()
    path = plugin(claude_home, "playwright@official")
    path.mkdir(parents=True)
    (path / ".mcp.json").write_text(json.dumps({"playwright": {"command": "npx"}}))
    studio = Studio(p.root)
    assert {"name": "mcp__plugin_playwright_playwright", "server": "playwright", "source": "plugin",
            "origin": "playwright"} in studio.state()["external_mcp"]
    a = p.agents()["architect"].to_json()
    r = studio.save_agent("architect", {**a, "mcp": ["mcp__plugin_playwright_playwright", "mcp__claude_ai_Atlassian"]})
    assert r["ok"] and r["external_mcp"]
    assert p.agents()["architect"].mcp == ["mcp__plugin_playwright_playwright", "mcp__claude_ai_Atlassian"]
