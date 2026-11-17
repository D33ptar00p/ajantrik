# How the loop works

The loop is configuration, not prose: one graph of agent, approval and gate blocks, driven by the same engine in every runtime.

The engine (`ajantrik/core/engine.py`) reads the stages as a graph (`ajantrik/core/graph.py`) and
answers one question: *what next?*

- `step <job>` returns one action: run these agents (in parallel or not, fresh or resumed), run the
  gate, wait for an approval, deliver, or stop.
- After the agents of a stage finish, `advance <job>` checks what they wrote, using marker
  lines (`STATUS: READY|BLOCKED`, `VERDICT: PASS|CHANGES`,
  `READY|ESCALATE`, `Builders:`). Then it moves the job on, back a round, or to needs-human.

The interactive `/factory` skill and the headless runner both drive this same API, so the loop is
configuration rather than prose. A loop has three kinds of block:

| kind | does |
|---|---|
| `agent` | runs one agent; its `role` says what it writes (below) |
| `approval` | the job waits at `awaiting-<id>` until a human approves (on the board: Needs human) |
| `gate` | the guardrails it names (`steps`: `gate.steps` keys and the built-in `secrets` and `clean-tree`; empty = all) |

| agent role | writes | and then |
|---|---|---|
| `plain` | its `output` file (spec, design, tests, or anything you add) | `BLOCKED` on the first line stops the job |
| `builder` | code commits and `build-<id>.md` | the ones the design picked (`Builders:` line) run; later rounds resume them with the findings |
| `reviewer` | `round-<N>/review-<id>.md` with `VERDICT: PASS\|CHANGES` | runs always, or only when its path globs, diff patterns or gate metrics match |
| `integrator` | `decision.md` and the PR text (one at most) | refuse-path check before it, outgoing secret scan after it |

The role comes from the agent: `builder-*` agents build, `reviewer-*` agents review, `integrator`
integrates and every other agent writes a file. A block's `role` overrides that.

Phases (`loop.phases`) are only board columns, in order: Plan, Build, Guardrails, Review and
Integrate by default, and you can add your own. An agent block goes in any phase without changing
what it does; a gate goes in Guardrails (else Review) unless you put it elsewhere; approvals have no
phase. What runs in parallel comes
from the arrows: a block whose success edge fans out (`next: [a, b]`, or a second arrow drawn in the
studio) starts `a` and `b` at the same time as one step. Each branch is a single agent, and all
branches must lead on to the same block (or the same fan-out), where the path joins again. Without a
fan-out, blocks run one after the other. Delivery
shows in the studio as a single Deliver block that always runs after the last block; it is the same
`delivery` settings as the Deliver tab: after the last block the job is delivered by `delivery.mode`
(`none`, `push`, `pr` or `direct` with `ff-only` or `squash`), optionally after an approval.

The blocks form a graph:

- Each block has an optional `next`: a block id, `end` (the job is ready), or a list of ids (a
  fan-out, above).
- Without `next` a block continues to the one after it in the list, so a plain ordered list works
  as it always did.
- `loop.start` is the first block; it defaults to the first in the list.
- The run order is the path of `next` edges from `loop.start`. Blocks off that path never run.
- Gates and reviewer agents also have an `on_fail` edge. It must point to an earlier block and
  defaults to the nearest builder before it (below).

Guardrails, rounds and `on_fail` routing are covered in [Guardrails](guardrails.md).

---

[← Docs index](README.md) · [Project README](../README.md)
