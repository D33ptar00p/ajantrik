"""A scripted backend: plays every factory agent by writing the files the engine expects."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ajantrik.editions.headless.backends.base import AgentResult, AgentRun

from .conftest import GIT_ENV


def commit(wt: Path, msg: str) -> None:
    import os

    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", msg], cwd=wt, check=True, env=env)


@dataclass
class FakeBackend:
    """verdicts: reviewer id -> list of verdicts per round (last one repeats).
    build: called as build(worktree, round) by builders to change code."""

    verdicts: dict[str, list[str]] = field(default_factory=dict)
    build: Callable[[Path, int], None] | None = None
    blocked_builder: bool = False
    calls: list[AgentRun] = field(default_factory=list)
    name: str = "fake"
    supports_resume: bool = True

    async def run(self, run: AgentRun) -> AgentResult:
        self.calls.append(run)
        jd, wt = run.job_dir, run.worktree
        rnd = int(m.group(1)) if (m := re.search(r"Round: (\d+)\.", run.prompt)) else 0
        a = run.agent
        if a == "spec-writer":
            (jd / "spec.md").write_text("# Spec\n\nAdd a multiply function.\n")
        elif a == "architect":
            (jd / "design.md").write_text("# Design\n\nBuilders: main\n")
        elif a == "test-author":
            (wt / "tests" / "test_mul.py").write_text(
                "from app.calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n")
            commit(wt, "test: mul")
            (jd / "tests.md").write_text("# Tests\n\ntests/test_mul.py\n")
        elif a.startswith("builder"):
            m = re.search(r"You are builder (\S+?)\.", run.prompt) or re.search(r"build-([a-z0-9_-]+)\.md", run.prompt)
            assert m, run.prompt
            bid = m.group(1)
            if self.blocked_builder:
                (jd / f"build-{bid}.md").write_text("STATUS: BLOCKED\n\n## Blocked\n\nneeds a new dependency\n")
                return AgentResult(True, session_id=f"s-{run.name}")
            build_round = 1 if "Build round 1." in run.prompt else rnd + 1
            if run.session_id:  # resumed: only the findings message
                build_round = int(re.search(r"Round (\d+) findings", run.prompt).group(1)) + 1  # type: ignore[union-attr]
            (self.build or default_build)(wt, build_round)
            (jd / f"build-{bid}.md").write_text(f"STATUS: READY\n\nround {build_round}\n")
        elif a.startswith("reviewer"):
            m = re.search(r"Write (round-\d+/review-([a-z0-9-]+)\.md)", run.prompt)
            assert m, run.prompt
            seq = self.verdicts.get(m.group(2), ["PASS"])
            verdict = seq[min(rnd - 1, len(seq) - 1)]
            (jd / m.group(1)).write_text(f"VERDICT: {verdict}\n\n- finding for round {rnd}\n")
        elif a == "integrator":
            (jd / "decision.md").write_text("READY\n\nAll green.\n")
            (jd / "pr-title.txt").write_text("feat: add mul\n")
            (jd / "pr.md").write_text("## What\n\nAdds mul.\n")
        return AgentResult(True, session_id=f"s-{run.name}")


def default_build(wt: Path, rnd: int) -> None:
    calc = wt / "app" / "calc.py"
    if "def mul" not in calc.read_text():
        calc.write_text(calc.read_text() + "\n\ndef mul(a, b):\n    return a * b\n")
        commit(wt, f"feat: mul (round {rnd})")
