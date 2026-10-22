"""Guardrails: the PII and content scanners, the library, and gates that run a chosen set."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ajantrik.core import gate as gate_mod
from ajantrik.core.security import content_scan, pii_scan
from ajantrik.kit.project import guardrail_library
from ajantrik.kit.schema import BUILTIN_GUARDRAILS, FactoryConfig, GateStep

from .conftest import sh
from .fakes import commit


def branch_with(repo: Path, files: dict[str, str]) -> str:
    """Commit files on a new branch; returns the merge base with main. Anything already in the tree
    (the rendered factory) is committed on main first, so the branch holds only these files."""
    if sh("git", "status", "--porcelain", cwd=repo):
        commit(repo, "render")
    sh("git", "checkout", "-q", "-b", "work", cwd=repo)
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    commit(repo, "work")
    return sh("git", "merge-base", "main", "HEAD", cwd=repo)


# ---- PII -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("line, rules", [
    ("contact jane.doe@acme.io for access", ["email"]),
    ("user@example.com and ops@test", []),
    ("card 4539 1488 0343 6467 on file", ["payment-card"]),
    ("stripe test 4242 4242 4242 4242", []),
    ("not a card 1234 5678 9012 3456", []),
    ("ssn 123-45-6789", ["us-ssn"]),
    ("ssn 000-12-3456", []),
    ("iban GB82 WEST 1234 5698 7654 32", ["iban"]),
    ("call +44 20 7946 0123 tomorrow", ["phone"]),
    ("fictional 555-0100 line: (212) 555-0100", []),
    ("version 1.2.3 and port 8080", []),
])
def test_pii_rules(line, rules):
    assert [r for r, _ in pii_scan.findings_in(line)] == rules


def test_pii_scan_reports_without_values(git_repo, tmp_path):
    mb = branch_with(git_repo, {"app/users.py": 'ADMIN = "jane.doe@acme.io"\nOK = "ops@example.com"\n'})
    out = tmp_path / "pii.json"
    assert pii_scan.main(["--out", str(out), "--git", str(git_repo), mb]) == 1
    text = out.read_text()
    assert "acme" not in text
    assert [(f["file"], f["line"], f["rule"]) for f in json.loads(text)["findings"]] == [("app/users.py", 1, "email")]


# ---- topics and regexes ----------------------------------------------------------------------------
def test_topics_match_words_and_phrases_case_insensitively():
    topics = content_scan.parse_topics("codenames: Project Falcon, skunk; banned: foo")
    lines = [("a.py", 1, "# project  falcon ships"), ("a.py", 2, "skunkworks is fine"), ("b.md", 3, "FOO bar")]
    found = content_scan.scan(lines, topics, [], [], set())
    assert [(f["line"], f["rule"]) for f in found] == [(1, "topic:codenames"), (3, "topic:banned")]


def test_regexes_and_path_filter(git_repo, tmp_path):
    mb = branch_with(git_repo, {"docs/notes.md": "DO NOT COMMIT\n", "app/x.py": "url = 'http://db.internal/x'\n"})
    out = tmp_path / "regex.json"
    args = ["--out", str(out), "--git", str(git_repo), mb, "--regex", "(?i)do not commit\nhttps?://[\\w.-]*\\.internal\\b"]
    assert content_scan.main(args) == 1
    assert sorted(f["rule"] for f in json.loads(out.read_text())["findings"]) == ["regex:1", "regex:2"]
    assert content_scan.main([*args, "--path", "app/*"]) == 1
    assert [f["file"] for f in json.loads(out.read_text())["findings"]] == ["app/x.py"]
    assert "internal" not in out.read_text()
    assert content_scan.main(["--out", str(out), "--git", str(git_repo), mb]) == 2  # nothing to look for


# ---- the library and gates -------------------------------------------------------------------------
def test_library_guardrails_are_valid_steps_with_unique_keys():
    lib = guardrail_library()
    keys = [g["step"]["key"] for g in lib]
    assert {"pii", "large-files", "conflict-markers", "dependency-audit", "debug-leftovers", "topics", "regex"} <= set(keys)
    assert len(set(keys)) == len(keys) and not set(keys) & set(BUILTIN_GUARDRAILS)
    for g in lib:
        GateStep.model_validate(g["step"])
        assert g["title"] and g["description"] and g["step"]["cmd"]


def test_library_guardrails_pass_a_clean_branch_and_catch_a_dirty_one(git_repo, factory, tmp_path):
    p = factory(gate__steps=[g["step"] for g in guardrail_library()])
    cfg = p.load().cfg.model_dump(mode="json")  # type: ignore[union-attr]
    branch_with(git_repo, {"app/ok.py": "def f():\n    return 1\n"})
    ok, result = gate_mod.run_gate(git_repo, cfg, git_repo, "main", tmp_path / "clean")
    assert ok, (result, (tmp_path / "clean" / "gate.log").read_text())
    assert result["debug-leftovers"] == "PASS" and result["pii"] == "PASS" and result["topics"] == "PASS"

    (git_repo / "app" / "bad.py").write_text("<<<<<<< HEAD\nx = 'skunkworks'\nprint('a@acme.io')\nbreakpoint()\n")
    commit(git_repo, "bad")
    ok, result = gate_mod.run_gate(git_repo, cfg, git_repo, "main", tmp_path / "dirty")
    assert not ok
    assert {k for k in ("pii", "conflict-markers", "topics", "debug-leftovers") if result[k] == "FAIL"} == {
        "pii", "conflict-markers", "topics", "debug-leftovers"}
    assert result["result"] == "FAIL"


def test_a_gate_runs_only_the_guardrails_it_picks(git_repo, factory, tmp_path):
    steps = [{"key": "lint", "cmd": "true"}, {"key": "unit", "cmd": "false"}]
    p = factory(gate__steps=steps)
    cfg = p.load().cfg.model_dump(mode="json")  # type: ignore[union-attr]
    branch_with(git_repo, {"app/ok.py": "x = 1\n"})
    ok, result = gate_mod.run_gate(git_repo, cfg, git_repo, "main", tmp_path / "r", keys=["lint"])
    assert ok and result["lint"] == "PASS" and "unit" not in result and "secrets" not in result
    ok, result = gate_mod.run_gate(git_repo, cfg, git_repo, "main", tmp_path / "r", keys=["unit", "secrets"])
    assert not ok and result["unit"] == "FAIL" and result["secrets"] == "PASS" and result["lint"] == "PASS"


def test_gate_blocks_may_name_the_built_in_guardrails():
    c = FactoryConfig.model_validate({"repo": {"name": "x"}, "gate": {"steps": [{"key": "lint", "cmd": "true"}]},
                                      "loop": {"stages": [{"id": "g", "kind": "gate", "steps": ["lint", "secrets",
                                                                                               "clean-tree"]}]}})
    assert c.stage("g").steps == ["lint", "secrets", "clean-tree"]  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="built in"):
        FactoryConfig.model_validate({"repo": {"name": "x"}, "gate": {"steps": [{"key": "clean-tree", "cmd": "true"}]}})


def test_scanners_are_stdlib_only():
    for mod in (pii_scan, content_scan):
        src = Path(mod.__file__).read_text()  # type: ignore[arg-type]
        assert "import yaml" not in src and "pydantic" not in src
    assert sys.version_info >= (3, 10)
