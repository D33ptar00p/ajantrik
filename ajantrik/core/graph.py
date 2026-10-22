"""The loop as a graph: each stage has success edges (`next`) and gate and reviewer stages one
failure edge (`on_fail`, back into the round loop).

`next` is optional. Without it a stage continues to the one after it in the list, so a plain
ordered list (every config written before the graph designer) means exactly what it always did.
`next: end` finishes the job (ready). `next: [a, b]` fans out: a and b run at the same time and both
lead on to one stage, where the path joins. The run order is the path of success edges from
`loop.start` (default: the first stage); stages off that path never run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

END = "end"
# Delivery is not a stage in the config: it follows the last stage on its own (unless delivery.mode is
# none). The engine and the board use this id for it, so no stage may take it.
DELIVER = "deliver"


# What an agent stage does comes from the agent: plain agents write a file, builders write code and
# build-<id>.md (and are rebuilt with a failed round's findings), reviewers write a VERDICT each round
# and the integrator decides READY or ESCALATE. A stage's `role` says which; without one it follows the
# agent's name (builder-*, reviewer-*, integrator). Phases are only board columns.
ROLES = ("plain", "builder", "reviewer", "integrator")


def _get(stage: Any, key: str) -> Any:
    return stage.get(key) if isinstance(stage, Mapping) else getattr(stage, key, None)


def role_for_agent(agent: str | None) -> str:
    """The role an agent's name implies: builder-web -> builder, reviewer-code -> reviewer."""
    for r in ("builder", "reviewer", "integrator"):
        if agent == r or (agent or "").startswith(f"{r}-"):
            return r
    return "plain"


def role_of(stage: Any) -> str:
    """plain, builder, reviewer or integrator for an agent stage; "" for approvals and gates."""
    if _get(stage, "kind") != "agent":
        return ""
    return _get(stage, "role") or role_for_agent(_get(stage, "agent"))


def successors(stages: Sequence[Any], sid: str) -> list[str]:
    """Where sid's success edges go: one stage, several (a fan-out: they run in parallel), or none."""
    ids = [_get(s, "id") for s in stages]
    i = ids.index(sid)
    nxt = _get(stages[i], "next")
    if nxt == END:
        return []
    if isinstance(nxt, (list, tuple)):
        return [str(n) for n in nxt if n != END]
    if nxt:
        return [str(nxt)]
    return [ids[i + 1]] if i + 1 < len(ids) else []


def successor(stages: Sequence[Any], sid: str) -> str | None:
    """The first success edge (for a fan-out, its first branch; entering it enters the whole group)."""
    nxt = successors(stages, sid)
    return nxt[0] if nxt else None


def groups(stages: Sequence[Any], start: str | None = None) -> list[list[str]]:
    """The run path as steps. A stage with several success edges fans out: those stages run at the same
    time as one step and must all lead to the same next stage (or the same fan-out), where the path joins
    again. A branch is a single agent stage. Raises ValueError on a cycle, an unknown id or a fan-out
    that breaks these rules."""
    if not stages:
        return []
    by_id = {_get(s, "id"): s for s in stages}
    seen: set[str] = set()
    out: list[list[str]] = []

    def visit(sid: str) -> None:
        if sid not in by_id:
            raise ValueError(f"no stage {sid!r} (from `next` or `loop.start`)")
        if sid in seen:
            raise ValueError(f"the success edges loop back to {sid!r}; going back is what on_fail is for")
        seen.add(sid)

    cur: list[str] = [start or _get(stages[0], "id")]
    while cur:
        for sid in cur:
            visit(sid)
        if len(cur) > 1:
            for sid in cur:
                if _get(by_id[sid], "kind") != "agent":
                    raise ValueError(f"{sid} is a parallel branch: only agents run in parallel")
            joins = {tuple(successors(stages, sid)) for sid in cur}
            if len(joins) > 1:
                shown = sorted(", ".join(j) or END for j in joins)
                raise ValueError(f"parallel stages {', '.join(cur)} must all lead to the same next stage "
                                 f"(they lead to {' / '.join(shown)})")
            out.append(cur)
            cur = list(joins.pop())
        else:
            out.append(cur)
            cur = successors(stages, cur[0])
    return out


def run_order(stages: Sequence[Any], start: str | None = None) -> list[str]:
    """Stage ids in the order a job visits them (parallel stages in edge order). Raises ValueError on a
    cycle, an unknown id or a broken fan-out."""
    return [sid for g in groups(stages, start) for sid in g]
