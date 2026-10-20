"""Backends: the LiteLLM tool loop (guarded tools), and the claude / codex CLIs through fake binaries."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from ajantrik.editions.headless.backends.base import AgentRun, agent_env
from ajantrik.editions.headless.backends.cli_agents import ClaudeCliBackend, CodexCliBackend
from ajantrik.editions.headless.backends.llm_api import LlmApiBackend, litellm_model
from ajantrik.editions.headless.runner import Runner

from .fakes import commit


def scripted(turns: list[list[tuple[str, dict[str, Any]]] | str]):
    """A fake litellm.acompletion: each turn is a list of tool calls, or the final text."""
    seen: list[list[dict[str, Any]]] = []

    async def completion(messages, tools=None, **kw):
        seen.append(list(messages))
        turn = turns[len(seen) - 1]
        if isinstance(turn, str):
            return {"choices": [{"message": {"content": turn, "tool_calls": None}}], "usage": {"prompt_tokens": 1}}
        calls = [{"id": f"c{i}", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(turn)]
        return {"choices": [{"message": {"content": "", "tool_calls": calls}}], "usage": {"prompt_tokens": 1}}

    completion.seen = seen  # type: ignore[attr-defined]
    return completion


def make_run(project, agent: str = "spec-writer", prompt: str = "go") -> AgentRun:
    from ajantrik.core.engine import Engine

    eng = Engine(project.root)
    job = eng.new_job("Add a multiply function")
    jd = project.root / ".factory" / "jobs" / job["id"]
    return AgentRun(name=f"{agent}-x", agent=agent, system="sys", prompt=prompt, model="m",
                    tools=["Read", "Grep", "Glob", "Bash", "Edit", "Write"], root=project.root,
                    worktree=Path(job["worktree"]), job_dir=jd)


def test_api_tool_loop_runs_guarded_tools(factory):
    p = factory()
    run = make_run(p)
    spec = f"{run.job_dir}/spec.md"
    completion = scripted([
        [("Read", {"file_path": "app/calc.py"}), ("Bash", {"command": "cat .env"})],
        [("Write", {"file_path": spec, "content": "# Spec\n"}), ("Bash", {"command": "git push origin HEAD"})],
        [("Write", {"file_path": str(p.root / "app" / "evil.py"), "content": "x"}),
         ("Bash", {"command": "python3 .factory/factory.py approve 001"})],
        "done",
    ])
    result = asyncio.run(LlmApiBackend({"provider": "ollama", "max_turns": 10}, completion).run(run))
    assert result.ok and result.text == "done"
    assert Path(spec).read_text() == "# Spec\n"
    tool_msgs = [m for m in completion.seen[-1] if m["role"] == "tool"]
    outputs = [m["content"] for m in tool_msgs]
    assert "def add" in outputs[0]
    assert "blocked" in outputs[1] and ".env" in outputs[1]
    assert "blocked" in outputs[3] and "never push" in outputs[3]
    assert "blocked" in outputs[4] and "outside the job worktree" in outputs[4]
    assert "blocked" in outputs[5] and "agents never approve" in outputs[5]
    assert not (p.root / "app" / "evil.py").exists()


def test_api_backend_stops_after_max_turns(factory):
    run = make_run(factory())
    completion = scripted([[("Glob", {"pattern": "*.py"})]] * 3)
    result = asyncio.run(LlmApiBackend({"provider": "anthropic", "max_turns": 3}, completion).run(run))
    assert not result.ok and "3 turns" in result.error


def test_litellm_model_names():
    assert litellm_model("ollama", "llama3.2") == "ollama_chat/llama3.2"
    assert litellm_model("litellm", "claude-sonnet") == "litellm_proxy/claude-sonnet"
    assert litellm_model("anthropic", "claude-opus-5-5") == "anthropic/claude-opus-5-5"
    assert litellm_model("openai", "openai/gpt-5") == "openai/gpt-5"
    assert litellm_model("bedrock", "anthropic.claude-v2") == "bedrock/anthropic.claude-v2"


def test_api_key_comes_from_a_reference(monkeypatch):
    monkeypatch.setenv("MY_KEY", "k-123")
    kw = LlmApiBackend({"provider": "litellm", "base_url": "http://proxy:4000", "api_key_ref": "env:MY_KEY"}).kwargs("x")
    assert kw == {"model": "litellm_proxy/x", "api_base": "http://proxy:4000", "api_key": "k-123"}
    assert LlmApiBackend({"provider": "ollama"}).kwargs("llama3.2")["api_base"] == "http://localhost:11434"


def test_agent_env_strips_credentials():
    env = agent_env({"PATH": "/bin", "GITHUB_TOKEN": "x", "AWS_SECRET_ACCESS_KEY": "y", "LANG": "C",
                     "ANTHROPIC_API_KEY": "z"}, keep={"ANTHROPIC_API_KEY"})
    assert env == {"PATH": "/bin", "LANG": "C", "ANTHROPIC_API_KEY": "z"}


def fake_cli(tmp_path: Path, name: str, events: list[dict[str, Any]], writes: str = "") -> Path:
    """A stand-in CLI that records its argv and stdin, writes a file, and prints JSON events."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / name
    lines = "\n".join(json.dumps(e) for e in events)
    write_line = f"Path({writes!r}).write_text('# Spec')" if writes else ""
    script.write_text(f"""#!/usr/bin/env python3
import json, sys, os
from pathlib import Path
Path({str(tmp_path / (name + '.argv'))!r}).write_text(json.dumps(sys.argv[1:]))
data = sys.stdin.read() if not sys.stdin.isatty() else ""
Path({str(tmp_path / (name + '.stdin'))!r}).write_text(data)
Path({str(tmp_path / (name + '.env'))!r}).write_text(json.dumps({{k: v for k, v in os.environ.items()
    if k in ("FACTORY_GUARD", "CLAUDE_PROJECT_DIR", "GITHUB_TOKEN")}}))
{write_line}
print({lines!r})
""")
    script.chmod(0o755)
    return script


