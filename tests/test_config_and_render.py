"""Schema rules, routing, rendering (the default preset against this repo's own factory), tickets and backlog."""

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
from ajantrik.kit.schema import FactoryConfig, default_stages, migrate, warnings

from .conftest import sh

REPO = Path(__file__).resolve().parents[1]


def cfg(**kw) -> FactoryConfig:
    return FactoryConfig.model_validate({"repo": {"name": "x"}, **kw})


# ---- schema ------------------------------------------------------------------------------------------
def test_defaults_are_the_hand_built_loop():
    c = cfg()
    assert c.version == 2
    assert [s.id for s in c.loop.stages] == ["spec", "design", "approval", "tests", "main", "gate", "code",
                                              "security", "integrate"]
    assert [(p.id, p.title) for p in c.loop.phases] == [
        ("plan", "Plan"), ("build", "Build"), ("guardrails", "Guardrails"), ("review", "Review"), ("integrate", "Integrate")]
    assert c.stage("gate").next == ["code", "security"]  # type: ignore[union-attr]  # the edges fan out
    assert [[s.id for s in g] for g in c.loop.groups()] == [
        ["spec"], ["design"], ["approval"], ["tests"], ["main"], ["gate"], ["code", "security"], ["integrate"]]
    tests, main = c.stage("tests"), c.stage("main")
    assert tests and tests.phase == "build" and tests.role == "plain"  # test-author writes tests.md
    assert main and main.role == "builder" and main.agent == "builder-main"
    sec = c.stage("security")
    assert sec and sec.role == "reviewer" and sec.agent == "reviewer-security" and sec.chip == "sec" and not sec.routed
    assert [(s.id, s.on_fail) for s in c.loop.stages if s.on_fail] == [
        ("gate", "main"), ("code", "main"), ("security", "main")]  # the failure loops are part of the default
    integrate = c.stage("integrate")
    assert integrate and integrate.role == "integrator" and integrate.output == "decision.md"
    assert c.repo.title == "x Software Factory" and c.delivery.mode == "none"


@pytest.mark.parametrize("stages, message", [
    ([{"id": "deliver", "kind": "approval"}], "reserved"),
    ([{"id": "g", "kind": "gate", "on_fail": "z"}], "not a stage"),
    ([{"id": "g", "kind": "gate", "on_fail": "x"}, {"id": "x", "kind": "agent", "agent": "x"}], "earlier stage"),
    ([{"id": "a", "kind": "agent", "agent": "x"}, {"id": "a", "kind": "approval"}], "duplicate"),
    ([{"id": "ready", "kind": "approval"}], "reserved"),
    ([{"id": "Bad Id", "kind": "approval"}], "kebab"),
    ([{"id": "a", "kind": "agent"}], "needs `agent`"),
    ([{"id": "a", "kind": "agent", "agent": "x", "phase": "nope"}], "not in loop.phases"),
    ([{"id": "a", "kind": "agent", "role": "builder", "output": "a.md"}], "writes its own file"),
    ([{"id": "a", "kind": "gate", "role": "builder"}], "for agent stages"),
    ([{"id": "g", "kind": "gate", "steps": ["lint"]}], "unknown steps"),
    ([{"id": "a", "kind": "agent", "agent": "x", "area_globs": ["src/"]}], "for builders"),
    ([{"id": "a", "kind": "agent", "role": "builder", "phase": "build", "path_globs": ["src/"]}], "for reviewers"),
    ([{"id": "a", "kind": "agent", "phase": "build", "output": "a.md", "agent": "x", "area_globs": ["src/"]}],
     "for builders"),
    ([{"id": "r", "kind": "agent", "role": "reviewer", "phase": "review", "builder": "zz"}], "not a builder stage"),
    ([{"id": "a", "kind": "approval", "phase": "build"}], "Needs human"),
    ([{"id": "a", "kind": "approval", "agent": "x"}], "for agent stages"),
    ([{"id": "a", "kind": "agent", "agent": "x", "on_fail": "a"}], "only gate and reviewer"),
    ([{"id": "i", "kind": "agent", "role": "integrator", "phase": "integrate", "agent": "x"},
      {"id": "j", "kind": "agent", "role": "integrator", "phase": "integrate", "agent": "x"}], "at most one integrate"),
])
def test_loop_rules(stages, message):
    with pytest.raises(ValidationError, match=message):
        cfg(loop={"stages": stages})


