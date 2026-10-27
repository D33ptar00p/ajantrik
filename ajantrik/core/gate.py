"""The gate: deterministic checks a job branch must pass before any reviewer sees it.

Steps come from factory.yaml (gate.steps); the secret scan of every commit on the branch and the
clean-tree check are built in. Writes <out>/gate.log (redacted on exit) and <out>/gate.json:

    {"<step key>": "PASS|FAIL|skipped|off", ..., "secrets": "PASS|FAIL", "dirty": false,
     "passed": 412, "failed": 0, "coverage": 86.2, "<metric>": ..., "external_only": false}

Each step runs `bash -c <cmd>` in the worktree with WORKTREE BASE MERGE_BASE OUT REPO FACTORY_DIR
and CHANGED (a file listing the changed paths) in its environment. Exit 0 is PASS. With the sandbox on
(sandbox.apply.gate), setup and steps run inside it: they write only the worktree, the round folder and
the temp dir. The secret scan is the factory's own and runs outside.
"""

from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import json
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from . import sandbox as sbx
from .routing import changed_paths, merge_base
from .util import any_match, git, read_json, write_json


def _policy(root: Path) -> Any:
    spec = importlib.util.spec_from_file_location("factory_policy", root / ".factory" / "security" / "policy.py")
    if not spec or not spec.loader:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_junit(path: Path) -> tuple[int, int] | None:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if root.tag == "testsuites" and not suites:
        suites = [root]
    passed = failed = 0
    for s in suites:
        total, fails, errors, skipped = (int(s.get(k, 0) or 0) for k in ("tests", "failures", "errors", "skipped"))
        failed += fails + errors
        passed += total - fails - errors - skipped
    return passed, failed


def parse_coverage(out: Path, spec: dict[str, Any]) -> float | None:
    try:
        if spec.get("cobertura"):
            return round(float(ET.parse(out / spec["cobertura"]).getroot().get("line-rate", "")) * 100, 1)
        if spec.get("json_summary"):
            return round(float(json.loads((out / spec["json_summary"]).read_text())["total"]["lines"]["pct"]), 1)
    except (OSError, ET.ParseError, ValueError, KeyError, TypeError):
        pass
    return None


