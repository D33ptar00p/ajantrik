---
name: mc-test-patterns
description: How tests are written and run in mocap-desktop-gui (pytest layout, fixtures, mocking McProjectManager and takes, the venv and addopts gotchas). Use when writing, fixing or running tests under tests/ in this repo.
---

# Tests in mocap-desktop-gui

## Running

- The project venv is mkvirtualenv at `~/.virtualenvs/mocap-desktop-gui`, not
  poetry's. `poetry run pytest` does not work.
- Call the interpreter by absolute path; a shell hook rewrites bare `pytest`:
  ```bash
  QT_QPA_PLATFORM=offscreen PYTHONPATH=<checkout> $HOME/.virtualenvs/mocap-desktop-gui/bin/python -m pytest -o addopts="" -q <files>
  ```
- `pyproject.toml` `addopts` adds coverage reports and `--randomly-seed=3694812`
  (pytest-randomly). With the repo addopts, never pass `-p no:randomly` (pytest
  rejects the seed and aborts); use `--no-cov` for quick runs. With
  `-o addopts=""` both are free to choose.
- Five tests in the suite are deliberate `xfail`s (the M3 path); they are not
  failures.
- In a factory worktree, put the worktree first on `PYTHONPATH` so its code is
  imported, not the main checkout's.

## Layout and style

- Tests live in `tests/mc/test_mc_<module or behaviour>.py`, one file per
  behaviour area (`test_mc_calib_apply_flow.py`,
  `test_mc_take_settings_action_bbox_invalidation.py`). Extend the file for the
  area when it exists.
- Module docstring: one sentence on what the file covers. Group tests in
  `class TestXxx:` per method or behaviour; test names describe the behaviour
  (`test_auto_off_holds_the_output_and_asks`). No Jira keys.
- Lint exceptions in `pyproject.toml` disable flakeheaven for `tests/`, but
  black/isort still run on commit.

## Fixtures

- `tests/mc/conftest.py` provides `qt_app` (a `QCoreApplication`) for anything
  creating QObjects or using signals. Request it in tests that touch Qt.
- `intrinsic_integration_config` loads real chessboard videos and skips when
  absent; tests using real data are marked `@pytest.mark.integration`.
- Use `tmp_path` for any file the code writes, and patch the home/store
  location helpers (`mc_common_paths`) so nothing touches the real `~/moveai`.
- `freezegun` (`pytest-freezegun`) for timestamps in folder names and metadata;
  `Faker` is available for names.

## Mocking the big objects

- Build `McProjectManager` through a fixture that patches what it loads, as in
  `tests/mc/test_mc_calib_apply_flow.py` (`pm(qt_app)` fixture), rather than
  opening a real project.
- Takes are usually `MagicMock`s with the attributes the code reads (id, type,
  cam ids, run status, saved dir); see the `_take(...)` helper in that file.
- Assert on emitted signals by connecting a `MagicMock` slot or a list append,
  then checking calls; assert on files written under `tmp_path`; avoid asserting
  only that an internal mock was called.

## Stored-format tests

For anything read from disk, keep a small fixture in the **old** format (inline
YAML/JSON written to `tmp_path`, or a file under `tests/data/`) and assert the
new code loads it and, where a migration runs, that running it twice gives the
same result.