def test_a_gate_can_sit_in_any_phase_with_its_own_steps():
    c = cfg(gate={"steps": [{"key": "lint", "cmd": "true"}, {"key": "unit", "cmd": "true"}]},
            loop={"stages": [
                {"id": "spec", "kind": "agent", "agent": "spec-writer"},
                {"id": "lint-early", "kind": "gate", "phase": "plan", "steps": ["lint"], "next": ["a", "b"]},
                {"id": "a", "kind": "agent", "role": "builder", "phase": "build", "next": "gate"},
                {"id": "b", "kind": "agent", "role": "builder", "phase": "build"},
                {"id": "gate", "kind": "gate"},
            ]})
    assert [[s.id for s in g] for g in c.loop.groups()] == [["spec"], ["lint-early"], ["a", "b"], ["gate"]]
    assert c.stage("spec").phase == "plan" and c.stage("gate").phase == "guardrails"  # type: ignore[union-attr]
    assert any("lint-early: no builder before it" in w for w in warnings(c))


def test_phases_are_only_board_columns():
    c = cfg(loop={"phases": [{"id": "plan"}, {"id": "build", "parallel": True}, {"id": "qa-checks"}], "stages": [
        {"id": "spec", "kind": "agent", "agent": "spec-writer"},
        {"id": "a", "kind": "agent", "agent": "builder-a", "phase": "build"},
        {"id": "b", "kind": "agent", "role": "builder", "phase": "qa-checks"},
        {"id": "g", "kind": "gate"}]})
    assert [(p.id, p.title) for p in c.loop.phases] == [("plan", "Plan"), ("build", "Build"), ("qa-checks", "Qa checks")]
    # what a stage does comes from its agent (or role), not its phase; phases default to the first / review / last
    assert [(s.id, s.phase, s.role) for s in c.loop.stages] == [
        ("spec", "plan", "plain"), ("a", "build", "builder"), ("b", "qa-checks", "builder"), ("g", "qa-checks", None)]
    assert [[s.id for s in g] for g in c.loop.groups()] == [["spec"], ["a"], ["b"], ["g"]]  # no fan-out
    with pytest.raises(ValidationError, match="duplicate phases"):
        cfg(loop={"phases": [{"id": "plan"}, {"id": "plan"}]})
    with pytest.raises(ValidationError, match=r"not in loop\.phases"):
        cfg(loop={"phases": [{"id": "plan"}], "stages": [{"id": "a", "kind": "agent", "agent": "x", "phase": "nope"}]})
    with pytest.raises(ValidationError, match="at least one phase"):
        cfg(loop={"phases": []})


@pytest.mark.parametrize("stages, message", [
    ([{"id": "s", "kind": "agent", "agent": "x", "next": ["a", "g"]}, {"id": "a", "kind": "agent", "role": "builder", "phase": "build",
      "next": "end"}, {"id": "g", "kind": "gate"}], "only agents run in parallel"),
    ([{"id": "s", "kind": "agent", "agent": "x", "next": ["a", "b"]}, {"id": "a", "kind": "agent", "role": "builder", "phase": "build",
      "next": "end"}, {"id": "b", "kind": "agent", "role": "builder", "phase": "build"}, {"id": "c", "kind": "agent", "agent": "x"}],
     "must all lead to the same next stage"),
    ([{"id": "s", "kind": "agent", "agent": "x", "next": ["a", "end"]}, {"id": "a", "kind": "agent", "agent": "x"}],
     "distinct stages"),
])
def test_fan_out_rules(stages, message):
    with pytest.raises(ValidationError, match=message):
        cfg(loop={"stages": stages})


