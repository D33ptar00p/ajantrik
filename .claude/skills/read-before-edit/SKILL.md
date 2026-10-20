---
name: read-before-edit
description: Read a file before editing it, match its style, and prefer targeted edits over rewriting whole files.
---

# Read before edit

Edit replaces an exact string. Guessing at what is in a file wastes a turn on a
failed match, and guessing at surrounding code produces edits that apply
cleanly and mean the wrong thing.

## Rules

- Read a file before your first edit to it.
- Match the file's existing style: its imports, naming and error handling.
  Consistency with the surrounding code beats your own preference.
- Prefer Edit over Write for a file that already exists. Write replaces the
  whole file, and it is how unrelated code gets deleted by accident.
