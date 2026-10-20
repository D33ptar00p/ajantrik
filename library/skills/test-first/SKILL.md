---
name: test-first
description: Write or change the test together with the code it covers, covering the named case and one failure or edge case. Preloaded by builders and the test author.
---

# Test first

A change without its test is not finished, and the loop will send it back.
Writing the test second means writing it to match whatever the code happens to
do, which is how a green suite ends up proving nothing.

## Rules

- Write or change the test in the same piece of work as the code it covers.
- Cover the case the acceptance criterion names, not a case that is easier.
- Cover one failure or edge case as well: empty input, a missing key, a
  boundary. A test that only proves the happy path proves very little.
- Run the tests before saying you are done, and read the actual failure before
  editing again.