def test_a_fan_out_can_lead_straight_into_another():
    c = cfg(loop={"stages": [
        {"id": "s", "kind": "agent", "agent": "x", "next": ["a", "b"]},
        {"id": "a", "kind": "agent", "role": "builder", "phase": "build", "next": ["c", "d"]},
        {"id": "b", "kind": "agent", "role": "builder", "phase": "build", "next": ["c", "d"]},
        {"id": "c", "kind": "agent", "role": "reviewer", "phase": "review", "next": "end"},
        {"id": "d", "kind": "agent", "role": "reviewer", "phase": "review"}]})
    assert [[s.id for s in g] for g in c.loop.groups()] == [["s"], ["a", "b"], ["c", "d"]]


def test_version_1_configs_become_agents_in_phases():
    v1 = {"repo": {"name": "x"}, "version": 1,
          "builders": [{"id": "py", "area_globs": ["app/"]}, {"id": "ui", "agent": "builder-front"}],
          "reviewers": {"always": ["code"], "routed": [
              {"id": "ui", "path_globs": ["web/"], "builder": "ui"}]},
          "gate": {"steps": [{"key": "unit", "cmd": "true", "builder": "py"}]},
          "ui": {"build_label": "py ∥ ui"},
          "loop": {"stages": [
              {"id": "spec", "kind": "agent", "agent": "spec-writer", "output": "spec.md"},
              {"id": "approval", "kind": "approval"},
              {"id": "build", "kind": "builders", "title": "Build"},
              {"id": "gate", "kind": "gate"},
              {"id": "review", "kind": "reviewers", "on_fail": "build"},
              {"id": "integrate", "kind": "integrate", "agent": "integrator"},
              {"id": "deliver", "kind": "delivery"}]}}
    data = migrate(v1)
    assert "builders" not in data and "reviewers" not in data and "build_label" not in data["ui"]
    assert v1["gate"]["steps"][0]["builder"] == "py"  # the input is not changed
    c = FactoryConfig.model_validate(v1)
    assert c.loop.order() == ["spec", "approval", "py", "ui", "gate", "code", "review-ui", "integrate"]
    assert [[s.id for s in g] for g in c.loop.groups()] == [
        ["spec"], ["approval"], ["py", "ui"], ["gate"], ["code", "review-ui"], ["integrate"]]
    assert c.stage("approval").next == ["py", "ui"] and c.stage("py").next == "gate"  # type: ignore[union-attr]
    assert [c.stage(i).phase for i in ("spec", "py", "gate", "code")] == ["plan", "build", "guardrails", "review"]  # type: ignore[union-attr]
    ui = c.stage("ui")
    assert ui and ui.role == "builder" and ui.agent == "builder-front"
    rui = c.stage("review-ui")
    assert rui and rui.role == "reviewer" and rui.agent == "reviewer-ui" and rui.chip == "ui" and rui.builder == "ui"
    assert rui.on_fail == "py" and rui.routed
    assert c.stage("gate").on_fail == "py" and c.stage("code").on_fail == "py"  # type: ignore[union-attr]
    assert c.gate.steps[0].builder == "py"
    integrate = c.stage("integrate")
    assert integrate and integrate.role == "integrator" and integrate.output == "decision.md"
    assert FactoryConfig.model_validate(c.model_dump()) == c  # version 2 is left alone


def test_credentials_must_be_references():
    with pytest.raises(ValidationError, match="never the value"):
        cfg(connections={"tickets": {"provider": "jira", "token_ref": "ATATT3xFfGF0abc"}})
    with pytest.raises(ValidationError, match="never the value"):
        cfg(runtime={"api": {"api_key_ref": "sk-ant-xyz"}})
    c = cfg(connections={"tickets": {"provider": "jira", "project": "PROJ", "token_ref": "env:JIRA_TOKEN",
                                     "site": "https://x.atlassian.net"}})
    assert c.connections.tickets.jql and "project = PROJ" in c.connections.tickets.jql
    assert c.connections.tickets.browse_url == "https://x.atlassian.net/browse/"


