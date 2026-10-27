"""Any chat model with tool calling, through LiteLLM: Anthropic, OpenAI, Bedrock, a local Ollama,
a LiteLLM proxy or any OpenAI-compatible server. The factory runs the tool loop itself, with the
Toolbox (guarded Read/Grep/Glob/Edit/Write/Bash) as the only way the model touches anything.

No session resume: a builder in a later round starts fresh and reads every round folder, which
the prompts already ask for.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ajantrik.core.connections.secrets import resolve

from .base import AgentResult, AgentRun
from .tools import Toolbox

PREFIX = {"anthropic": "anthropic", "openai": "openai", "bedrock": "bedrock", "ollama": "ollama_chat",
          "litellm": "litellm_proxy", "openai-compatible": "openai"}
DEFAULT_BASE = {"ollama": "http://localhost:11434"}


def litellm_model(provider: str, model: str) -> str:
    known = tuple(f"{p}/" for p in {*PREFIX.values(), "ollama", "azure", "vertex_ai", "gemini", "groq", "mistral"})
    if model.startswith(known):
        return model
    return f"{PREFIX[provider]}/{model}"


class LlmApiBackend:
    name = "api"
    supports_resume = False

    def __init__(self, api: dict[str, Any], completion: Any = None, sandbox: dict[str, Any] | None = None):
        self.api = api
        self.sandbox = sandbox
        self._completion = completion  # injectable for tests

    def completion_fn(self) -> Any:
        if self._completion is not None:
            return self._completion
        try:
            import litellm  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError("the api runtime needs litellm: uv pip install 'ajantrik[api]'") from e
        litellm.drop_params = True
        return litellm.acompletion

    def kwargs(self, model: str) -> dict[str, Any]:
        provider = self.api.get("provider", "anthropic")
        kw: dict[str, Any] = {"model": litellm_model(provider, model)}
        base = self.api.get("base_url") or DEFAULT_BASE.get(provider)
        if base:
            kw["api_base"] = base
        key = resolve(self.api.get("api_key_ref"), "model API key")
        if key:
            kw["api_key"] = key
        if self.api.get("temperature") is not None:
            kw["temperature"] = self.api["temperature"]
        kw.update(self.api.get("extra") or {})
        return kw

    async def run(self, run: AgentRun) -> AgentResult:
        try:
            completion = self.completion_fn()
            kw = self.kwargs(run.model)
        except Exception as e:
            return AgentResult(False, error=str(e))
        box = Toolbox(run.root, run.worktree, run.agent, run.tools, self.api.get("tool_output_limit", 20000),
                      self.api.get("command_timeout_s", 600), sandbox=self.sandbox, job_dir=run.job_dir)
        messages: list[dict[str, Any]] = [{"role": "system", "content": run.system},
                                          {"role": "user", "content": run.prompt}]
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "turns": 0}
        log = run.log_path.open("a") if run.log_path else None
        try:
            for _ in range(int(self.api.get("max_turns", 80))):
                resp = await completion(messages=messages, tools=box.specs() or None, **kw)
                usage["turns"] += 1
                u = getattr(resp, "usage", None) or {}
                for k in ("prompt_tokens", "completion_tokens"):
                    usage[k] += int(_get(u, k) or 0)
                msg = _get(_get(resp, "choices")[0], "message")
                calls = _get(msg, "tool_calls") or []
                content = _get(msg, "content") or ""
                if not calls:
                    # Small local models often write the call as JSON text instead of a tool call.
                    calls = text_tool_calls(content, box.allowed, usage["turns"])
                    if calls:
                        content = ""
                entry: dict[str, Any] = {"role": "assistant", "content": content}
                if calls:
                    entry["tool_calls"] = [{"id": _get(c, "id"), "type": "function",
                                            "function": {"name": _get(_get(c, "function"), "name"),
                                                         "arguments": _get(_get(c, "function"), "arguments") or "{}"}}
                                           for c in calls]
                messages.append(entry)
                _log(log, {"assistant": content, "tool_calls": entry.get("tool_calls", [])})
                if not calls:
                    return AgentResult(True, text=content, usage=usage)
                for c in entry["tool_calls"]:
                    name = c["function"]["name"]
                    try:
                        args = json.loads(c["function"]["arguments"] or "{}")
                        out, err = box.call(name, args if isinstance(args, dict) else {})
                    except ValueError:
                        out, err = "arguments were not valid JSON", True
                    _log(log, {"tool": name, "error": err, "output": out[:2000]})
                    messages.append({"role": "tool", "tool_call_id": c["id"], "name": name, "content": out})
            return AgentResult(False, error=f"stopped after {self.api.get('max_turns', 80)} turns", usage=usage)
        except Exception as e:  # provider errors: the agent run failed, the job goes to a human
            return AgentResult(False, error=f"{type(e).__name__}: {e}"[:500], usage=usage)
        finally:
            if log:
                log.close()


def text_tool_calls(content: str, allowed: list[str], turn: int) -> list[dict[str, Any]]:
    """`{"name": "Write", "parameters": {...}}` (or a list of them, possibly fenced) written as text."""
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith(("{", "[")):
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = data if isinstance(data, list) else [data]
    calls = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            return []
        name = it.get("name") or (it.get("function") or {}).get("name")
        args = it.get("parameters", it.get("arguments", it.get("input", {})))
        if name not in allowed or not isinstance(args, dict | str):
            return []
        calls.append({"id": f"text-{turn}-{i}", "function": {"name": name, "arguments":
                                                              args if isinstance(args, str) else json.dumps(args)}})
    return calls


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _log(log: Any, entry: dict[str, Any]) -> None:
    if log:
        log.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        log.flush()


async def probe(api: dict[str, Any], model: str, completion: Any = None) -> tuple[bool, str]:
    """Can this model be reached, and does it call tools?"""
    backend = LlmApiBackend(api, completion)
    try:
        fn = backend.completion_fn()
        kw = backend.kwargs(model)
        tool = {"type": "function", "function": {"name": "Glob", "description": "List files.",
                                                 "parameters": {"type": "object", "properties": {
                                                     "pattern": {"type": "string"}}, "required": ["pattern"]}}}
        resp = await fn(messages=[{"role": "user", "content": "List the Python files: call Glob with *.py."}],
                        tools=[tool], max_tokens=200, **kw)
        msg = _get(_get(resp, "choices")[0], "message")
        calls = _get(msg, "tool_calls") or []
        return True, f"{kw['model']} reachable; " + ("calls tools" if calls else "answered without calling a tool "
                                                     "(agents need tool calling)")
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:300]}"


__all__ = ["LlmApiBackend", "Path", "litellm_model", "probe"]
