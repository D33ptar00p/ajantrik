"""Schema rules, routing, rendering (presets against the hand-built factories), tickets and backlog."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest
import yaml
from pydantic import ValidationError

from ajantrik.core.connections import tickets as tix
from ajantrik.core.routing import refused, route_reviewers
from ajantrik.core.util import FactoryError, glob_match
from ajantrik.kit.project import Project, list_presets
from ajantrik.kit.render import render
from ajantrik.kit.schema import FactoryConfig, warnings

from .conftest import sh

MOVE = Path("/home/deep/Documents/Moveai/Engineering/repos/move-software-factories")
ORIGINALS = {"python-qt": MOVE / "mocap-desktop-software-factory-claude-code",
             "react-vite": MOVE / "accounts-portal-software-factory-claude-code",
             "python-ts-data": Path("/home/deep/Documents/Moveai/personal/omnigrid")}


def cfg(**kw) -> FactoryConfig:
    return FactoryConfig.model_validate({"repo": {"name": "x"}, **kw})


# ---- schema ------------------------------------------------------------------------------------------
def test_defaults_are_the_hand_built_loop():
    c = cfg()
    assert [s.id for s in c.loop.stages] == ["spec", "design", "approval", "tests", "build", "gate", "review",
                                              "integrate", "deliver"]
    assert c.repo.title == "x Software Factory" and c.delivery.mode == "none"


@pytest.mark.parametrize("stages, message", [
    ([{"id": "deliver", "kind": "delivery"}, {"id": "spec", "kind": "agent", "agent": "a"}], "must be last"),
    ([{"id": "gate", "kind": "gate"}], "need a builders stage"),
    ([{"id": "b", "kind": "builders"}, {"id": "g", "kind": "gate", "on_fail": "z"}], "not a stage"),
    ([{"id": "b", "kind": "builders"}, {"id": "g", "kind": "gate", "on_fail": "x"},
      {"id": "x", "kind": "agent", "agent": "x"}], "earlier stage"),
    ([{"id": "a", "kind": "agent", "agent": "x"}, {"id": "a", "kind": "approval"}], "duplicate"),
    ([{"id": "ready", "kind": "approval"}], "reserved"),
    ([{"id": "Bad Id", "kind": "approval"}], "kebab"),
    ([{"id": "a", "kind": "agent"}], "needs `agent`"),
])
def test_loop_rules(stages, message):
    with pytest.raises(ValidationError, match=message):
        cfg(loop={"stages": stages})


def test_credentials_must_be_references():
    with pytest.raises(ValidationError, match="never the value"):
        cfg(connections={"tickets": {"provider": "jira", "token_ref": "ATATT3xFfGF0abc"}})
    with pytest.raises(ValidationError, match="never the value"):
        cfg(runtime={"api": {"api_key_ref": "sk-ant-xyz"}})
    c = cfg(connections={"tickets": {"provider": "jira", "project": "GEN", "token_ref": "env:JIRA_TOKEN",
                                     "site": "https://x.atlassian.net"}})
    assert c.connections.tickets.jql and "project = GEN" in c.connections.tickets.jql
    assert c.connections.tickets.browse_url == "https://x.atlassian.net/browse/"


def test_warnings_for_risky_choices():
    stages = [s for s in cfg().model_dump()["loop"]["stages"] if s["kind"] != "approval"]
    w = warnings(cfg(loop={"stages": stages}, delivery={"mode": "direct", "require_approval": False}))
    assert any("direct without require_approval" in x for x in w)
    assert any("no human approval at all" in x for x in w)
    assert any("no Codex" in x or "no tool hooks" in x for x in warnings(cfg(runtime={"kind": "codex-cli"})))


def test_minimal_loop_without_gate_or_reviewers():
    c = cfg(loop={"stages": [{"id": "build", "kind": "builders"}, {"id": "ship", "kind": "delivery"}]})
    assert any("no gate" in w for w in warnings(c))


# ---- routing ------------------------------------------------------------------------------------------
def test_glob_semantics():
    assert glob_match("mocap_desktop_gui/mc_calib_view.py", "mc_calib*")
    assert glob_match("pyside_rc/qml/Main.qml", "pyside_rc/qml/")
    assert glob_match("a/b/pyside_rc/qml/x.qml", "pyside_rc/qml/")
    assert glob_match("src/pages/billing/Plan.tsx", "src/**/billing/*")
    assert not glob_match("src/app.ts", "*.tsx")


def test_python_qt_routing_matches_the_original_rules(tmp_path):
    p = Project(tmp_path)
    p.init_from_preset("python-qt")
    c = p.load().cfg.model_dump()  # type: ignore[union-attr]
    r = route_reviewers(c, ["mocap_desktop_gui/mc_calib_panel.py"], {})
    assert r["calib"] and not r["xplat"] and r["code"] == "always"
    r = route_reviewers(c, ["mocap_desktop_gui/widgets.py"], {"mocap_desktop_gui/widgets.py": ["  subprocess.run(x)"]})
    assert r["xplat"] and not r["calib"]
    r = route_reviewers(c, ["mocap_desktop_gui/model.py"], {"mocap_desktop_gui/model.py": ["    @Property(int)"]})
    assert r["ui"] and r["ux"]
    r = route_reviewers(c, ["pyside_rc/qml/Panel.qml"], {})
    assert r["ui"] and r["ux"] and not r["calib"]


def test_routing_exclusions_metrics_and_refusals(tmp_path):
    p = Project(tmp_path)
    p.init_from_preset("react-vite")
    c = p.load().cfg.model_dump()  # type: ignore[union-attr]
    r = route_reviewers(c, ["src/components/__tests__/Button.test.tsx"], {})
    assert not r["ui"] and not r["ux"]
    assert refused(c, ["package.json"], {"package.json": ['    "postinstall": "node x.js",']})
    assert not refused(c, ["package.json"], {"package.json": ['    "lodash": "^4.17.21",']})
    p2 = Project(tmp_path / "og")
    p2.init_from_preset("python-ts-data")
    c2 = p2.load().cfg.model_dump()  # type: ignore[union-attr]
    assert route_reviewers(c2, ["README.md"], {}, {"data_mb_delta": 2.5})["deploy"]
    assert not route_reviewers(c2, ["README.md"], {}, {"data_mb_delta": 0.1})["deploy"]


# ---- presets and render ----------------------------------------------------------------------------------
def test_every_preset_loads_and_renders(tmp_path):
    names = [x["name"] for x in list_presets()]
    assert {"blank", "python-qt", "react-vite", "python-ts-data"} <= set(names)
    for name in names:
        root = tmp_path / name
        root.mkdir()
        sh("git", "init", "-q", cwd=root)
        p = Project(root)
        p.init_from_preset(name)
        res = render(p)
        assert not res.errors and not res.warnings, (name, res.errors, res.warnings)
        assert render(p, check=True).changed == []  # idempotent
        data = json.loads((root / ".factory" / "factory.json").read_text())
        for agent in p.load().cfg.agent_names():  # type: ignore[union-attr]
            assert (root / ".claude" / "agents" / f"{agent}.md").is_file(), (name, agent)
        assert data["repo"]["name"]


@pytest.mark.skipif(not MOVE.is_dir(), reason="the hand-built factories are not on this machine")
@pytest.mark.parametrize("preset", ["python-qt", "react-vite", "python-ts-data"])
def test_presets_reproduce_the_hand_built_factories(tmp_path, preset):
    original = ORIGINALS[preset]
    root = tmp_path / preset
    root.mkdir()
    sh("git", "init", "-q", cwd=root)
    p = Project(root)
    p.init_from_preset(preset)
    render(p)
    from ajantrik.kit.project import split_frontmatter

    orig_agents = sorted(f.stem for f in (original / ".claude" / "agents").glob("*.md"))
    assert sorted(f.stem for f in (root / ".claude" / "agents").glob("*.md")) == orig_agents
    for name in orig_agents:
        want_meta, want = split_frontmatter((original / ".claude" / "agents" / f"{name}.md").read_text())
        got_meta, got = split_frontmatter((root / ".claude" / "agents" / f"{name}.md").read_text())
        got = got.split("-->\n\n", 1)[-1]  # the generated-file banner
        # bodies differ only where the preset moved repo scripts to .factory/harness/
        norm = lambda s: s.replace(".factory/harness/", ".factory/").strip()  # noqa: E731
        assert norm(got) == norm(want), name
        assert got_meta["model"] == want_meta["model"], name
        assert set(got_meta.get("skills") or []) == set(want_meta.get("skills") or []), name
    want_settings = json.loads((original / ".claude" / "settings.json").read_text())
    got_settings = json.loads((root / ".claude" / "settings.json").read_text())
    assert got_settings == want_settings
    for skill in (original / ".claude" / "skills").iterdir():
        if skill.name != "factory":
            assert (root / ".claude" / "skills" / skill.name / "SKILL.md").is_file()


def test_render_keeps_user_settings_and_human_files(git_repo, factory):
    (git_repo / ".claude").mkdir(exist_ok=True)
    (git_repo / ".claude" / "settings.json").write_text(json.dumps({"model": "opus", "permissions": {
        "allow": ["Bash(npm test)"], "deny": ["Read(secret.txt)"]}}))
    p = factory()
    s = json.loads((git_repo / ".claude" / "settings.json").read_text())
    assert s["model"] == "opus" and s["permissions"]["allow"] == ["Bash(npm test)"]
    assert "Read(secret.txt)" in s["permissions"]["deny"] and "Read(**/.env)" in s["permissions"]["deny"]
    assert len(s["hooks"]["PreToolUse"]) == 3
    allow = git_repo / ".factory" / "security" / "allow.txt"
    allow.write_text("abc  # reviewed\n")
    render(p)
    assert allow.read_text() == "abc  # reviewed\n"
    assert json.loads((git_repo / ".claude" / "settings.json").read_text()) == s  # idempotent merge


def test_removed_agents_are_cleaned_up_unless_edited(git_repo, factory):
    p = factory()
    data = p.raw()
    data["reviewers"]["always"] = [{"id": "code"}]
    p.yaml_path.write_text(yaml.safe_dump(data))
    p.delete_agent("reviewer-security")
    res = render(p)
    assert ".claude/agents/reviewer-security.md" in res.removed
    assert not (git_repo / ".claude" / "agents" / "reviewer-security.md").exists()


def test_skill_md_follows_the_loop(git_repo, factory):
    factory(connections__tickets={"provider": "jira", "mode": "mcp", "site": "https://m.atlassian.net",
                                  "project": "GEN"}, delivery__mode="pr")
    text = (git_repo / ".claude" / "skills" / "factory" / "SKILL.md").read_text()
    assert "getJiraIssue" in text and "GEN-123" in text and "/factory sync" in text
    assert "factory.py deliver" in text and "Never push, never merge" not in text
    factory()
    text = (git_repo / ".claude" / "skills" / "factory" / "SKILL.md").read_text()
    assert "getJiraIssue" not in text and "Never push, never merge" in text and "/factory sync" not in text


def test_guard_hooks_in_a_rendered_repo(git_repo, factory):
    factory()
    guard = git_repo / ".factory" / "security" / "guard.py"

    def hook(kind, event):
        return subprocess.run([sys.executable, str(guard), kind], input=json.dumps(event), capture_output=True,
                              text=True, env={"CLAUDE_PROJECT_DIR": str(git_repo), "PATH": "/usr/bin:/bin"})

    wt = git_repo.parent / "demo-factory" / "001-x"
    wt.mkdir(parents=True)
    ev = {"session_id": "s", "cwd": str(git_repo), "agent_type": "builder-main", "tool_name": "Write",
          "tool_input": {"file_path": str(wt / "a.py"), "content": "x"}}
    assert hook("files", ev).returncode == 0
    ev["tool_input"] = {"file_path": str(git_repo / "app" / "a.py"), "content": "x"}
    assert hook("files", ev).returncode == 2


# ---- tickets ---------------------------------------------------------------------------------------------
class FakeJira:
    issues: ClassVar[dict[str, dict]] = {"GEN-1": {"key": "GEN-1", "summary": "Add rename", "type": "Story", "status": "To Do",
                        "status_category": "To Do", "labels": ["factory"], "epic": "GEN-100"},
              "GEN-2": {"key": "GEN-2", "summary": "Fix crash", "type": "Bug", "status": "To Do",
                        "status_category": "To Do", "labels": ["factory"], "epic": None},
              "GEN-3": {"key": "GEN-3", "summary": "Export", "type": "Story", "status": "To Do",
                        "status_category": "To Do", "labels": ["factory"], "epic": "GEN-200"}}

    def __init__(self, conn):
        pass

    def search(self, jql, limit=50):
        return list(self.issues.values())

    def get(self, key):
        return self.issues[key]


def test_sync_and_next(git_repo, factory, monkeypatch):
    monkeypatch.setattr(tix, "Jira", FakeJira)
    sh("git", "push", "-q", "origin", "main:feature/GEN-100_Projects", cwd=git_repo)
    factory(connections__tickets={"provider": "jira", "mode": "rest", "site": "https://m.atlassian.net",
                                      "project": "GEN"})
    from ajantrik.core.engine import load_config

    c = load_config(git_repo)
    report = tix.sync(git_repo, c, [])
    assert report == ["GEN-1: added (base feature/GEN-100_Projects)", "GEN-2: added (base main)",
                      "GEN-3: blocked (epic GEN-200 has no branch on origin)"]
    assert tix.sync(git_repo, c, [])[0].startswith("GEN-1: skipped")
    FakeJira.issues["GEN-1"] = {**FakeJira.issues["GEN-1"], "status_category": "Done", "status": "Done"}
    item = tix.claim_next(git_repo, c)
    assert item and item["key"] == "GEN-2" and item["base"] == "main"
    text = (git_repo / ".factory" / "backlog.md").read_text()
    assert "- [x] GEN-1 · base: feature/GEN-100_Projects · Add rename → Done" in text
    assert "- [x] GEN-2 · base: main · Fix crash → claimed" in text
    from ajantrik.core.engine import Engine

    job = Engine(git_repo).new_job(item["text"], item["key"], item["base"])
    tix.mark_claimed(git_repo, item["line"], job["id"])
    assert f"→ {job['id']}" in (git_repo / ".factory" / "backlog.md").read_text()
    assert job["branch"] == "bugfix/GEN-2_Fix_crash" and job["ticket"] == "GEN-2"


def _three_item_backlog(root: Path, *lines: str) -> Path:
    path = root / ".factory" / "backlog.md"
    path.write_text(tix.BACKLOG_HEADER + "".join(ln + "\n" for ln in lines))
    return path


def test_next_items_makes_one_job_from_several_items(git_repo, factory):
    from ajantrik.core.engine import Engine

    factory()
    path = _three_item_backlog(git_repo, "- [ ] t0", "- [ ] t1", "- [ ] t2")
    claim = tix.claim_items(git_repo, [0, 2])
    job = Engine(git_repo).new_job(claim["request"], claim["key"], claim["base"])
    for it in claim["items"]:
        tix.mark_claimed(git_repo, it["claimed"], job["id"])
    assert job["request"] == "t0; t2" and job["ticket"] is None
    lines = path.read_text().splitlines()
    assert f"- [x] t0 → {job['id']}" in lines and f"- [x] t2 → {job['id']}" in lines
    assert "- [ ] t1" in lines and not any("→ claimed" in ln for ln in lines)


def test_next_items_failed_job_creation_restores_both_lines(git_repo, factory):
    from ajantrik.core.engine import Engine

    factory()
    path = _three_item_backlog(git_repo, "- [ ] t0", "- [ ] t1", "- [ ] t2")
    before = path.read_bytes()
    claim = tix.claim_items(git_repo, [0, 2])
    assert path.read_bytes() != before
    with pytest.raises(FactoryError):
        Engine(git_repo).new_job(claim["request"], claim["key"], "no-such-branch")
    tix.release_items(git_repo, claim["items"])
    assert path.read_bytes() == before


def test_next_items_uses_the_single_key_and_base_of_the_items(git_repo, factory):
    from ajantrik.core.engine import Engine

    factory()
    _three_item_backlog(git_repo, "- [ ] GEN-4 · base: main · a", "- [ ] b", "- [ ] GEN-4 · c")
    claim = tix.claim_items(git_repo, [0, 2])
    job = Engine(git_repo).new_job(claim["request"], claim["key"], claim["base"])
    assert job["ticket"] == "GEN-4" and job["base"] == "main" and job["request"] == "a; c"


def test_next_items_cli_flag_rejects_non_numbers():
    from ajantrik.kit.cli import _indices, build_parser

    assert _indices("0,2") == [0, 2] and _indices("3") == [3]
    for bad in ("x", "1,x", "-1", "", "1,,2"):
        with pytest.raises(argparse.ArgumentTypeError):
            _indices(bad)
    assert build_parser().parse_args(["next", "--items", "0,2"]).items == [0, 2]
    assert build_parser().parse_args(["next"]).items is None
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["next", "--items", "x"])
    assert e.value.code != 0


def test_next_items_cli_claims_creates_and_drives_one_job(git_repo, factory, monkeypatch):
    from ajantrik.editions.headless.runner import Runner
    from ajantrik.kit.cli import main

    factory()
    path = _three_item_backlog(git_repo, "- [ ] t0", "- [ ] t1", "- [ ] t2")
    driven: list[str] = []

    async def fake_drive(self, job_id):
        driven.append(job_id)
        return {"id": job_id}

    monkeypatch.setattr(Runner, "drive", fake_drive)
    assert main(["--root", str(git_repo), "next", "--items", "0,2"]) == 0
    assert len(driven) == 1
    lines = path.read_text().splitlines()
    assert f"- [x] t0 → {driven[0]}" in lines and f"- [x] t2 → {driven[0]}" in lines and "- [ ] t1" in lines


def test_next_items_cli_failure_exits_non_zero_and_restores_the_file(git_repo, factory, monkeypatch):
    from ajantrik.editions.headless.runner import Runner
    from ajantrik.kit.cli import main

    factory()
    path = _three_item_backlog(git_repo, "- [ ] base: no-such-branch · t0", "- [ ] t1", "- [x] t2 → 001-x")
    before = path.read_bytes()

    async def fake_drive(self, job_id):
        raise AssertionError("must not run")

    monkeypatch.setattr(Runner, "drive", fake_drive)
    assert main(["--root", str(git_repo), "next", "--items", "0"]) == 1  # job creation fails: bad base
    assert path.read_bytes() == before
    assert main(["--root", str(git_repo), "next", "--items", "0,2"]) == 1  # a done item is refused
    assert path.read_bytes() == before


# ---- loop graph ---------------------------------------------------------------------------------------
def test_next_edges_set_the_run_order_not_the_list():
    from ajantrik.core.engine import Engine  # noqa: F401  (engine and schema share ajantrik.core.graph)

    c = cfg(loop={"start": "a", "stages": [
        {"id": "b", "kind": "builders", "next": "g"},
        {"id": "a", "kind": "agent", "agent": "x", "next": "b"},
        {"id": "g", "kind": "gate", "next": "end"},
        {"id": "lonely", "kind": "agent", "agent": "y"},
    ]})
    assert c.loop.order() == ["a", "b", "g"]
    assert any("never run: lonely" in w for w in warnings(c))


@pytest.mark.parametrize("loop, message", [
    ({"stages": [{"id": "a", "kind": "agent", "agent": "x", "next": "b"},
                 {"id": "b", "kind": "agent", "agent": "x", "next": "a"}]}, "loop back"),
    ({"stages": [{"id": "a", "kind": "agent", "agent": "x", "next": "zz"}]}, "not a stage"),
    ({"start": "zz", "stages": [{"id": "a", "kind": "agent", "agent": "x"}]}, "loop.start"),
    ({"stages": [{"id": "d", "kind": "delivery", "next": "a"}, {"id": "a", "kind": "agent", "agent": "x"}]},
     "must be last"),
    ({"stages": [{"id": "end", "kind": "approval"}]}, "reserved"),
])
def test_graph_rules(loop, message):
    with pytest.raises(ValidationError, match=message):
        cfg(loop=loop)


def test_engine_follows_success_edges(git_repo, factory):
    from ajantrik.core.engine import Engine

    stages = [{"id": "design", "kind": "agent", "agent": "architect", "output": "design.md", "next": "end"},
              {"id": "spec", "kind": "agent", "agent": "spec-writer", "output": "spec.md", "next": "design",
               "pos": {"x": 10, "y": 20}}]
    factory(loop={"start": "spec", "stages": stages}, builders=[{"id": "main"}])
    eng = Engine(git_repo)
    assert eng.order == ["spec", "design"]
    job = eng.new_job("Add a multiply function", skip_preflight=True)
    assert job["stage"] == "spec"
    (git_repo / ".factory" / "jobs" / job["id"] / "spec.md").write_text("# spec\n")
    assert eng.advance(job["id"])["stage"] == "design"
    (git_repo / ".factory" / "jobs" / job["id"] / "design.md").write_text("# design\n")
    assert eng.advance(job["id"])["stage"] == "ready"