def test_warnings_for_risky_choices():
    stages = [s for s in cfg().model_dump()["loop"]["stages"] if s["kind"] != "approval"]
    w = warnings(cfg(loop={"stages": stages}, delivery={"mode": "direct", "require_approval": False}))
    assert any("direct without require_approval" in x for x in w)
    assert any("no human approval at all" in x for x in w)
    assert any("no Codex" in x or "no tool hooks" in x for x in warnings(cfg(runtime={"kind": "codex-cli"})))


def test_delivery_pos_is_layout_only_and_optional():
    assert cfg().delivery.pos is None
    assert cfg(delivery={"mode": "pr"}).delivery.pos is None
    c = cfg(delivery={"mode": "pr", "pos": {"x": 10, "y": 20}})
    assert c.delivery.pos is not None and (c.delivery.pos.x, c.delivery.pos.y) == (10, 20)
    with pytest.raises(ValidationError):
        cfg(delivery={"mode": "pr", "bogus": 1})
    with pytest.raises(ValidationError):
        cfg(delivery={"mode": "pr", "pos": {"x": "left", "y": 2}})


def test_minimal_loop_without_gate_or_reviewers():
    c = cfg(loop={"stages": [{"id": "main", "kind": "agent", "role": "builder", "phase": "build"}]})
    assert any("no gate" in w for w in warnings(c))
    assert any("no review agent" in w for w in warnings(c))


# ---- routing ------------------------------------------------------------------------------------------
def test_glob_semantics():
    assert glob_match("desktop_gui/app_calib_view.py", "app_calib*")
    assert glob_match("pyside_rc/qml/Main.qml", "pyside_rc/qml/")
    assert glob_match("a/b/pyside_rc/qml/x.qml", "pyside_rc/qml/")
    assert glob_match("src/pages/billing/Plan.tsx", "src/**/billing/*")
    assert not glob_match("src/app.ts", "*.tsx")


def routed(*reviewers: dict, **kw) -> dict:
    """The default loop plus routed reviewers (fed from the gate, back to `main` on failure)."""
    stages = [s.model_dump(exclude_defaults=True) | {"kind": s.kind} for s in default_stages()]
    for st in stages:
        if st["id"] == "gate":
            st["next"] = [*st["next"], *(r["id"] for r in reviewers)]
        elif st.get("role") == "reviewer":
            st["next"] = "integrate"
    extra = [{"kind": "agent", "phase": "review", "role": "reviewer", "next": "integrate", "on_fail": "main", **r}
             for r in reviewers]
    at = next(i for i, st in enumerate(stages) if st["id"] == "integrate")
    return cfg(loop={"stages": [*stages[:at], *extra, *stages[at:]]}, **kw).model_dump()


def test_routing_by_paths_diff_patterns_and_scoped_patterns():
    qml = {"path_globs": ["pyside_rc/qml/", "*.qrc"],
           "scoped_patterns": [{"paths": ["desktop_gui/"], "pattern": r"@Property|Signal\(|@Slot"}]}
    c = routed({"id": "calib", "path_globs": ["app_calib*", "app/settings/"]},
               {"id": "xplat", "path_globs": ["main.py"], "diff_patterns": ["subprocess", r"sys\.platform"]},
               {"id": "ui", **qml}, {"id": "ux", **qml})
    r = route_reviewers(c, ["desktop_gui/app_calib_panel.py"], {})
    assert r["calib"] and not r["xplat"] and r["code"] == "always"
    r = route_reviewers(c, ["desktop_gui/widgets.py"], {"desktop_gui/widgets.py": ["  subprocess.run(x)"]})
    assert r["xplat"] and not r["calib"]
    r = route_reviewers(c, ["desktop_gui/model.py"], {"desktop_gui/model.py": ["    @Property(int)"]})
    assert r["ui"] and r["ux"]
    r = route_reviewers(c, ["pyside_rc/qml/Panel.qml"], {})
    assert r["ui"] and r["ux"] and not r["calib"]