@contextlib.contextmanager
def port_lock(root: Path, enabled: bool):
    if not enabled:
        yield
        return
    path = Path(os.environ.get("FACTORY_LOCK") or root / ".factory" / ".port.lock")
    with path.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def run_gate(root: Path, cfg: dict[str, Any], worktree: Path, base: str, out: Path,
             keys: list[str] | None = None) -> tuple[bool, dict[str, Any]]:
    """Run the gate steps named by keys (all when None) into out/. A second gate in the same round folder
    adds to its gate.json and gate.log; `requires` can name a step an earlier gate ran."""
    gcfg = cfg.get("gate") or {}
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "gate.log"
    prior = read_json(out / "gate.json", {}) if keys is not None else {}
    prior = {k: v for k, v in (prior or {}).items() if k not in ("result", "error", "external_only")}
    log = log_path.open("a" if prior else "w")

    def say(msg: str) -> None:
        log.write(msg + "\n")
        log.flush()

    result: dict[str, Any] = dict(prior)
    steps = [s for s in gcfg.get("steps") or [] if keys is None or s["key"] in keys]
    for s in steps:
        result[s["key"]] = "pending"
    try:
        mb = merge_base(worktree, base)
        changed = changed_paths(worktree, base)
        changed_file = out / "changed.txt"
        changed_file.write_text("\n".join(changed) + ("\n" if changed else ""))
        branch = git("branch", "--show-current", cwd=worktree, check=False)
        say(f"gate: worktree={worktree} branch={branch} base={base} merge-base={mb[:10]}")
        env = {**os.environ, "WORKTREE": str(worktree), "BASE": base, "MERGE_BASE": mb, "OUT": str(out),
               "REPO": str(root), "FACTORY_DIR": str(root / ".factory"), "CHANGED": str(changed_file),
               "CI": "true",
               # No .pyc files: they would dirty the tree, and a same-second, same-size edit between rounds
               # could otherwise run stale bytecode.
               "PYTHONDONTWRITEBYTECODE": "1"}
        deadline = time.monotonic() + float(gcfg.get("timeout_min", 20)) * 60
        sb = cfg.get("sandbox") or {}
        sb = sb if (sb.get("apply") or {}).get("gate", True) else None
        writable = sbx.writable_for(worktree, out)

        def boxed(cmd: str) -> list[str]:
            return sbx.wrap(["bash", "-c", cmd], sb, sbx.settings_path(root, f"gate-{out.parent.name}-{out.name}"),
                            writable, say=say)

        for chk in gcfg.get("setup") or []:
            r = subprocess.run(boxed(chk["test"]), cwd=worktree, env=env, capture_output=True, text=True)
            if r.returncode != 0:
                say(f"gate: setup failed: {chk['error']}")
                result.update({s["key"]: "FAIL" for s in steps})
                result.update({"secrets": "FAIL", "dirty": False, "error": chk["error"]})
                return _finish(root, out, log, result, False)

        if gcfg.get("secrets", True) and (keys is None or "secrets" in keys):
            say("\n== secrets: secret_scan.sh over the branch's commits")
            scan = root / ".factory" / "security" / "secret_scan.sh"
            r = subprocess.run([str(scan), str(out / "secrets.json"), "--git", str(worktree), base],
                               cwd=worktree, capture_output=True, text=True, env={**os.environ,
                                                                                   "CLAUDE_PROJECT_DIR": str(root)})
            log.write(r.stdout + r.stderr)
            result["secrets"] = "PASS" if r.returncode == 0 else "FAIL"
            say(f"secrets: {result['secrets']}")

        passed = failed = 0
        saw_junit = False
        for s in steps:
            key = s["key"]
            # FACTORY_GATE_<KEY>=gate|report|off overrides a step's mode for one run (e.g. FACTORY_GATE_E2E=off).
            mode = os.environ.get(f"FACTORY_GATE_{key.upper().replace('-', '_')}") or s.get("mode", "gate")
            s = {**s, "mode": mode}
            if mode == "off":
                result[key] = "off"
                continue
            if s.get("when_changed") and not any_match(changed, s["when_changed"]):
                result[key] = "skipped"
                say(f"\n== {key}: skipped (no change in {', '.join(s['when_changed'])})")
                continue
            blocked_by = [r for r in s.get("requires") or [] if result.get(r) != "PASS"]
            if blocked_by:
                result[key] = "skipped"
                say(f"\n== {key}: skipped ({', '.join(blocked_by)} did not pass)")
                continue
            remaining = deadline - time.monotonic()
            timeout = min(float(s.get("timeout_min", 20)) * 60, max(remaining, 1))
            say(f"\n== {key}: {s['cmd'].splitlines()[0][:160]}")
            step_env = {**env, **{k: os.path.expandvars(v) for k, v in (s.get("env") or {}).items()}}
            with port_lock(root, bool(s.get("lock"))):
                try:
                    rc = subprocess.run(boxed(s["cmd"]), cwd=worktree, env=step_env, stdout=log,
                                        stderr=subprocess.STDOUT, timeout=timeout).returncode
                    ok = rc == 0
                except subprocess.TimeoutExpired:
                    say(f"{key}: timed out after {timeout / 60:.1f} min")
                    ok = False
            result[key] = "PASS" if ok else "FAIL"
            if s.get("junit"):
                counts = parse_junit(out / s["junit"])
                if counts:
                    saw_junit = True
                    passed += counts[0]
                    failed += counts[1]
            if s.get("coverage"):
                cov = parse_coverage(out, s["coverage"])
                if cov is not None:
                    result["coverage"] = cov
            if s.get("metrics_file"):
                try:
                    extra = json.loads((out / s["metrics_file"]).read_text())
                    if isinstance(extra, dict):
                        result.update({k: v for k, v in extra.items() if k not in result or result[k] == "pending"})
                except (OSError, ValueError):
                    pass
            say(f"{key}: {result[key]}")
        if saw_junit:
            result["passed"] = passed + (prior.get("passed") or 0)
            result["failed"] = failed + (prior.get("failed") or 0)
        result.setdefault("coverage", None)

        dirty = False
        if gcfg.get("clean_tree", True) and (keys is None or "clean-tree" in keys):
            status = git("status", "--porcelain", cwd=worktree, check=False)
            dirty = bool(status)
            if dirty:
                say("\ngate: the worktree has uncommitted changes after the gate:\n" + status)
        result["dirty"] = dirty

        def mode_of(s: dict[str, Any]) -> str:
            return os.environ.get(f"FACTORY_GATE_{s['key'].upper().replace('-', '_')}") or s.get("mode", "gate")

        failing = [s for s in steps if mode_of(s) == "gate" and result.get(s["key"]) == "FAIL"]
        ok = not failing and result.get("secrets", "PASS") == "PASS" and not dirty
        result["external_only"] = bool(failing) and all(s.get("external_failure") == "needs-human" for s in failing) \
            and result.get("secrets", "PASS") == "PASS" and not dirty
        return _finish(root, out, log, result, ok)
    except Exception as e:  # a crashed gate is a failed gate, with the reason in the log
        say(f"gate: crashed: {e}")
        result.setdefault("error", str(e))
        return _finish(root, out, log, result, False)


def _finish(root: Path, out: Path, log: Any, result: dict[str, Any], ok: bool) -> tuple[bool, dict[str, Any]]:
    log.close()
    result["result"] = "PASS" if ok else "FAIL"
    write_json(out / "gate.json", result)
    # Tool output can echo .env values or tokens; scrub the log before an agent reads it.
    policy = _policy(root)
    if policy is not None:
        path = out / "gate.log"
        env = policy.env_secrets(root)
        path.write_text(policy.redact(path.read_text(errors="replace"), env))
    return ok, result
