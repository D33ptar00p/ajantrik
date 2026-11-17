# Guardrails

Deterministic checks that run inside the loop's gate blocks.

A guardrail is a bash command run in the job's worktree (`gate.steps`); it fails the gate with a
non-zero exit. The studio's Guardrails tab shows the command each one runs. Two are built in: the
secret scan (`gate.secrets`: every commit on the branch, plus gitleaks when installed) and the
clean-tree check (`gate.clean_tree`). The library (`library/guardrails.yaml`, "+ From library") adds:

| guardrail | checks the lines the branch adds for |
|---|---|
| `pii` | email addresses, phone numbers, US SSNs, payment cards (Luhn) and IBANs (checksum); placeholders such as example.com are ignored |
| `topics` | named lists of words or phrases (`TOPICS: "codenames: project falcon; banned: …"`), case-insensitive |
| `regex` | your own patterns (`PATTERNS`, one per line), optionally only in `PATHS` |
| `large-files` | added or grown files over `LIMIT_KB` |
| `conflict-markers` | unresolved merge conflict markers |
| `dependency-audit` | known vulnerabilities, with pip-audit or npm audit when a dependency file changes |
| `debug-leftovers` | `breakpoint()`, `pdb.set_trace()`, `console.log()`, `debugger` (report only by default) |

The PII, topic and regex scans write findings as file, line, rule and fingerprint, never the matched
text. To accept a reviewed false positive, add its fingerprint to `.factory/security/allow.txt`.

A failing gate or a reviewer's `CHANGES` sends the job back to its `on_fail` (the default loop sets
it to the builder), or to the nearest builder before it; with no builder before it, the job goes to a human. Only the builders that own
the findings rebuild, when that is known (a reviewer's or guardrail's `builder`). A round opens at
the first gate or reviewer after a build; checks before any builder run in round 0. Once
`max_rounds` is used up, the job goes to needs-human.

Configs written before phases (`version: 1`, with `builders:` and `reviewers:` teams) still load:
each builder becomes a build agent and each reviewer a review agent, failures go back to the first
builder explicitly, and the next save writes version 2.

---

[← Docs index](README.md) · [Project README](../README.md)
