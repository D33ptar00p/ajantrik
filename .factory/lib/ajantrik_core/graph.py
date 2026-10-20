"""The loop as a graph: each stage has one success edge (`next`) and gate/reviewers stages one
failure edge (`on_fail`, back into the round loop).

`next` is optional. Without it a stage continues to the one after it in the list, so a plain
ordered list (every config written before the graph designer) means exactly what it always did.
`next: end` finishes the job (ready). The run order is the path of success edges from
`loop.start` (default: the first stage); stages off that path never run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

END = "end"


def _get(stage: Any, key: str) -> Any:
    return stage.get(key) if isinstance(stage, Mapping) else getattr(stage, key, None)


def successor(stages: Sequence[Any], sid: str) -> str | None:
    ids = [_get(s, "id") for s in stages]
    i = ids.index(sid)
    nxt = _get(stages[i], "next")
    if nxt == END:
        return None
    if nxt:
        return str(nxt)
    return ids[i + 1] if i + 1 < len(ids) else None


def run_order(stages: Sequence[Any], start: str | None = None) -> list[str]:
    """Stage ids in the order a job visits them. Raises ValueError on a cycle or an unknown id."""
    if not stages:
        return []
    ids = [_get(s, "id") for s in stages]
    cur: str | None = start or ids[0]
    seen: list[str] = []
    while cur is not None:
        if cur not in ids:
            raise ValueError(f"no stage {cur!r} (from `next` or `loop.start`)")
        if cur in seen:
            raise ValueError(f"the success edges loop back to {cur!r}; going back is what on_fail is for")
        seen.append(cur)
        cur = successor(stages, cur)
    return seen