def test_routing_exclusions_metrics_and_refusals():
    ui = {"path_globs": ["src/components/*.tsx"], "exclude_globs": ["__test__/", "__tests__/"]}
    c = routed({"id": "review-ui", "agent": "reviewer-ui", **ui}, {"id": "ux", **ui},
               security={"refuse_patterns": [{"paths": ["package.json"],
                                              "pattern": r'"(preinstall|install|postinstall|prepare)"\s*:'}]})
    r = route_reviewers(c, ["src/components/__tests__/Button.test.tsx"], {})
    assert not r["review-ui"] and not r["ux"]
    assert route_reviewers(c, ["src/components/Button.tsx"], {})["review-ui"]
    assert refused(c, ["package.json"], {"package.json": ['    "postinstall": "node x.js",']})
    assert not refused(c, ["package.json"], {"package.json": ['    "lodash": "^4.17.21",']})
    c2 = routed({"id": "deploy", "path_globs": ["deploy.sh"], "metrics": [{"key": "data_mb_delta", "gt": 1.0}]})
    assert route_reviewers(c2, ["README.md"], {}, {"data_mb_delta": 2.5})["deploy"]
    assert not route_reviewers(c2, ["README.md"], {}, {"data_mb_delta": 0.1})["deploy"]


# ---- presets and render ----------------------------------------------------------------------------------
def test_every_preset_loads_and_renders(tmp_path):
    names = [x["name"] for x in list_presets()]
    assert names == ["default"]
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


def test_default_preset_is_generic(tmp_path):
    """The default preset carries the generic agents, skills and loop, without any repo's connections."""
    sh("git", "init", "-q", cwd=tmp_path)
    p = Project(tmp_path)
    p.init_from_preset("default", name="demo")
    assert render(p).errors == []
    loaded = p.load()
    assert set(loaded.agents) == {"spec-writer", "architect", "test-author", "builder-main", "reviewer-code",
                                  "reviewer-security", "integrator"}
    assert set(loaded.skills) == {"establish-test-setup", "read-before-edit", "run-checks", "smallest-change",
                                  "test-first"}
    data = p.raw()
    assert data["repo"]["name"] == "demo" and data["delivery"]["mode"] == "none"
    assert data["connections"]["tickets"] == {"provider": "none", "write_back": "none"}
    assert {g["key"] for g in data["gate"]["steps"]} == {"pii", "conflict-markers"}
    assert "env:" not in p.yaml_path.read_text()  # no credential references


@pytest.mark.skipif(not (REPO / ".factory" / "config" / "factory.yaml").is_file(),
                    reason="this repo's own factory is not checked in")
def test_default_preset_matches_this_repos_factory(tmp_path):
    """The default preset is the factory ajantrik runs on itself: same agents, skills and loop."""
    sh("git", "init", "-q", cwd=tmp_path)
    p = Project(tmp_path)
    p.init_from_preset("default", name="demo")
    ours = REPO / ".factory" / "config"
    for sub, pattern in (("agents", "*.md"), ("skills", "*/SKILL.md")):
        want = {f.relative_to(ours / sub): f.read_text() for f in (ours / sub).glob(pattern)}
        got = {f.relative_to(p.config_dir / sub): f.read_text() for f in (p.config_dir / sub).glob(pattern)}
        assert want and got == want, sub
    mine = yaml.safe_load((ours / "factory.yaml").read_text())["loop"]["stages"]
    assert [(s["id"], s["kind"]) for s in p.raw()["loop"]["stages"]] == [(s["id"], s["kind"]) for s in mine]


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
    data["loop"]["stages"] = [s for s in data["loop"]["stages"] if s["id"] != "security"]
    next(s for s in data["loop"]["stages"] if s["id"] == "gate")["next"] = "code"
    p.yaml_path.write_text(yaml.safe_dump(data))
    p.delete_agent("reviewer-security")
    res = render(p)
    assert ".claude/agents/reviewer-security.md" in res.removed
    assert not (git_repo / ".claude" / "agents" / "reviewer-security.md").exists()


