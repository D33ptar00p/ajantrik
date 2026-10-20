"""The backlog functions in core/connections/tickets.py: list, add, update, delete, combine, claim."""

from __future__ import annotations

from pathlib import Path

import pytest

from ajantrik.core.connections import tickets as tix
from ajantrik.core.util import FactoryError

QUEUED = "- [ ] Escape plant names in tooltips"
TICKET = "- [ ] GEN-5 · base: feature/GEN-1_Proj · Add project rename"
BLOCKED = "- [!] GEN-6 · epic GEN-9 has no branch on origin · Actor profile export"
DONE = "- [x] GEN-1 · base: main · Old thing → 001-GEN-1-old-thing"


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
    assert [i["key"] for i in items] == [None, "GEN-5", "GEN-6", "GEN-1"]
    assert [i["base"] for i in items] == [None, "feature/GEN-1_Proj", None, "main"]
    assert [i["text"] for i in items] == ["Escape plant names in tooltips", "Add project rename",
                                          "Actor profile export", "Old thing"]
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
    items = tix.add_item(four, "Do it", key="GEN-9", base="main")
    after = path.read_bytes()
    assert after == before + "- [ ] GEN-9 · base: main · Do it\n".encode()
    assert len(items) == 5 and items[-1]["line"] == "- [ ] GEN-9 · base: main · Do it"
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
    items = tix.update_item(four, 1, TICKET, text="New text", key="GEN-5", base="main", state="blocked")
    after = path.read_text().splitlines(keepends=True)
    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(before) == len(after) and len(changed) == 1
    assert after[changed[0]] == "- [!] GEN-5 · base: main · New text\n"
    assert items[1]["state"] == "blocked" and items[1]["text"] == "New text"


def test_update_can_requeue_a_blocked_item_and_drops_its_reason(four):
    tix.update_item(four, 2, BLOCKED, text="Actor profile export", key="GEN-6", base="main", state="queued")
    assert "- [ ] GEN-6 · base: main · Actor profile export\n" in tix.backlog_path(four).read_text()
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
    {"text": "ok", "key": "bad key"}, {"text": "ok", "key": "gen-1"}, {"text": "ok", "key": "GEN-1 extra"},
    {"text": "ok", "base": "a b"}, {"text": "ok", "base": "a\nb"}, {"text": "ok", "key": "GEN-1\n"},
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
        tix.add_item(tmp_path, "x", key="GEN-1", key_pattern=r"ABC-\d+")
    tix.add_item(tmp_path, "x", key="ABC-1", key_pattern=r"ABC-\d+")
    assert tix.list_items(tmp_path, r"ABC-\d+")[0]["key"] == "ABC-1"


def test_empty_key_and_base_are_left_out(tmp_path):
    tix.add_item(tmp_path, "x", key="", base="")
    assert tix.list_items(tmp_path)[0]["line"] == "- [ ] x"


# ---- combine ------------------------------------------------------------------------------------------
@pytest.fixture
def mixed(tmp_path: Path) -> list[dict]:
    write(tmp_path, "- [ ] GEN-1 · base: main · alpha", "- [ ] beta", "- [ ] GEN-1 · base: main · gamma",
          "- [ ] GEN-2 · base: dev · delta", BLOCKED, DONE, "- [ ] base: dev · eps")
    return tix.list_items(tmp_path)


def test_combine_joins_texts_in_index_order_with_the_single_key_and_base(mixed):
    r = tix.combine(mixed, [(2, mixed[2]["line"]), (0, mixed[0]["line"])])
    assert r["request"] == "alpha; gamma" and r["key"] == "GEN-1" and r["base"] == "main"
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
    write(tmp_path, "- [ ] GEN-1 · one", "- [x] done → 001-x", "- [ ] GEN-2 · two", "- [ ] base: a · b",
          "- [ ] base: c · d")
    path = tix.backlog_path(tmp_path)
    before = path.read_bytes()
    with pytest.raises(FactoryError):
        tix.claim_items(tmp_path, indices)
    assert path.read_bytes() == before


def test_release_items_restores_the_original_lines_exactly(tmp_path):
    write(tmp_path, "- [ ] GEN-1 · base: main · one", "- [ ] two", "- [ ] GEN-1 · three")
    before = tix.backlog_path(tmp_path).read_bytes()
    r = tix.claim_items(tmp_path, [0, 2])
    assert tix.backlog_path(tmp_path).read_bytes() != before
    tix.release_items(tmp_path, r["items"])
    assert tix.backlog_path(tmp_path).read_bytes() == before
