---
name: smallest-change
description: Change only what the job asks for, with no reformatting, renaming or tidying of untouched code, nothing unrelated deleted.
---

# Smallest change

Every line you change is a line a human has to review, and work beyond the job
is work nobody asked for.

## Rules

- Change only what the spec and design ask for.
- Do not reformat, rename or tidy code you are not otherwise touching.
- Do not delete or rewrite anything unrelated. If existing code is in your way,
  say so in your build file rather than removing it.
- Leave existing behaviour working. The tests that passed before your change
  must still pass after it.
