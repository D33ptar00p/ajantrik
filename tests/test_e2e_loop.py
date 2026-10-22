"""The whole loop, headless, with a scripted backend: stages, approvals, rounds, gate, delivery."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ajantrik.core import jobs
from ajantrik.core.util import FactoryError
from ajantrik.editions.headless.runner import Runner
from ajantrik.kit.schema import default_stages

from .conftest import sh
from .fakes import FakeBackend, commit

PYTEST_STEP = [{"key": "tests", "chip": "test", "cmd": f"{sys.executable} -m pytest -q -p no:cacheprovider "
                "--junitxml=$OUT/junit.xml", "junit": "junit.xml"}]


def runner(project, backend):
    return Runner(project, backend=backend, say=lambda m: None)


def test_full_loop_with_approvals_and_push(factory, git_repo):
    p = factory(gate__steps=PYTEST_STEP, delivery__mode="push", delivery__require_approval=True)
    fake = FakeBackend()
    r = runner(p, fake)
    job = r.run("Add a multiply function")
    assert job["stage"] == "awaiting-approval"
    assert [c.agent for c in fake.calls] == ["spec-writer", "architect"]
    assert job["builders"] == ["main"]

    job = r.go(job["id"], by="tester")
    assert job["stage"] == "awaiting-deliver", job["note"]
    assert job["note"] == "approve delivery (push)"
    assert job["gate"]["tests"] == "PASS" and job["gate"]["secrets"] == "PASS"
    assert job["reviews"] == {"code": "PASS", "security": "PASS"}
    gate = json.loads((jobs.job_dir(git_repo, job["id"]) / "round-1" / "gate.json").read_text())
    assert gate["passed"] == 2 and gate["failed"] == 0

    with pytest.raises(FactoryError, match="needs an approval"):
        r.engine.deliver(job["id"], "tester")
    job = r.go(job["id"], by="tester")
    assert job["stage"] == "delivered"
    assert job["delivery"]["mode"] == "push"
    remote = sh("git", "ls-remote", "--heads", "origin", cwd=git_repo)
    assert job["branch"] in remote


def test_review_changes_resume_builders_then_escalate(factory, git_repo):
    p = factory(loop__max_rounds=2)
    fake = FakeBackend(verdicts={"code": ["CHANGES"]})
    r = runner(p, fake)
    job = r.run("Add a multiply function")
    job = r.go(job["id"], by="tester")
    assert job["stage"] == "needs-human"
    assert "2 rounds used" in job["note"]
    jd = jobs.job_dir(git_repo, job["id"])
    assert (jd / "decision.md").read_text().startswith("ESCALATE")
    builders = [c for c in fake.calls if c.agent == "builder-main"]
    assert len(builders) == 2
    assert builders[0].session_id is None
    assert builders[1].session_id == "s-" + builders[0].name  # resumed with the findings
    assert builders[1].prompt.startswith("Round 1 findings")


def test_gate_failure_uses_a_round_and_builders_fix_it(factory, git_repo):
    def build(wt: Path, rnd: int) -> None:
        calc = wt / "app" / "calc.py"
        text = calc.read_text().replace("\n\ndef mul(a, b):\n    return a + b\n", "")
        body = "a + b" if rnd == 1 else "a * b"  # round 1 is wrong
        calc.write_text(text + f"\n\ndef mul(a, b):\n    return {body}\n")
        commit(wt, f"mul round {rnd}")

    p = factory(gate__steps=PYTEST_STEP, delivery__mode="none")
    r = runner(p, FakeBackend(build=build))
    job = r.go(r.run("Add a multiply function")["id"], by="tester")
    assert job["stage"] == "ready", job["note"]
    assert job["round"] == 2
    jd = jobs.job_dir(git_repo, job["id"])
    assert json.loads((jd / "round-1" / "gate.json").read_text())["tests"] == "FAIL"
    assert (jd / "round-1" / "review-code.md").exists() is False
    step = r.engine.step(job["id"])
    assert step["type"] == "stop" and any("push -u origin" in c for c in step["commands"])


def test_no_human_in_the_loop_delivers_straight_to_main(factory, git_repo):
    stages = [s.model_dump(exclude_defaults=True) | {"kind": s.kind} for s in default_stages()]
    p = factory(loop__stages=[s for s in stages if s["kind"] != "approval"],
                delivery__mode="direct", delivery__require_approval=False,
                delivery__direct={"strategy": "ff-only", "push": True})
    r = runner(p, FakeBackend())
    job = r.run("Add a multiply function")
    assert job["stage"] == "delivered", job["note"]
    head = sh("git", "-C", job["worktree"], "rev-parse", "HEAD", cwd=git_repo)
    assert sh("git", "ls-remote", "origin", "refs/heads/main", cwd=git_repo).split()[0] == head


def test_direct_squash_and_local_base_refusal(factory, git_repo):
    p = factory(delivery__mode="direct", delivery__require_approval=False,
                delivery__direct={"strategy": "squash", "push": False})
    r = runner(p, FakeBackend())
    job = r.go(r.run("Add a multiply function")["id"], by="tester")
    # main is checked out in the user's checkout: moving it would leave that checkout stale.
    assert job["stage"] == "needs-human" and "checked out" in job["note"]

    p = factory(delivery__mode="direct", delivery__require_approval=False,
                delivery__direct={"strategy": "squash", "push": True})
    r = runner(p, FakeBackend())
    before = sh("git", "ls-remote", "origin", "refs/heads/main", cwd=git_repo).split()[0]
    job = r.go(r.run("Add a divide function")["id"], by="tester")
    assert job["stage"] == "delivered", job["note"]
    after = sh("git", "ls-remote", "origin", "refs/heads/main", cwd=git_repo).split()[0]
    sh("git", "fetch", "-q", "origin", cwd=git_repo)
    assert sh("git", "rev-list", "--count", f"{before}..{after}", cwd=git_repo) == "1"  # one squash commit
    assert sh("git", "log", "-1", "--format=%s", after, cwd=git_repo) == "feat: add mul"


def test_pr_delivery_through_gh(factory, git_repo, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    (bin_dir / "gh").write_text(f"#!/bin/sh\necho \"$@\" >> {log}\necho https://github.com/x/demo/pull/7\n")
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    p = factory(delivery__mode="pr", delivery__require_approval=False,
                delivery__pr={"draft": True, "reviewers": ["alice"], "labels": []})
    r = runner(p, FakeBackend())
    job = r.go(r.run("Add a multiply function")["id"], by="tester")
    assert job["stage"] == "delivered"
    assert job["delivery"]["url"] == "https://github.com/x/demo/pull/7"
    args = log.read_text()
    assert "pr create --base main" in args and "--draft" in args and "--reviewer alice" in args


def test_builder_blocked_goes_to_a_human(factory, git_repo):
    r = runner(factory(), FakeBackend(blocked_builder=True))
    job = r.go(r.run("Add a multiply function")["id"], by="tester")
    assert job["stage"] == "needs-human"
    assert "needs a new dependency" in job["note"]


def test_refused_paths_stop_before_integration(factory, git_repo):
    def build(wt: Path, rnd: int) -> None:
        (wt / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        (wt / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        commit(wt, "ci")

    r = runner(factory(), FakeBackend(build=build))
    job = r.go(r.run("Add CI")["id"], by="tester")
    assert job["stage"] == "needs-human"
    assert ".github/workflows/ci.yml" in job["note"]


def test_rework_before_approval_and_after_needs_human(factory, git_repo):
    fake = FakeBackend(verdicts={"code": ["CHANGES"]})
    p = factory(loop__max_rounds=1)
    r = runner(p, fake)
    job = r.run("Add a multiply function")
    job = r.engine.rework(job["id"], "use integers only")
    assert job["stage"] == "design"  # architect re-runs; spec only with --scope
    job = r.resume(job["id"])
    assert job["stage"] == "awaiting-approval"
    assert "Rework: read rework-1.md" in [c for c in fake.calls if c.agent == "architect"][-1].prompt
    # an approval given before the rework would not count: approvals are tied to stageSince
    job = r.go(job["id"], by="tester")
    assert job["stage"] == "needs-human"
    fake.verdicts = {}
    job = r.engine.rework(job["id"], "fix the review findings")
    assert job["stage"] == "main" and job["round_base"] == job["round"]
    job = r.resume(job["id"])
    assert job["stage"] == "ready"
    last_builder = [c for c in fake.calls if c.agent == "builder-main"][-1]
    assert last_builder.session_id is None and "rework-2.md" in last_builder.prompt


def test_vendored_cli_drives_a_job_without_the_package(factory, git_repo):
    factory()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(git_repo), **{k: "t" for k in ("GIT_AUTHOR_NAME",
                                                                             "GIT_COMMITTER_NAME")},
           "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_EMAIL": "t@x"}

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["/usr/bin/python3", ".factory/factory.py", *args], cwd=git_repo, env=env,
                              capture_output=True, text=True)

    r = cli("--json", "new", "Add a multiply function")
    assert r.returncode == 0, r.stderr
    job = json.loads(r.stdout)
    step = json.loads(cli("step", job["id"]).stdout)
    assert step["type"] == "agents" and step["agents"][0]["agent"] == "spec-writer"
    assert "Feature: Add a multiply function" in step["agents"][0]["prompt"]
    r = cli("advance", job["id"])
    assert "needs-human" in r.stdout  # spec-writer never wrote spec.md
    assert cli("status").returncode == 0


def _agents_in_phases(**gate_and_reviews):
    return {"max_rounds": 3,
            "stages": [
                {"id": "spec", "kind": "agent", "agent": "spec-writer", "output": "spec.md", "phase": "plan"},
                {"id": "design", "kind": "agent", "agent": "architect", "output": "design.md", "phase": "plan",
                 "read_builders": True},
                {"id": "approval", "kind": "approval", "next": ["a", "b"]},
                {"id": "a", "kind": "agent", "agent": "builder-main", "phase": "build", "next": "lint"},
                {"id": "b", "kind": "agent", "agent": "builder-main", "phase": "build"},
                {"id": "lint", "kind": "gate", "steps": ["lint"]},
                {"id": "tests", "kind": "gate", "steps": ["tests"], "next": ["code", "security"]},
                {"id": "code", "kind": "agent", "agent": "reviewer-code", "phase": "review", "next": "end",
                 **gate_and_reviews},
                {"id": "security", "kind": "agent", "agent": "reviewer-security", "phase": "review"},
            ]}


def test_fanned_out_agents_run_together_and_findings_go_to_their_owner(factory, git_repo):
    p = factory(gate__steps=[{"key": "lint", "cmd": "true"}, *PYTEST_STEP], delivery__mode="none",
                loop=_agents_in_phases(builder="b"))
    fake = FakeBackend(verdicts={"code": ["CHANGES", "PASS"]})
    r = runner(p, fake)
    job = r.run("Add a multiply function")
    assert [c.agent for c in fake.calls] == ["spec-writer", "architect"]  # plan agents one after the other
    r.engine.approve(job["id"], "tester")
    step = r.engine.step(job["id"])
    assert step["stage"] == "a" and step["parallel"] is True
    assert [s["name"].split("-")[1] for s in step["agents"]] == ["a", "b"]
    job = r.resume(job["id"])
    assert job["stage"] == "ready", job["note"]
    assert job["round"] == 2
    built = [re.search(r"You are builder (\w+)", c.prompt or "") for c in fake.calls if c.agent == "builder-main"]
    assert [m.group(1) for m in built if m] == ["a", "b"]  # round 2 resumed b only: its prompt is the findings
    assert [c.name.split("-")[1] for c in fake.calls if c.agent == "builder-main"] == ["a", "b", "b"]
    gate = json.loads((jobs.job_dir(git_repo, job["id"]) / "round-1" / "gate.json").read_text())
    assert gate["lint"] == "PASS" and gate["tests"] == "PASS"  # two gates, one round, one gate.json
    assert job["reviews"] == {"code": "PASS", "security": "PASS"}


def test_a_gate_before_any_builder_stops_for_a_human(factory, git_repo):
    loop = _agents_in_phases()
    loop["stages"].insert(1, {"id": "early", "kind": "gate", "phase": "plan", "steps": ["lint"]})
    p = factory(gate__steps=[{"key": "lint", "cmd": "false"}, *PYTEST_STEP], loop=loop)
    r = runner(p, FakeBackend())
    job = r.run("Add a multiply function")
    assert job["stage"] == "needs-human" and "no builder before early" in job["note"]
    assert job["round"] == 0
    assert json.loads((jobs.job_dir(git_repo, job["id"]) / "round-0" / "gate.json").read_text())["lint"] == "FAIL"