def test_skill_md_follows_the_loop(git_repo, factory):
    factory(connections__tickets={"provider": "jira", "mode": "mcp", "site": "https://m.atlassian.net",
                                  "project": "PROJ"}, delivery__mode="pr")
    text = (git_repo / ".claude" / "skills" / "factory" / "SKILL.md").read_text()
    assert "getJiraIssue" in text and "PROJ-123" in text and "/factory sync" in text
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
    issues: ClassVar[dict[str, dict]] = {"PROJ-1": {"key": "PROJ-1", "summary": "Add rename", "type": "Story", "status": "To Do",
                        "status_category": "To Do", "labels": ["factory"], "epic": "PROJ-100"},
              "PROJ-2": {"key": "PROJ-2", "summary": "Fix crash", "type": "Bug", "status": "To Do",
                        "status_category": "To Do", "labels": ["factory"], "epic": None},
              "PROJ-3": {"key": "PROJ-3", "summary": "Export", "type": "Story", "status": "To Do",
                        "status_category": "To Do", "labels": ["factory"], "epic": "PROJ-200"}}

    def __init__(self, conn):
        pass

    def search(self, jql, limit=50):
        return list(self.issues.values())

    def get(self, key):
        return self.issues[key]


def test_sync_and_next(git_repo, factory, monkeypatch):
    monkeypatch.setattr(tix, "Jira", FakeJira)
    sh("git", "push", "-q", "origin", "main:feature/PROJ-100_Projects", cwd=git_repo)
    factory(connections__tickets={"provider": "jira", "mode": "rest", "site": "https://m.atlassian.net",
                                      "project": "PROJ"})
    from ajantrik.core.engine import load_config

    c = load_config(git_repo)
    report = tix.sync(git_repo, c, [])
    assert report == ["PROJ-1: added (base feature/PROJ-100_Projects)", "PROJ-2: added (base main)",
                      "PROJ-3: blocked (epic PROJ-200 has no branch on origin)"]
    assert tix.sync(git_repo, c, [])[0].startswith("PROJ-1: skipped")
    FakeJira.issues["PROJ-1"] = {**FakeJira.issues["PROJ-1"], "status_category": "Done", "status": "Done"}
    item = tix.claim_next(git_repo, c)
    assert item and item["key"] == "PROJ-2" and item["base"] == "main"
    text = (git_repo / ".factory" / "backlog.md").read_text()
    assert "- [x] PROJ-1 · base: feature/PROJ-100_Projects · Add rename → Done" in text
    assert "- [x] PROJ-2 · base: main · Fix crash → claimed" in text
    from ajantrik.core.engine import Engine

    job = Engine(git_repo).new_job(item["text"], item["key"], item["base"])
    tix.mark_claimed(git_repo, item["line"], job["id"])
    assert f"→ {job['id']}" in (git_repo / ".factory" / "backlog.md").read_text()
    assert job["branch"] == "bugfix/PROJ-2_Fix_crash" and job["ticket"] == "PROJ-2"


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
    _three_item_backlog(git_repo, "- [ ] PROJ-4 · base: main · a", "- [ ] b", "- [ ] PROJ-4 · c")
    claim = tix.claim_items(git_repo, [0, 2])
    job = Engine(git_repo).new_job(claim["request"], claim["key"], claim["base"])
    assert job["ticket"] == "PROJ-4" and job["base"] == "main" and job["request"] == "a; c"


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

    factory(runtime__kind="claude-cli")  # Runner refuses the interactive runtime
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

    factory(runtime__kind="claude-cli")  # Runner refuses the interactive runtime
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
        {"id": "b", "kind": "agent", "role": "builder", "phase": "build", "next": "g"},
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
    factory(loop={"start": "spec", "stages": stages})
    eng = Engine(git_repo)
    assert eng.order == ["spec", "design"]
    job = eng.new_job("Add a multiply function", skip_preflight=True)
    assert job["stage"] == "spec"
    (git_repo / ".factory" / "jobs" / job["id"] / "spec.md").write_text("# spec\n")
    assert eng.advance(job["id"])["stage"] == "design"
    (git_repo / ".factory" / "jobs" / job["id"] / "design.md").write_text("# design\n")
    assert eng.advance(job["id"])["stage"] == "ready"
