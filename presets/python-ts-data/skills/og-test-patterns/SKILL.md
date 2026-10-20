---
name: og-test-patterns
description: How OmniGrid's pytest suite is written and run - commands for a factory worktree, naming tests as behaviour sentences, small fixtures, skipping when raw data is absent, and testing provenance and fail-loud paths. Preloaded by factory agents that write or review tests.
---

# OmniGrid test patterns

## Run

Main checkout: `.venv/bin/python -m pytest` (`testpaths = ["tests"]`, ~45 s).

Factory worktree (the shared venv has the main checkout installed in editable
mode, so the worktree must come first on the path):

```bash
cd <worktree> && PYTHONPATH=<worktree> <repo>/.venv/bin/python -m pytest -p no:cacheprovider tests/test_gb.py -q
cd <worktree> && PYTHONPATH=<worktree> <repo>/.venv/bin/python -m pytest -p no:cacheprovider "tests/test_gb.py::test_name" -q
```

`-p no:cacheprovider` keeps `.pytest_cache` out of the worktree. pytest-cov is
not installed; the gate records coverage only if it is.

## Files

`tests/test_schema.py` (fuel taxonomy), `tests/test_provenance.py` (`Cited`,
`collect_source_ids`, registry enforcement), `tests/test_gem.py` (GEM reader),
`tests/test_gb.py` (GB build). Add to the matching file; a new area gets
`tests/test_<area>.py` with a module docstring saying what the tests pin.

## Style

- **Names are behaviour sentences**:
  `test_unreadable_power_returns_none_rather_than_a_guess`,
  `test_only_embedded_units_carry_a_published_region`. No ticket ids.
- **Small fixtures in the test** (a few dicts, a tiny GeoJSON written to
  `tmp_path`) for logic. Assert on the behaviour, not on internals.
- **Real-data tests skip** when the raw file is missing, so a fresh clone
  passes:
  ```python
  RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
  pytestmark = pytest.mark.skipif(
      not (RAW / "elexon_bmu" / "bmunits.json").exists(),
      reason="GB raw inputs not fetched",
  )
  ```
  Module-scoped fixtures for expensive reads (`@pytest.fixture(scope="module")`).
- **Test the honesty**: a new value's `Cited` kind and `source_id`; unknown
  input raises (`pytest.raises(UnknownFuel)`); missing input gives `None`;
  every cited source is registered.
- `pytest.mark.parametrize` for tables of inputs. No `xfail` or `skip` to get a
  red test past the gate.

## What tests don't cover

There is no JS test runner. Map behaviour, panels and layout are verified by
reviewer-ui and reviewer-ux with the screenshot harness, and the gate's smoke
loads both views of the production build. Don't add a JS test framework (it is
a new dependency).
