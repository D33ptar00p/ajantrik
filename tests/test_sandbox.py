"""The sandbox: srt and Claude Code settings, render, and that development and testing (agents' Bash,
the file tools, worktree setup, gate steps, the codex process) run inside it. The conftest puts a
pass-through `srt` on PATH that logs every call; `-m live` runs the real one."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ajantrik.core import sandbox as sbx
from ajantrik.editions.headless.backends.cli_agents import CodexCliBackend
from ajantrik.editions.headless.backends.tools import Toolbox
from ajantrik.editions.headless.runner import Runner, system_prompt
from ajantrik.kit.cli import check_sandbox
from ajantrik.kit.schema import FactoryConfig, warnings

from .fakes import FakeBackend
from .test_backends import fake_cli, make_run
from .test_e2e_loop import PYTEST_STEP


def calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []


def default_sb(**kw) -> dict:
    return FactoryConfig.model_validate({"repo": {"name": "x"}, "sandbox": kw}).sandbox.model_dump()


# ---- config -------------------------------------------------------------------------------------
def test_on_by_default_and_fails_closed():
    sb = FactoryConfig.model_validate({"repo": {"name": "x"}}).sandbox
    assert sb.enabled and sb.fail_if_unavailable and sb.apply.gate and sb.apply.codex
    assert "~/.ssh" in sb.filesystem.deny_read and ".env" in sb.filesystem.deny_write
    assert not sb.allow_unsandboxed_commands


def test_srt_settings_use_srt_keys():
    sb = default_sb(network={"allowed_domains": ["pypi.org"], "denied_domains": ["evil.example"]},
                    filesystem={"allow_write": ["~/.cache/pip"]}, ignore_violations={"npm *": ["/usr/lib"]})
    s = sbx.srt_settings(sb, ["/wt", "/job"], ["api.openai.com", "pypi.org"])
    assert s["network"] == {"allowedDomains": ["pypi.org", "api.openai.com"], "deniedDomains": ["evil.example"],
                            "allowLocalBinding": False}  # unset optional keys are left out
    assert s["filesystem"]["allowWrite"] == ["/wt", "/job", "~/.cache/pip"]
    assert s["filesystem"]["denyRead"][0] == "~/.ssh" and ".env" in s["filesystem"]["denyWrite"]
    assert s["ignoreViolations"] == {"npm *": ["/usr/lib"]}
    assert s["enableWeakerNestedSandbox"] is False and s["filesystem"]["allowRead"] == []
    # srt refuses a file without these lists, even when they are empty.
    empty = sbx.srt_settings(default_sb(network={"allowed_domains": []}, filesystem={"deny_read": []}))
    assert empty["network"]["allowedDomains"] == [] and empty["network"]["deniedDomains"] == []
    assert set(empty["filesystem"]) == {"denyRead", "allowRead", "allowWrite", "denyWrite"}


def test_claude_settings_and_the_factory_cli_outside():
    c = sbx.claude_settings(default_sb(excluded_commands=["docker *"], network={"http_proxy_port": 8888}), ["/wt"])
    assert c["enabled"] and c["failIfUnavailable"] and c["autoAllowBashIfSandboxed"]
    assert c["allowUnsandboxedCommands"] is False
    assert c["excludedCommands"] == ["python3 .factory/factory.py *", "docker *"]
    assert c["network"]["httpProxyPort"] == 8888 and "/wt" in c["filesystem"]["allowWrite"]
    assert sbx.claude_settings(default_sb(enabled=False)) == {"enabled": False}


def test_warnings_for_a_weaker_or_absent_sandbox():
    def w(**sb):
        return " ".join(warnings(FactoryConfig.model_validate({"repo": {"name": "x"}, "sandbox": sb})))

    assert "sandbox" not in w()
    assert "sandbox is off" in w(enabled=False)
    assert "apply.gate is off" in w(apply={"gate": False})
    assert "retry a blocked command" in w(allow_unsandboxed_commands=True)
    assert "enable_weaker_nested_sandbox" in w(enable_weaker_nested_sandbox=True)
    assert "any host" in w(network={"allowed_domains": ["*"]})


def test_render_owns_the_sandbox_block(factory, git_repo):
    settings = git_repo / ".claude" / "settings.json"
    factory()
    data = json.loads(settings.read_text())
    sb = data["sandbox"]
    assert sb["enabled"] and str(git_repo.parent / "demo-factory") in sb["filesystem"]["allowWrite"]
    assert str(git_repo / ".factory" / "jobs") in sb["filesystem"]["allowWrite"]
    deny = data["permissions"]["deny"]
    assert "Edit(.env)" in deny and "Edit(.claude/**)" in deny and "Read(~/.ssh/**)" in deny
    # A user's own rule stays; a rule the old sandbox config added and the new one dropped goes.
    data["permissions"]["deny"].append("Bash(rm -rf *)")
    data["sandbox"] = {"enabled": True, "stale": 1}
    settings.write_text(json.dumps(data))
    factory(sandbox={"enabled": True, "filesystem": {"deny_write": [".env"]}})
    data = json.loads(settings.read_text())
    assert "stale" not in data["sandbox"]
    assert "Bash(rm -rf *)" in data["permissions"]["deny"] and "Edit(.claude/**)" not in data["permissions"]["deny"]
    assert "Read(~/.ssh/**)" in data["permissions"]["deny"]  # the guard's own DENY list is never taken out
    factory(sandbox={"enabled": False})
    data = json.loads(settings.read_text())
    assert data["sandbox"] == {"enabled": False} and "Edit(.env)" not in data["permissions"]["deny"]
    assert ".sandbox/" in (git_repo / ".factory" / ".gitignore").read_text()


def test_skill_and_agents_are_told(factory, git_repo):
    p = factory()
    assert "Commands run in a sandbox" in (git_repo / ".claude" / "skills" / "factory" / "SKILL.md").read_text()
    loaded = p.load()
    assert loaded.cfg is not None
    agent = loaded.agents["spec-writer"]
    assert "OS sandbox" in system_prompt(loaded.cfg, agent, loaded.skills)
    factory(sandbox={"enabled": False})
    assert "Commands run in a sandbox" not in (git_repo / ".claude" / "skills" / "factory" / "SKILL.md").read_text()


# ---- wrap -----------------------------------------------------------------------------------------
def test_wrap(tmp_path):
    path = tmp_path / "s.json"
    argv = sbx.wrap(["bash", "-c", "true"], default_sb(), path, ["/wt"])
    assert argv[1:] == ["--settings", str(path), "bash", "-c", "true"] and Path(argv[0]).name == "srt"
    assert json.loads(path.read_text())["filesystem"]["allowWrite"][0] == "/wt"
    assert sbx.wrap(["x"], default_sb(enabled=False), path) == ["x"]
    assert sbx.wrap(["x"], None, path) == ["x"]
    with pytest.raises(sbx.SandboxUnavailable, match="not found on PATH: no-such-srt"):
        sbx.wrap(["x"], default_sb(binary="no-such-srt"), path)
    said: list[str] = []
    assert sbx.wrap(["x"], default_sb(binary="no-such-srt", fail_if_unavailable=False), path, say=said.append) == ["x"]
    assert "without the sandbox" in said[0]


def test_settings_live_where_sandboxed_code_cannot_write(tmp_path):
    path = sbx.settings_path(tmp_path, "001-a/b c")
    assert path.parent == tmp_path / ".factory" / ".sandbox" and path.name == "001-a_b_c.json"


def test_doctor_check(factory):
    p = factory()
    cfg = p.load().cfg
    assert check_sandbox(cfg)["ok"]
    cfg2 = FactoryConfig.model_validate({"repo": {"name": "x"}, "sandbox": {"binary": "no-such-srt"}})
    r = check_sandbox(cfg2)
    assert not r["ok"] and "npm i -g @anthropic-ai/sandbox-runtime" in r["detail"]
    assert check_sandbox(FactoryConfig.model_validate({"repo": {"name": "x"}, "sandbox": {"enabled": False}}))["ok"]


# ---- development: agents' commands and file tools -------------------------------------------------
def test_api_bash_runs_in_the_sandbox(factory, fake_srt):
    p = factory()
    run = make_run(p, agent="builder-main")
    box = Toolbox(p.root, run.worktree, run.agent, run.tools, sandbox=default_sb(), job_dir=run.job_dir)
    out, err = box.call("Bash", {"command": "echo hi"})
    assert not err and "hi" in out
    c = calls(fake_srt)[-1]
    assert c["argv"] == ["bash", "-c", "echo hi"] and c["cwd"] == str(run.worktree)
    assert c["settings"]["filesystem"]["allowWrite"][:2] == [str(run.worktree), str(run.job_dir)]
    assert (p.root / ".factory" / ".sandbox" / f"{run.job_dir.name}-builder-main.json").is_file()

    n = len(calls(fake_srt))
    off = Toolbox(p.root, run.worktree, run.agent, run.tools, sandbox=default_sb(enabled=False), job_dir=run.job_dir)
    assert off.call("Bash", {"command": "echo hi"})[1] is False and len(calls(fake_srt)) == n

    gone = Toolbox(p.root, run.worktree, run.agent, run.tools, sandbox=default_sb(binary="no-such-srt"),
                   job_dir=run.job_dir)
    out, err = gone.call("Bash", {"command": "echo hi"})
    assert err and "sandbox unavailable" in out


def test_api_file_tools_keep_to_the_sandbox_rules(factory, tmp_path):
    p = factory()
    run = make_run(p, agent="builder-main")
    sb = default_sb(filesystem={"deny_write": ["fixtures/golden.json", "vendor/"], "deny_read": [str(tmp_path / "private")],
                                "allow_read": [str(tmp_path / "private" / "ok.txt")]})
    box = Toolbox(p.root, run.worktree, run.agent, run.tools, sandbox=sb, job_dir=run.job_dir)
    out, err = box.call("Write", {"file_path": "app/mul.py", "content": "x"})
    assert not err, out
    for target in ("fixtures/golden.json", "vendor/lib.py"):
        out, err = box.call("Write", {"file_path": target, "content": "x"})
        assert err and "deny_write" in out, out
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "ok.txt").write_text("fine")
    (tmp_path / "private" / "no.txt").write_text("secret")
    box.readable_roots.append(tmp_path)
    assert "deny_read" in box.call("Read", {"file_path": str(tmp_path / "private" / "no.txt")})[0]
    assert "fine" in box.call("Read", {"file_path": str(tmp_path / "private" / "ok.txt")})[0]


def test_codex_process_runs_in_the_sandbox(factory, tmp_path, fake_srt, monkeypatch):
    p = factory()
    run = make_run(p)
    events = [{"type": "thread.started", "thread_id": "th-1"}, {"type": "turn.completed"}]
    binary = fake_cli(tmp_path, "codex", events)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gw.example.com/v1")
    result = asyncio.run(CodexCliBackend(str(binary), os_sandbox=default_sb()).run(run))
    assert result.ok, result.error
    c = calls(fake_srt)[-1]
    assert c["argv"][0] == str(binary) and c["argv"][1] == "exec"
    assert c["argv"][c["argv"].index("--sandbox") + 1] == ("danger-full-access" if sys.platform == "darwin"
                                                           else "workspace-write")
    hosts = c["settings"]["network"]["allowedDomains"]
    assert "api.openai.com" in hosts and "gw.example.com" in hosts and "pypi.org" in hosts

    monkeypatch.setattr(sbx, "platform", lambda: "darwin")
    assert CodexCliBackend(str(binary), os_sandbox=default_sb()).sandbox == "danger-full-access"
    assert CodexCliBackend(str(binary), os_sandbox=default_sb(enabled=False)).sandbox == "workspace-write"
    result = asyncio.run(CodexCliBackend(str(binary), os_sandbox=default_sb(binary="no-such-srt")).run(run))
    assert not result.ok and "sandbox unavailable" in result.error


# ---- testing: worktree setup and the gate ---------------------------------------------------------
def runner(project, backend):
    return Runner(project, backend=backend, say=lambda m: None)


def test_setup_and_gate_run_in_the_sandbox(factory, git_repo, fake_srt):
    p = factory(gate__steps=PYTEST_STEP, worktree=[{"kind": "run", "cmd": "echo setup > .setup-ran"}])
    r = runner(p, FakeBackend())
    job = r.run("Add a multiply function")
    wt = job["worktree"]
    setup = [c for c in calls(fake_srt) if c["argv"][-1].startswith("echo setup")]
    assert setup and setup[0]["cwd"] == wt and setup[0]["settings"]["filesystem"]["allowWrite"][0] == wt

    job = r.go(job["id"], by="tester")
    assert job["gate"]["tests"] == "PASS", job["note"]
    gate = [c for c in calls(fake_srt) if "pytest" in c["argv"][-1]]
    assert gate and gate[0]["cwd"] == wt
    allowed = gate[0]["settings"]["filesystem"]["allowWrite"]
    assert allowed[0] == wt and allowed[1].endswith("/round-1")


def test_gate_outside_the_sandbox_when_turned_off(factory, fake_srt):
    p = factory(gate__steps=PYTEST_STEP, sandbox={"enabled": True, "apply": {"gate": False}})
    r = runner(p, FakeBackend())
    job = r.go(r.run("Add a multiply function")["id"], by="tester")
    assert job["gate"]["tests"] == "PASS"
    assert not [c for c in calls(fake_srt) if "pytest" in c["argv"][-1]]


def test_gate_fails_closed_without_the_sandbox(factory):
    p = factory(gate__steps=PYTEST_STEP, sandbox={"enabled": True, "binary": "no-such-srt"})
    r = runner(p, FakeBackend())
    job = r.go(r.run("Add a multiply function")["id"], by="tester")
    assert job["stage"] == "needs-human" and "sandbox unavailable" in job["note"]


# ---- the real srt (opt-in) ------------------------------------------------------------------------
def real_srt(fake_dir: Path) -> str | None:
    path = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep) if Path(d) != fake_dir)
    return shutil.which("srt", path=path)


@pytest.mark.live
def test_real_srt_denies_writes_and_hosts(tmp_path, fake_srt, monkeypatch):
    binary = real_srt(fake_srt.parent)
    if not binary:
        pytest.skip("srt is not installed (npm i -g @anthropic-ai/sandbox-runtime)")
    monkeypatch.undo()  # the real platform dependency check, the real PATH
    sb = default_sb(binary=binary, network={"allowed_domains": []})
    wt = tmp_path / "wt"
    wt.mkdir()
    outside = tmp_path / "outside.txt"

    def run(cmd: str) -> subprocess.CompletedProcess[str]:
        argv = sbx.wrap(["bash", "-c", cmd], sb, tmp_path / "srt.json", [wt])
        return subprocess.run(argv, cwd=wt, capture_output=True, text=True, timeout=120)

    assert run("echo ok > inside.txt").returncode == 0 and (wt / "inside.txt").is_file()
    run(f"echo no > {outside}")
    assert not outside.exists()
    assert run("curl -sS --max-time 10 https://example.com").returncode != 0