def test_claude_cli_backend(factory, tmp_path, monkeypatch):
    p = factory()
    run = make_run(p)
    run.session_id = "sess-1"
    events = [{"type": "system", "subtype": "init", "session_id": "sess-1"},
              {"type": "result", "subtype": "success", "is_error": False, "result": "wrote spec",
               "session_id": "sess-1", "total_cost_usd": 0.12}]
    binary = fake_cli(tmp_path, "claude", events, writes=str(run.job_dir / "spec.md"))
    monkeypatch.setenv("GITHUB_TOKEN", "should-not-leak")
    result = asyncio.run(ClaudeCliBackend(str(binary)).run(run))
    assert result.ok and result.session_id == "sess-1" and result.usage["total_cost_usd"] == 0.12
    argv = json.loads((tmp_path / "claude.argv").read_text())
    assert argv[:2] == ["-p", "go"] and "--resume" in argv and "sess-1" in argv
    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep,Glob,Bash,Edit,Write"
    assert argv[argv.index("--add-dir") + 1] == str(run.worktree)
    env = json.loads((tmp_path / "claude.env").read_text())
    assert env == {"FACTORY_GUARD": "strict", "CLAUDE_PROJECT_DIR": str(p.root)}  # token stripped


def test_codex_cli_backend(factory, tmp_path):
    p = factory()
    run = make_run(p)
    events = [{"type": "thread.started", "thread_id": "th-9"},
              {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
              {"type": "turn.completed", "usage": {}}]
    binary = fake_cli(tmp_path, "codex", events)
    result = asyncio.run(CodexCliBackend(str(binary)).run(run))
    assert result.ok and result.session_id == "th-9" and result.text == "done"
    argv = json.loads((tmp_path / "codex.argv").read_text())
    assert argv[0] == "exec" and "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--add-dir") + 1] == str(run.job_dir) and argv[-1] == "-"
    assert (tmp_path / "codex.stdin").read_text().startswith("sys\n\n---\n\ngo")  # instructions lead
    run.session_id = "th-9"
    asyncio.run(CodexCliBackend(str(binary)).run(run))
    argv = json.loads((tmp_path / "codex.argv").read_text())
    assert argv[-3:] == ["resume", "th-9", "-"]
    assert (tmp_path / "codex.stdin").read_text() == "go"


def test_runner_composes_agent_instructions_and_models(factory):
    p = factory(runtime={"kind": "api", "models": {"sonnet": "llama3.2", "opus": "qwen3"},
                         "per_agent": {"architect": "sonnet"}, "api": {"provider": "ollama"}})
    seen: list[AgentRun] = []

    class Spy:
        name, supports_resume = "spy", False

        async def run(self, run: AgentRun):
            from ajantrik.editions.headless.backends.base import AgentResult

            seen.append(run)
            if run.agent == "spec-writer":
                (run.job_dir / "spec.md").write_text("# Spec\n")
            if run.agent == "architect":
                (run.job_dir / "design.md").write_text("Builders: main\n")
            return AgentResult(True)

    r = Runner(p, backend=Spy(), say=lambda m: None)
    job = r.run("Add a multiply function")
    assert job["stage"] == "awaiting-approval"
    spec, arch = seen
    assert spec.model == "qwen3"  # spec-writer is opus in the library
    assert arch.model == "llama3.2"  # per_agent override
    assert "You are the `spec-writer` agent" in spec.system and "CLAUDE.md, AGENTS.md" in spec.system
    assert "Builders available: main" in arch.prompt


def test_commit_helper_used_by_fakes(git_repo):
    (git_repo / "x.txt").write_text("x")
    commit(git_repo, "x")
    assert os.path.exists(git_repo / "x.txt")


def test_text_tool_calls_from_small_models(factory):
    from ajantrik.editions.headless.backends.llm_api import text_tool_calls

    allowed = ["Write", "Read"]
    assert text_tool_calls('{"name": "Write", "parameters": {"file_path": "a", "content": "b"}}', allowed, 1)
    assert text_tool_calls('```json\n[{"name": "Read", "arguments": {"file_path": "a"}}]\n```', allowed, 1)
    assert text_tool_calls('{"name": "Bash", "parameters": {"command": "ls"}}', allowed, 1) == []
    assert text_tool_calls("I wrote the spec.", allowed, 1) == []
    run = make_run(factory())
    spec = f"{run.job_dir}/spec.md"
    completion = scripted([json.dumps({"name": "Write", "parameters": {"file_path": spec, "content": "# S"}}), "done"])
    result = asyncio.run(LlmApiBackend({"provider": "ollama"}, completion).run(run))
    assert result.ok and Path(spec).read_text() == "# S"
