"""The backlog functions in core/connections/tickets.py: list, add, update, delete, combine, claim."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from ajantrik.core.connections import tickets as tix
from ajantrik.core.util import FactoryError

QUEUED = "- [ ] Escape plant names in tooltips"
TICKET = "- [ ] PROJ-5 · base: feature/PROJ-1_Proj · Add project rename"
BLOCKED = "- [!] PROJ-6 · epic PROJ-9 has no branch on origin · User profile export"
DONE = "- [x] PROJ-1 · base: main · Old thing → 001-PROJ-1-old-thing"


def write(root: Path, *lines: str, header: bool = True) -> Path:
    path = tix.backlog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((tix.BACKLOG_HEADER if header else "") + "".join(ln + "\n" for ln in lines))
    return path


@pytest.fixture
def four(tmp_path: Path) -> Path:
    write(tmp_path, QUEUED, TICKET, BLOCKED, DONE)
    return tmp_path


# ---- list ---------------------------------------------------------------------------------------------
def test_list_parses_every_item_in_file_order(four):
    items = tix.list_items(four)
    assert [i["index"] for i in items] == [0, 1, 2, 3]
    assert [i["state"] for i in items] == ["queued", "queued", "blocked", "done"]
    assert [i["key"] for i in items] == [None, "PROJ-5", "PROJ-6", "PROJ-1"]
    assert [i["base"] for i in items] == [None, "feature/PROJ-1_Proj", None, "main"]
    assert [i["text"] for i in items] == ["Escape plant names in tooltips", "Add project rename",
                                          "User profile export", "Old thing"]
    assert [i["line"] for i in items] == [QUEUED, TICKET, BLOCKED, DONE]
    assert len(items) == 4


def test_list_of_a_missing_file_is_empty(tmp_path):
    assert tix.list_items(tmp_path) == []


def test_list_ignores_lines_that_are_not_items_when_counting_indexes(tmp_path):
    write(tmp_path, "some prose", QUEUED, "", "- not an item", TICKET)
    items = tix.list_items(tmp_path)
    assert [(i["index"], i["line"]) for i in items] == [(0, QUEUED), (1, TICKET)]


def test_list_uses_the_given_key_pattern(tmp_path):
    write(tmp_path, "- [ ] ABC_12 · Do it")
    assert tix.list_items(tmp_path, r"ABC_\d+")[0]["key"] == "ABC_12"
    assert tix.list_items(tmp_path)[0]["key"] is None


# ---- add ----------------------------------------------------------------------------------------------
def test_add_appends_one_line_and_leaves_earlier_bytes_alone(four):
    path = tix.backlog_path(four)
    before = path.read_bytes()
    items = tix.add_item(four, "Do it", key="PROJ-9", base="main")
    after = path.read_bytes()
    assert after == before + "- [ ] PROJ-9 · base: main · Do it\n".encode()
    assert len(items) == 5 and items[-1]["line"] == "- [ ] PROJ-9 · base: main · Do it"
    assert items[-1]["state"] == "queued" and items[-1]["index"] == 4


def test_add_text_only_has_no_extra_parts(four):
    tix.add_item(four, "  Just text  ")
    assert tix.backlog_path(four).read_text().endswith("\n- [ ] Just text\n")


def test_add_creates_a_missing_file_starting_with_the_header(tmp_path):
    items = tix.add_item(tmp_path, "First", base="main")
    text = tix.backlog_path(tmp_path).read_text()
    assert text.startswith(tix.BACKLOG_HEADER)
    assert text == tix.BACKLOG_HEADER + "- [ ] base: main · First\n"
    assert [i["text"] for i in items] == ["First"]


def test_add_to_a_file_without_a_trailing_newline_changes_no_other_byte(tmp_path):
    path = tix.backlog_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"## Queue\n\n" + QUEUED.encode())
    tix.add_item(tmp_path, "More")
    assert path.read_bytes() == b"## Queue\n\n" + QUEUED.encode() + b"\n- [ ] More\n"


# ---- update -------------------------------------------------------------------------------------------
def test_update_rewrites_only_that_line(four):
    path = tix.backlog_path(four)
    before = path.read_text().splitlines(keepends=True)
    items = tix.update_item(four, 1, TICKET, text="New text", key="PROJ-5", base="main", state="blocked")
    after = path.read_text().splitlines(keepends=True)
    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(before) == len(after) and len(changed) == 1
    assert after[changed[0]] == "- [!] PROJ-5 · base: main · New text\n"
    assert items[1]["state"] == "blocked" and items[1]["text"] == "New text"


def test_update_can_requeue_a_blocked_item_and_drops_its_reason(four):
    tix.update_item(four, 2, BLOCKED, text="User profile export", key="PROJ-6", base="main", state="queued")
    assert "- [ ] PROJ-6 · base: main · User profile export\n" in tix.backlog_path(four).read_text()
    assert "no branch on origin" not in tix.backlog_path(four).read_text()


def test_update_with_a_stale_line_conflicts_and_changes_nothing(four):
    before = tix.backlog_path(four).read_bytes()
    with pytest.raises(tix.BacklogConflict, match="reload"):
        tix.update_item(four, 1, "- [ ] something else", text="x", state="queued")
    assert tix.backlog_path(four).read_bytes() == before


def test_update_a_missing_index_conflicts(four):
    with pytest.raises(tix.BacklogConflict):
        tix.update_item(four, 9, TICKET, text="x", state="queued")


def test_update_a_done_item_is_refused(four):
    before = tix.backlog_path(four).read_bytes()
    with pytest.raises(FactoryError):
        tix.update_item(four, 3, DONE, text="x", state="queued")
    assert tix.backlog_path(four).read_bytes() == before


def test_update_state_must_be_queued_or_blocked(four):
    with pytest.raises(FactoryError):
        tix.update_item(four, 0, QUEUED, text="x", state="done")
    with pytest.raises(FactoryError):
        tix.update_item(four, 0, QUEUED, text="x", state="x")


# ---- delete -------------------------------------------------------------------------------------------
def test_delete_removes_only_that_line(four):
    path = tix.backlog_path(four)
    before = path.read_text()
    items = tix.delete_item(four, 0, QUEUED)
    assert path.read_text() == before.replace(QUEUED + "\n", "", 1)
    assert [i["line"] for i in items] == [TICKET, BLOCKED, DONE]
    assert [i["index"] for i in items] == [0, 1, 2]


def test_delete_can_remove_a_done_item(four):
    tix.delete_item(four, 3, DONE)
    assert DONE not in tix.backlog_path(four).read_text()


def test_delete_with_a_stale_line_conflicts_and_changes_nothing(four):
    before = tix.backlog_path(four).read_bytes()
    with pytest.raises(tix.BacklogConflict):
        tix.delete_item(four, 0, TICKET)
    with pytest.raises(tix.BacklogConflict):
        tix.delete_item(four, 7, TICKET)
    assert tix.backlog_path(four).read_bytes() == before


def test_delete_from_a_missing_file_conflicts(tmp_path):
    with pytest.raises(tix.BacklogConflict):
        tix.delete_item(tmp_path, 0, QUEUED)
    assert not tix.backlog_path(tmp_path).exists()


def test_conflict_is_a_factory_error():
    assert issubclass(tix.BacklogConflict, FactoryError)


# ---- validation ---------------------------------------------------------------------------------------
BAD = [
    {"text": ""}, {"text": "   "}, {"text": "two\nlines"}, {"text": "two\rlines"}, {"text": "a · b"},
    {"text": "ok", "key": "bad key"}, {"text": "ok", "key": "proj-1"}, {"text": "ok", "key": "PROJ-1 extra"},
    {"text": "ok", "base": "a b"}, {"text": "ok", "base": "a\nb"}, {"text": "ok", "key": "PROJ-1\n"},
    {"text": "ok", "base": "-v"}, {"text": "ok", "base": "--all"}, {"text": "ok", "base": "a..b"},
]


@pytest.mark.parametrize("fields", BAD)
def test_bad_input_is_rejected_and_the_file_is_unchanged(four, fields):
    path = tix.backlog_path(four)
    before = path.read_bytes()
    with pytest.raises(FactoryError):
        tix.add_item(four, **fields)
    with pytest.raises(FactoryError):
        tix.update_item(four, 0, QUEUED, state="queued", **fields)
    assert path.read_bytes() == before


def test_bad_input_does_not_create_a_missing_file(tmp_path):
    with pytest.raises(FactoryError):
        tix.add_item(tmp_path, "")
    assert not tix.backlog_path(tmp_path).exists()


def test_key_is_checked_against_the_configured_pattern(tmp_path):
    with pytest.raises(FactoryError):
        tix.add_item(tmp_path, "x", key="PROJ-1", key_pattern=r"ABC-\d+")
    tix.add_item(tmp_path, "x", key="ABC-1", key_pattern=r"ABC-\d+")
    assert tix.list_items(tmp_path, r"ABC-\d+")[0]["key"] == "ABC-1"


def test_empty_key_and_base_are_left_out(tmp_path):
    tix.add_item(tmp_path, "x", key="", base="")
    assert tix.list_items(tmp_path)[0]["line"] == "- [ ] x"


# ---- combine ------------------------------------------------------------------------------------------
@pytest.fixture
def mixed(tmp_path: Path) -> list[dict]:
    write(tmp_path, "- [ ] PROJ-1 · base: main · alpha", "- [ ] beta", "- [ ] PROJ-1 · base: main · gamma",
          "- [ ] PROJ-2 · base: dev · delta", BLOCKED, DONE, "- [ ] base: dev · eps")
    return tix.list_items(tmp_path)


def test_combine_joins_texts_in_index_order_with_the_single_key_and_base(mixed):
    r = tix.combine(mixed, [(2, mixed[2]["line"]), (0, mixed[0]["line"])])
    assert r["request"] == "alpha; gamma" and r["key"] == "PROJ-1" and r["base"] == "main"
    assert [i["index"] for i in r["items"]] == [0, 2]


def test_combine_without_key_or_base_gives_none(mixed):
    r = tix.combine(mixed, [(1, None)])
    assert r["request"] == "beta" and r["key"] is None and r["base"] is None


def test_combine_skips_the_line_check_for_none(mixed):
    assert tix.combine(mixed, [(0, None), (1, None)])["request"] == "alpha; beta"


def test_combine_errors_in_the_documented_order(mixed):
    with pytest.raises(FactoryError):
        tix.combine(mixed, [])
    with pytest.raises(FactoryError) as e:
        tix.combine(mixed, [(0, None), (0, None)])
    assert not isinstance(e.value, tix.BacklogConflict)
    with pytest.raises(tix.BacklogConflict):
        tix.combine(mixed, [(0, "- [ ] stale")])
    with pytest.raises(tix.BacklogConflict):
        tix.combine(mixed, [(99, None)])
    # a stale line is a conflict even when the item is also not queued (rule 3 before rule 4)
    with pytest.raises(tix.BacklogConflict):
        tix.combine(mixed, [(5, "- [x] stale")])


@pytest.mark.parametrize("picks", [[(4, None)], [(5, None)], [(0, None), (4, None)]])
def test_combine_refuses_items_that_are_not_queued(mixed, picks):
    with pytest.raises(FactoryError) as e:
        tix.combine(mixed, picks)
    assert not isinstance(e.value, tix.BacklogConflict)


def test_combine_refuses_two_keys(mixed):
    with pytest.raises(FactoryError) as e:
        tix.combine(mixed, [(0, None), (3, None)])
    assert not isinstance(e.value, tix.BacklogConflict)


def test_combine_refuses_two_bases(mixed):
    with pytest.raises(FactoryError):
        tix.combine(mixed, [(0, None), (6, None)])


# ---- claim and release --------------------------------------------------------------------------------
def test_claim_items_marks_only_the_chosen_lines(tmp_path):
    write(tmp_path, "- [ ] one", "- [ ] two", "- [ ] base: main · three")
    r = tix.claim_items(tmp_path, [0, 2])
    assert r["request"] == "one; three" and r["base"] == "main" and r["key"] is None
    lines = tix.backlog_path(tmp_path).read_text().splitlines()
    assert "- [x] one → claimed" in lines and "- [x] base: main · three → claimed" in lines
    assert "- [ ] two" in lines
    assert [i["claimed"] for i in r["items"]] == ["- [x] one → claimed", "- [x] base: main · three → claimed"]
    assert [i["line"] for i in r["items"]] == ["- [ ] one", "- [ ] base: main · three"]


@pytest.mark.parametrize("indices", [[], [0, 0], [9], [0, 1], [0, 2], [3, 4]])
def test_claim_items_rule_violation_leaves_the_file_unchanged(tmp_path, indices):
    write(tmp_path, "- [ ] PROJ-1 · one", "- [x] done → 001-x", "- [ ] PROJ-2 · two", "- [ ] base: a · b",
          "- [ ] base: c · d")
    path = tix.backlog_path(tmp_path)
    before = path.read_bytes()
    with pytest.raises(FactoryError):
        tix.claim_items(tmp_path, indices)
    assert path.read_bytes() == before


def test_release_items_restores_the_original_lines_exactly(tmp_path):
    write(tmp_path, "- [ ] PROJ-1 · base: main · one", "- [ ] two", "- [ ] PROJ-1 · three")
    before = tix.backlog_path(tmp_path).read_bytes()
    r = tix.claim_items(tmp_path, [0, 2])
    assert tix.backlog_path(tmp_path).read_bytes() != before
    tix.release_items(tmp_path, r["items"])
    assert tix.backlog_path(tmp_path).read_bytes() == before


# ---- two-way sync with Jira -----------------------------------------------------------------------------
JIRA_CFG = {"connections": {"tickets": {"provider": "jira", "mode": "rest", "site": "https://x.atlassian.net",
                                        "project": "PROJ", "label": "factory", "jql": "project = PROJ",
                                        "create_type": "Story"}},
            "repo": {"default_base": "main"}}


class FakeJira:
    """Jira in memory: create adds to what search returns. `fail` makes create raise for those summaries."""
    issues: ClassVar[list[dict]] = []
    created: ClassVar[list[tuple]] = []
    fail: ClassVar[set[str]] = set()
    on_create = None

    def __init__(self, conn):
        self.conn = conn

    def create(self, project, summary, issue_type, labels):
        if summary in FakeJira.fail:
            raise FactoryError("Jira POST /rest/api/3/issue: HTTP 400")
        key = f"{project}-{100 + len(FakeJira.created)}"
        FakeJira.created.append((project, summary, issue_type, labels))
        FakeJira.issues.append({"key": key, "summary": summary, "epic": None})
        if FakeJira.on_create:
            FakeJira.on_create()
        return key

    def search(self, jql, limit=50):
        return list(FakeJira.issues)

    def get(self, key):
        return {"epic": None}


@pytest.fixture
def jira(tmp_path, monkeypatch):
    FakeJira.issues, FakeJira.created, FakeJira.fail, FakeJira.on_create = [], [], set(), None
    monkeypatch.setattr(tix, "Jira", FakeJira)
    monkeypatch.setattr(tix, "resolve_epic_base", lambda root, conn, epic, default, remote="origin": (default, "no epic"))
    scans: list[list[str]] = []
    monkeypatch.setattr(tix, "_secret_scan", lambda root, texts: scans.append(texts))
    return scans


def test_push_creates_issues_only_for_queued_items_without_a_ticket(tmp_path, jira):
    write(tmp_path, "- [ ] base: dev · Escape names", TICKET, "- [!] blocked thing", DONE.replace("PROJ-1 · ", ""))
    report = tix.push_items(tmp_path, JIRA_CFG)
    assert FakeJira.created == [("PROJ", "Escape names", "Story", ["factory"])]
    assert jira == [["Escape names"]]
    lines = tix.backlog_path(tmp_path).read_text().splitlines()
    assert "- [ ] PROJ-100 · base: dev · Escape names" in lines
    assert TICKET in lines and "- [!] blocked thing" in lines
    assert report == ["PROJ-100: created from the backlog"]


def test_push_with_nothing_to_push_does_not_scan_or_call_jira(tmp_path, jira):
    write(tmp_path, TICKET)
    assert tix.push_items(tmp_path, JIRA_CFG) == []
    assert jira == [] and FakeJira.created == []


def test_sync_push_then_pull_does_not_duplicate_the_new_issue(tmp_path, jira):
    write(tmp_path, QUEUED)
    FakeJira.issues.append({"key": "PROJ-7", "summary": "From Jira", "epic": None})
    report = tix.sync(tmp_path, JIRA_CFG, [], push=True)
    text = tix.backlog_path(tmp_path).read_text()
    assert text.count("PROJ-100") == 1
    assert "- [ ] PROJ-7 · base: main · From Jira" in text
    assert report[0] == "PROJ-100: created from the backlog"
    assert "PROJ-7: added (base main)" in report


def test_sync_without_push_never_creates(tmp_path, jira):
    write(tmp_path, QUEUED)
    tix.sync(tmp_path, JIRA_CFG, [])
    assert FakeJira.created == [] and jira == []
    assert QUEUED in tix.backlog_path(tmp_path).read_text().splitlines()


def test_push_leaves_a_line_edited_meanwhile_alone(tmp_path, jira):
    path = write(tmp_path, QUEUED)
    FakeJira.on_create = lambda: path.write_text(path.read_text().replace(QUEUED, "- [ ] edited by hand"))
    report = tix.push_items(tmp_path, JIRA_CFG)
    assert "- [ ] edited by hand" in path.read_text().splitlines()
    assert "PROJ-100" not in path.read_text()
    assert report == ["PROJ-100: created, but the backlog line changed: add the key by hand"]


def test_one_failed_create_does_not_stop_the_others(tmp_path, jira):
    write(tmp_path, "- [ ] one", "- [ ] two")
    FakeJira.fail = {"one"}
    report = tix.push_items(tmp_path, JIRA_CFG)
    lines = tix.backlog_path(tmp_path).read_text().splitlines()
    assert "- [ ] one" in lines and "- [ ] PROJ-100 · two" in lines
    assert report[0].startswith("one: not pushed (") and report[1] == "PROJ-100: created from the backlog"


def test_identical_lines_each_get_their_own_key(tmp_path, jira):
    write(tmp_path, "- [ ] same", "- [ ] same")
    tix.push_items(tmp_path, JIRA_CFG)
    lines = tix.backlog_path(tmp_path).read_text().splitlines()
    assert "- [ ] PROJ-100 · same" in lines and "- [ ] PROJ-101 · same" in lines


@pytest.mark.parametrize("change", [{"mode": "mcp"}, {"project": None}, {"provider": "none"}])
def test_push_needs_jira_rest_and_a_project(tmp_path, jira, change):
    write(tmp_path, QUEUED)
    cfg = {**JIRA_CFG, "connections": {"tickets": {**JIRA_CFG["connections"]["tickets"], **change}}}
    with pytest.raises(FactoryError):
        tix.push_items(tmp_path, cfg)
    assert FakeJira.created == []


def _scanner(root: Path, code: int) -> None:
    scan = root / ".factory" / "security" / "secret_scan.sh"
    scan.parent.mkdir(parents=True, exist_ok=True)
    scan.write_text(f"#!/bin/sh\nexit {code}\n")
    scan.chmod(0o755)


@pytest.mark.parametrize("code", [1, 2])
def test_a_secret_or_a_failed_scan_pushes_nothing(tmp_path, monkeypatch, code):
    FakeJira.created = []
    monkeypatch.setattr(tix, "Jira", FakeJira)
    _scanner(tmp_path, code)
    path = write(tmp_path, "- [ ] uses token ghp_abc")
    before = path.read_bytes()
    with pytest.raises(FactoryError, match="nothing was pushed"):
        tix.push_items(tmp_path, JIRA_CFG)
    assert FakeJira.created == [] and path.read_bytes() == before


def test_push_without_the_secret_scanner_fails_closed(tmp_path, monkeypatch):
    FakeJira.created = []
    monkeypatch.setattr(tix, "Jira", FakeJira)
    write(tmp_path, QUEUED)
    with pytest.raises(FactoryError, match=r"secret_scan\.sh"):
        tix.push_items(tmp_path, JIRA_CFG)
    assert FakeJira.created == []


def test_a_clean_scan_lets_the_push_through(tmp_path, monkeypatch):
    FakeJira.created, FakeJira.issues, FakeJira.fail, FakeJira.on_create = [], [], set(), None
    monkeypatch.setattr(tix, "Jira", FakeJira)
    _scanner(tmp_path, 0)
    write(tmp_path, QUEUED)
    assert tix.push_items(tmp_path, JIRA_CFG) == ["PROJ-100: created from the backlog"]


def test_jira_create_posts_the_issue_and_returns_its_key(monkeypatch):
    calls = []
    monkeypatch.setattr(tix.Jira, "_request", lambda self, method, path, body=None, query=None:
                        calls.append((method, path, body)) or {"key": "PROJ-9"})
    assert tix.Jira({"site": "https://x.atlassian.net"}).create("PROJ", "Do it", "Task", ["factory"]) == "PROJ-9"
    assert calls == [("POST", "/rest/api/3/issue", {"fields": {"project": {"key": "PROJ"}, "summary": "Do it",
                                                               "issuetype": {"name": "Task"}, "labels": ["factory"]}})]
    monkeypatch.setattr(tix.Jira, "_request", lambda self, method, path, body=None, query=None: {})
    with pytest.raises(FactoryError, match="no key"):
        tix.Jira({"site": "https://x.atlassian.net"}).create("PROJ", "Do it", "Task", [])
