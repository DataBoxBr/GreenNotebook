import copy
import hashlib
import json
import os
import sys
import time

import pytest

from rednotebook import activities, sync


MONTH = "2026-09"


def snapshot(text="base", **fields):
    return {"1": {"text": text, **fields}}


def root(content):
    return sync.make_revision(MONTH, [], {}, content)


def revision(parents, base, content):
    return sync.make_revision(MONTH, [item["id"] for item in parents], base, content)


def graph(tmp_path, records):
    for record in records:
        sync.write_revision(tmp_path, record)
    return sync.read_graph(tmp_path)


def materialize(tmp_path, records):
    loaded = graph(tmp_path, records)
    assert loaded.complete(MONTH)
    return loaded.materialize(loaded.heads(MONTH))


def activity(**overrides):
    values = dict(
        id="appointment",
        title="Work",
        description="Notes",
        start="09:00",
        end="10:00",
        alert_minutes_before=5,
    )
    values.update(overrides)
    return activities.Activity(**values)


def with_activity(item):
    return snapshot(**{activities.ACTIVITIES_KEY: {activities.encode_activity(item): None}})


def test_revision_id_is_canonical_and_checked():
    event = "00000000000040008000000000000001"
    first = sync.make_revision(
        MONTH, [], {}, {1: {"text": "hello", "Work": {"b": None, "a": None}}}, event=event
    )
    second = sync.make_revision(
        MONTH, [], {}, {"1": {"Work": {"a": None, "b": None}, "text": "hello"}}, event=event
    )
    assert first == second
    assert sync.validate_revision(first) == first
    tampered = copy.deepcopy(first)
    tampered["content"]["1"]["text"] = "tampered"
    with pytest.raises(ValueError, match="hash"):
        sync.validate_revision(tampered)


def test_native_event_identity_is_unique_even_for_identical_payloads(tmp_path):
    first = root(snapshot("hello", Work={"same entry": None}))
    second = root(snapshot("hello", Work={"same entry": None}))
    assert first["event"] != second["event"] and first["id"] != second["id"]
    assert sync.EVENT_PATTERN.fullmatch(first["event"])
    assert sync.EVENT_PATTERN.fullmatch(second["event"])
    assert {key: value for key, value in first.items() if key not in ("id", "event")} == {
        key: value for key, value in second.items() if key not in ("id", "event")
    }
    assert materialize(tmp_path, [first, second]) == snapshot("hello", Work={"same entry": None})


def test_identical_native_root_is_not_deleted_by_another_events_descendant(tmp_path):
    first = root(snapshot("hello"))
    other = root(snapshot("hello"))
    deletion = revision([other], snapshot("hello"), {})
    result = materialize(tmp_path, [other, deletion, first])
    assert result == snapshot("hello")
    loaded = sync.read_graph(tmp_path)
    assert first["id"] not in loaded.ancestors[deletion["id"]]


@pytest.mark.parametrize("followup", [{}, snapshot("later remote replacement")])
def test_identical_nonroot_events_remain_concurrent_with_unseen_followup(tmp_path, followup):
    before = snapshot("base")
    initial = root(before)
    first = revision([initial], before, snapshot("hello"))
    other = revision([initial], before, snapshot("hello"))
    assert first["event"] != other["event"] and first["id"] != other["id"]
    later = revision([other], snapshot("hello"), followup)
    result = materialize(tmp_path, [initial, other, later, first])
    assert "hello" in result["1"]["text"]
    if followup:
        assert "later remote replacement" in result["1"]["text"]
        assert "sync conflict" in result["1"]["text"]
    else:
        assert result == snapshot("hello")


def test_only_legacy_imports_have_deterministic_event_identity():
    content = snapshot("same legacy", Work={"entry": None})
    first = sync.make_revision(MONTH, [], {}, content, event=sync.LEGACY_IMPORT_EVENT)
    second = sync.make_revision(MONTH, [], {}, copy.deepcopy(content), event="legacy-import")
    assert first == second
    assert first["event"] == "legacy-import"
    assert sync.validate_revision(first) == first
    assert root(content)["id"] != first["id"]
    with pytest.raises(ValueError, match="legacy import event"):
        sync.make_revision(MONTH, [first["id"]], content, snapshot("edited"), event="legacy-import")


@pytest.mark.parametrize(
    "event",
    ["", "ordinary-edit", "0" * 32, "f" * 64, True, [], {}, "00000000-0000-4000-8000-000000000001"],
)
def test_invalid_event_nonce_is_rejected_even_with_correct_payload_hash(event):
    record = root(snapshot())
    record["event"] = event
    payload = {key: value for key, value in record.items() if key != "id"}
    record["id"] = hashlib.sha256(sync.canonical_json(payload).encode("utf-8")).hexdigest()
    with pytest.raises(ValueError, match="event UUID"):
        sync.validate_revision(record)
    with pytest.raises(ValueError, match="event UUID"):
        sync.make_revision(MONTH, [], {}, snapshot(), event=event)


def test_event_is_mandatory_and_included_in_payload_hash():
    record = root(snapshot())
    tampered = copy.deepcopy(record)
    tampered["event"] = "00000000000040008000000000000001"
    with pytest.raises(ValueError, match="hash"):
        sync.validate_revision(tampered)
    del record["event"]
    payload = {key: value for key, value in record.items() if key != "id"}
    record["id"] = hashlib.sha256(sync.canonical_json(payload).encode("utf-8")).hexdigest()
    with pytest.raises(ValueError, match="schema"):
        sync.validate_revision(record)


def test_legacy_event_is_rejected_on_nonroot_records_with_valid_hash():
    initial = root(snapshot())
    child = revision([initial], snapshot(), snapshot("edit"))
    child["event"] = sync.LEGACY_IMPORT_EVENT
    payload = {key: value for key, value in child.items() if key != "id"}
    child["id"] = hashlib.sha256(sync.canonical_json(payload).encode("utf-8")).hexdigest()
    with pytest.raises(ValueError, match="legacy import event"):
        sync.validate_revision(child)


@pytest.mark.parametrize(
    "month,content",
    [
        ("2026-00", snapshot()),
        ("0000-09", snapshot()),
        ("2026-02", {"29": {"text": "bad"}}),
        (MONTH, {True: {"text": "bad"}}),
        (MONTH, {"01": {"text": "bad"}}),
        (MONTH, {"1": {"text": 5}}),
        (MONTH, {"1": {"text": "ok", "unknown": float("nan")}}),
        (MONTH, snapshot(**{activities.ACTIVITIES_KEY: {"not json": None}})),
        (
            MONTH,
            snapshot(**{activities.ACTIVITIES_KEY: {activities.encode_activity(activity()): 5}}),
        ),
    ],
)
def test_invalid_snapshots_rejected(month, content):
    with pytest.raises((ValueError, TypeError)):
        sync.make_revision(month, [], {}, content)


def test_duplicate_activity_ids_rejected():
    records = {activities.encode_activity(activity(title=title)): None for title in ("one", "two")}
    with pytest.raises(ValueError, match="Duplicate activity"):
        root(snapshot(**{activities.ACTIVITIES_KEY: records}))


def test_three_concurrent_contributors_and_later_causal_edit(tmp_path):
    base = snapshot(Work={"old": None})
    initial = root(base)
    branches = [
        revision([initial], base, snapshot(Work={"old": None, name: None}))
        for name in ("A", "B", "C")
    ]
    combined = materialize(tmp_path, [initial, *branches[:2]])
    edited = copy.deepcopy(combined)
    del edited["1"]["Work"]["B"]
    edited["1"]["Work"]["later"] = None
    followup = revision(branches[:2], combined, edited)
    result = materialize(tmp_path, [followup, branches[2]])
    assert result["1"]["Work"] == {"old": None, "A": None, "C": None, "later": None}


def test_nonoverlapping_text_changes_merge_three_ways(tmp_path):
    base = snapshot("first\nsecond\nthird\n")
    initial = root(base)
    texts = ["FIRST\nsecond\nthird\n", "first\nSECOND\nthird\n", "first\nsecond\nTHIRD\n"]
    branches = [revision([initial], base, snapshot(text)) for text in texts]
    assert (
        materialize(tmp_path, [*reversed(branches), initial])["1"]["text"]
        == "FIRST\nSECOND\nTHIRD\n"
    )


def test_nonoverlapping_edits_within_same_line(tmp_path):
    base = snapshot("red green blue")
    initial = root(base)
    one = revision([initial], base, snapshot("RED green blue"))
    two = revision([initial], base, snapshot("red green BLUE"))
    assert materialize(tmp_path, [initial, one, two])["1"]["text"] == "RED green BLUE"


def test_conflicting_text_preserves_complete_versions_and_resolves(tmp_path):
    base = snapshot("original")
    initial = root(base)
    branches = [
        revision([initial], base, snapshot(text))
        for text in ("one complete version", "two complete version", "three complete version")
    ]
    result = materialize(tmp_path, [initial, *branches])
    text = result["1"]["text"]
    assert text.count("complete version") == 3
    assert "sync conflict" in text
    for branch in branches:
        assert branch["id"][:12] in text
    resolved = revision(branches, result, snapshot("resolved"))
    assert materialize(tmp_path, [resolved]) == snapshot("resolved")


def test_identical_concurrent_content_is_not_duplicated(tmp_path):
    base = snapshot()
    initial = root(base)
    left = revision([initial], base, snapshot("same", A=None))
    right = revision([initial], base, snapshot("same", B=None))
    result = materialize(tmp_path, [initial, left, right])
    assert result == snapshot("same", A=None, B=None)


def test_category_delete_versus_edit_retains_only_edited_leaves(tmp_path):
    base = snapshot(Work={"old": None, "changed": "before"}, Tag=None)
    initial = root(base)
    deletion = revision([initial], base, snapshot())
    edit = revision(
        [initial], base, snapshot(Work={"old": None, "changed": "after", "new": None}, Tag=None)
    )
    assert materialize(tmp_path, [initial, deletion, edit]) == snapshot(
        Work={"changed": "after", "new": None}
    )


def test_day_delete_versus_text_edit_preserves_edit(tmp_path):
    base = snapshot()
    initial = root(base)
    deletion = revision([initial], base, {})
    edit = revision([initial], base, snapshot("edited"))
    assert materialize(tmp_path, [initial, deletion, edit]) == snapshot("edited")


def test_unknown_nested_fields_preserved_and_conflicts_explicit(tmp_path):
    base = snapshot(unknown={"list": [1, True, None], "value": 1})
    initial = root(base)
    left = revision(
        [initial], base, snapshot(unknown={"list": [1, True, None], "value": 2, "left": "kept"})
    )
    right = revision(
        [initial], base, snapshot(unknown={"list": [1, True, None], "value": 3, "right": "kept"})
    )
    day = materialize(tmp_path, [initial, left, right])["1"]
    result = day["unknown"]
    assert result["list"] == [1, True, None]
    assert result["left"] == result["right"] == "kept"
    assert result["value"] in (2, 3)
    entry = day[sync.CONFLICT_KEY]["conflicts"][0]
    assert entry["path"] == ["unknown", "value"]
    assert {item["value"] for item in entry["versions"]} == {2, 3}


def test_unknown_container_versus_scalar_preserves_both(tmp_path):
    base = snapshot(unknown=None)
    initial = root(base)
    left = revision([initial], base, snapshot(unknown={"entry": "kept"}))
    right = revision([initial], base, snapshot(unknown="also kept"))
    day = materialize(tmp_path, [initial, left, right])["1"]
    result = day["unknown"]
    assert result["entry"] == "kept"
    assert sync.CONFLICT_KEY not in result
    assert "also kept" in sync.canonical_json(day[sync.CONFLICT_KEY])


def test_activity_disjoint_edits_and_notification_do_not_regress(tmp_path):
    base = with_activity(activity())
    initial = root(base)
    left = revision([initial], base, with_activity(activity(title="New title")))
    right = revision([initial], base, with_activity(activity(description="New notes")))
    sent = revision([initial], base, with_activity(activity(notified_for="2026-09-01T08:55")))
    result = materialize(tmp_path, [initial, left, right, sent])
    assert activities.decode_activities(result["1"][activities.ACTIVITIES_KEY]) == [
        activity(title="New title", description="New notes", notified_for="2026-09-01T08:55")
    ]


def test_activity_delete_versus_edit_retains_valid_complete_record(tmp_path):
    base = with_activity(activity())
    initial = root(base)
    deletion = revision([initial], base, snapshot())
    edit = revision([initial], base, with_activity(activity(description="Edited notes")))
    result = materialize(tmp_path, [initial, deletion, edit])
    decoded = activities.decode_activities(result["1"][activities.ACTIVITIES_KEY])
    assert len(decoded) == 1
    assert decoded[0].description == "Edited notes"
    assert decoded[0].id == "appointment"
    assert decoded == [activity(description="Edited notes")]


def test_activity_later_edits_after_delete_versus_edit_remain_causal(tmp_path):
    base = with_activity(activity())
    initial = root(base)
    deletion = revision([initial], base, snapshot())
    edit = revision([initial], base, with_activity(activity(description="Edited notes")))
    observed = materialize(tmp_path, [initial, deletion, edit])
    later_content = with_activity(activity(description="Edited notes", start="09:30"))
    later = revision([deletion, edit], observed, later_content)
    result = materialize(tmp_path, [later])
    assert result == later_content
    notified = with_activity(
        activity(description="Edited notes", start="09:30", notified_for="2026-09-01T09:25")
    )
    sent = revision([later], result, notified)
    assert materialize(tmp_path, [sent]) == notified


def test_activity_concurrent_disjoint_edits_after_deletion_keep_all_fields(tmp_path):
    base = with_activity(activity())
    initial = root(base)
    deletion = revision([initial], base, snapshot())
    left = revision([initial], base, with_activity(activity(description="New notes")))
    right = revision([initial], base, with_activity(activity(start="09:30")))
    result = materialize(tmp_path, [initial, deletion, left, right])
    assert result == with_activity(activity(description="New notes", start="09:30"))


def test_activity_conflict_is_explicit_without_duplicate_reminders(tmp_path):
    base = with_activity(activity(notified_for="2026-09-01T08:55"))
    initial = root(base)
    left = revision(
        [initial], base, with_activity(activity(title="Left", notified_for="2026-09-01T08:55"))
    )
    right = revision(
        [initial], base, with_activity(activity(title="Right", notified_for="2026-09-01T08:55"))
    )
    result = materialize(tmp_path, [initial, left, right])
    records = result["1"][activities.ACTIVITIES_KEY]
    assert len(records) == 1
    encoded = json.loads(next(iter(records)))
    assert encoded["title"].startswith("[Sync conflict]")
    assert {record["title"] for record in encoded[sync.CONFLICT_KEY]["variants"]} == {
        "Left",
        "Right",
    }
    assert encoded["notified_for"] == "2026-09-01T08:55"
    resolved = revision(
        [left, right],
        result,
        with_activity(activity(title="Resolved", notified_for="2026-09-01T08:55")),
    )
    assert materialize(tmp_path, [resolved]) == with_activity(
        activity(title="Resolved", notified_for="2026-09-01T08:55")
    )


def test_invalid_combined_activity_schedule_preserves_valid_versions(tmp_path):
    base = with_activity(activity())
    initial = root(base)
    left = revision([initial], base, with_activity(activity(start="09:30")))
    right = revision([initial], base, with_activity(activity(end="09:15")))
    records = materialize(tmp_path, [initial, left, right])["1"][activities.ACTIVITIES_KEY]
    record = json.loads(next(iter(records)))
    assert len(activities.decode_activities(records)) == 1
    variants = record[sync.CONFLICT_KEY]["variants"]
    assert {(item["start"], item["end"]) for item in variants} >= {
        ("09:30", "10:00"),
        ("09:00", "09:15"),
    }


def test_unknown_activity_fields_are_preserved(tmp_path):
    record = json.loads(activities.encode_activity(activity()))
    record["extra"] = {"nested": [1, "value"]}
    record["empty"] = {}
    record["text"] = "Unknown v2 field"
    record["alert"] = {"unknown": True}
    content = snapshot(**{activities.ACTIVITIES_KEY: {sync.canonical_json(record): None}})
    assert materialize(tmp_path, [root(content)]) == content


def test_activity_delete_versus_nested_extra_edit_preserves_edit(tmp_path):
    encoded = json.loads(activities.encode_activity(activity()))
    encoded["extra"] = {"entry": "before"}
    base = snapshot(**{activities.ACTIVITIES_KEY: {sync.canonical_json(encoded): None}})
    initial = root(base)
    encoded["extra"]["entry"] = "after"
    edited = snapshot(**{activities.ACTIVITIES_KEY: {sync.canonical_json(encoded): None}})
    deletion = revision([initial], base, snapshot())
    change = revision([initial], base, edited)
    result = materialize(tmp_path, [initial, deletion, change])
    record = json.loads(next(iter(result["1"][activities.ACTIVITIES_KEY])))
    assert record["extra"] == {"entry": "after"}


def test_empty_container_versus_scalar_keeps_both(tmp_path):
    base = snapshot(unknown="before")
    initial = root(base)
    left = revision([initial], base, snapshot(unknown={}))
    right = revision([initial], base, snapshot(unknown="after"))
    day = materialize(tmp_path, [initial, left, right])["1"]
    assert day["unknown"] == {}
    values = day[sync.CONFLICT_KEY]["conflicts"][0]["versions"]
    assert {sync.canonical_json(item["value"]) for item in values} == {'"after"', "{}"}


def test_conflict_marker_does_not_overwrite_unknown_entry(tmp_path):
    base = snapshot(unknown="before")
    initial = root(base)
    left = revision([initial], base, snapshot(unknown={sync.CONFLICT_KEY: "user value"}))
    right = revision([initial], base, snapshot(unknown="concurrent scalar"))
    day = materialize(tmp_path, [initial, left, right])["1"]
    result = day["unknown"]
    assert result[sync.CONFLICT_KEY] == "user value"
    assert sync.CONFLICT_KEY + "_" not in result
    assert "concurrent scalar" in sync.canonical_json(day[sync.CONFLICT_KEY])


def test_category_conflicts_are_archived_only_at_day_level(tmp_path):
    base = snapshot(Work={"entry": {"old": None}})
    initial = root(base)
    left = revision([initial], base, snapshot(Work={"entry": None}))
    right = revision([initial], base, snapshot(Work={"entry": {"new": None}}))
    day = materialize(tmp_path, [initial, left, right])["1"]
    assert day["Work"] == {"entry": {"new": None}}
    entry = day[sync.CONFLICT_KEY]["conflicts"][0]
    assert entry["path"] == ["Work", "entry"]
    values = {sync.canonical_json(item["value"]) for item in entry["versions"]}
    assert values == {"null", '{"new":null}'}


def test_later_nested_edit_does_not_silently_discard_scalar_conflict(tmp_path):
    base = snapshot(unknown={"old": None})
    initial = root(base)
    left = revision([initial], base, snapshot(unknown="complete scalar version"))
    right = revision([initial], base, snapshot(unknown={"new": None}))
    observed = materialize(tmp_path, [initial, left, right])
    content = copy.deepcopy(observed)
    content["1"]["unknown"]["later"] = None
    edited = revision([left, right], observed, content)
    day = materialize(tmp_path, [edited])["1"]
    assert day["unknown"] == {"new": None, "later": None}
    assert "complete scalar version" in sync.canonical_json(day[sync.CONFLICT_KEY])


def test_day_conflict_metadata_is_stable_resolves_and_preserves_reserved_data(tmp_path):
    reserved = {"legacy": [1, True, "unchanged"]}
    base = snapshot(unknown=0, **{sync.CONFLICT_KEY: reserved})
    initial = root(base)
    left = revision([initial], base, snapshot(unknown=1, **{sync.CONFLICT_KEY: reserved}))
    right = revision([initial], base, snapshot(unknown=2, **{sync.CONFLICT_KEY: reserved}))
    observed = materialize(tmp_path, [initial, left, right])
    assert observed["1"][sync.CONFLICT_KEY]["original"] == reserved
    assert materialize(tmp_path, []) == observed
    cloned = root(observed)
    destination = graph(tmp_path / "destination", [cloned])
    assert destination.materialize(destination.heads(MONTH)) == observed
    resolved = copy.deepcopy(observed)
    resolved["1"]["unknown"] = 3
    followup = revision([left, right], observed, resolved)
    result = materialize(tmp_path, [followup])
    assert result["1"]["unknown"] == 3
    assert result["1"][sync.CONFLICT_KEY] == reserved


def test_long_history_reuses_causal_maxima_without_recursive_replay(monkeypatch):
    loaded = sync.RevisionGraph()
    original = loaded._maxima
    replayed = []

    def maxima(ids):
        if loaded._normal_heads(ids) not in loaded._maximal_writes:
            replayed.append(len(ids))
        return original(ids)

    monkeypatch.setattr(loaded, "_maxima", maxima)
    parents = []
    before = {}
    for index in range(3200):
        after = snapshot(str(index))
        record = sync.make_revision(MONTH, parents, before, after)
        loaded.accept(record)
        parents = [record["id"]]
        before = after
    assert loaded.materialize(parents) == snapshot("3199")
    assert loaded.materialize(iter(parents)) == snapshot("3199")
    assert replayed == []
    assert all(len(key) == 1 for key in loaded._maximal_writes if key)
    assert all(isinstance(ancestors, sync.CausalSet) for ancestors in loaded.ancestors.values())


def test_causal_bitsets_support_membership_iteration_and_set_operations():
    loaded = sync.RevisionGraph()
    initial = root(snapshot())
    left = revision([initial], snapshot(), snapshot("left"))
    right = revision([initial], snapshot(), snapshot("right"))
    for record in (initial, left, right):
        loaded.accept(record)
    before = loaded.ancestors[left["id"]]
    assert initial["id"] in before and left["id"] not in before
    assert len(before) == 1 and set(before) == {initial["id"]}
    all_ids = loaded.closure([left["id"], right["id"]])
    assert len(all_ids) == 3
    assert set(before | {left["id"]}) == {initial["id"], left["id"]}
    assert set(all_ids - {left["id"], right["id"]}) == {initial["id"]}
    assert set(loaded.closure([left["id"]]) & loaded.closure([right["id"]])) == {initial["id"]}


def test_cold_3200_revision_chain_uses_compact_memory_and_cached_rescans(
    tmp_path, monkeypatch, record_property
):
    sync._GRAPH_CACHE.clear()
    parents, before = [], {}
    for index in range(3200):
        after = snapshot(str(index))
        record = sync.make_revision(MONTH, parents, before, after)
        sync.write_revision(tmp_path, record)
        parents, before = [record["id"]], after
    started = time.perf_counter()
    loaded = sync.read_graph(tmp_path)
    record_property("cold_scan_seconds", time.perf_counter() - started)
    assert loaded.complete(MONTH)
    assert loaded.materialize(loaded.heads(MONTH)) == snapshot("3199")
    ancestry_bytes = sum(
        sys.getsizeof(value) + sys.getsizeof(value.bits) for value in loaded.ancestors.values()
    )
    record_property("ancestry_bytes", ancestry_bytes)
    assert ancestry_bytes < 3_000_000
    assert len(loaded._materialized) <= sync.MAX_MATERIALIZATIONS
    assert all(isinstance(key, tuple) and len(key) <= 1 for key in loaded._maximal_writes)

    def no_reparse(_value):
        raise AssertionError("Unchanged JSON files must not be reparsed")

    monkeypatch.setattr(sync, "_json_load", no_reparse)
    assert sync.read_graph(tmp_path) is loaded
    assert sync.read_graph(str(tmp_path / ".")) is loaded


def test_appended_revision_extends_cached_graph_and_parses_only_new_file(tmp_path, monkeypatch):
    initial = root(snapshot())
    loaded = graph(tmp_path, [initial])
    child = revision([initial], snapshot(), snapshot("new"))
    sync.write_revision(tmp_path, child)
    parse = sync._json_load
    calls = []

    def counted(value):
        calls.append(value)
        return parse(value)

    monkeypatch.setattr(sync, "_json_load", counted)
    assert sync.read_graph(tmp_path) is loaded
    assert loaded.materialize(loaded.heads(MONTH)) == snapshot("new")
    assert len(calls) == 1
    assert sync.read_graph(tmp_path) is loaded and len(calls) == 1


def test_changed_partial_replacement_and_removal_invalidate_graph(tmp_path):
    initial = root(snapshot())
    loaded = graph(tmp_path, [initial])
    path = next((tmp_path / sync.REVISION_DIR).glob("*.json"))
    path.write_text('{"version":1,', encoding="utf-8")
    partial = sync.read_graph(tmp_path)
    assert partial is not loaded and not partial.complete(MONTH)
    replacement = root(snapshot("replacement"))
    old_stat = path.stat()
    temporary = path.with_suffix(".replacement")
    temporary.write_text(sync.canonical_json(replacement), encoding="utf-8")
    os.utime(temporary, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    os.replace(temporary, path)
    replaced = sync.read_graph(tmp_path)
    assert replaced is not partial and replaced.complete(MONTH)
    assert set(replaced.records) == {replacement["id"]}
    assert replaced.materialize(replaced.heads(MONTH)) == snapshot("replacement")
    path.unlink()
    removed = sync.read_graph(tmp_path)
    assert removed is not replaced and removed.records == {}


def test_graph_folder_cache_is_bounded(tmp_path):
    sync._GRAPH_CACHE.clear()
    for index in range(8):
        directory = tmp_path / str(index)
        directory.mkdir()
        sync.read_graph(directory)
        assert len(sync._GRAPH_CACHE) <= sync.MAX_CACHED_FOLDERS
    assert os.path.normcase(os.path.abspath(tmp_path / "0")) not in sync._GRAPH_CACHE


@pytest.mark.parametrize("before,after", [(1, True), (True, 1), (1, 1.0), (False, 0)])
def test_json_scalar_type_changes_are_real_writes(tmp_path, before, after):
    base = snapshot(unknown=before)
    initial = root(base)
    edit = revision([initial], base, snapshot(unknown=after))
    result = materialize(tmp_path, [initial, edit])["1"]["unknown"]
    assert type(result) is type(after)
    assert result == after


def test_later_text_edit_with_late_third_peer_preserves_all_contributions(tmp_path):
    base = snapshot("first\nsecond\nthird\n")
    initial = root(base)
    left = revision([initial], base, snapshot("FIRST\nsecond\nthird\n"))
    middle = revision([initial], base, snapshot("first\nSECOND\nthird\n"))
    late = revision([initial], base, snapshot("first\nsecond\nTHIRD\n"))
    observed = materialize(tmp_path, [initial, left, middle])
    followup = revision([left, middle], observed, snapshot("FIRST AGAIN\nSECOND\nthird\n"))
    assert materialize(tmp_path, [followup, late])["1"]["text"] == "FIRST AGAIN\nSECOND\nTHIRD\n"


def test_activity_delete_after_observing_conflict_removes_every_version(tmp_path):
    base = with_activity(activity())
    initial = root(base)
    left = revision([initial], base, with_activity(activity(title="Left")))
    right = revision([initial], base, with_activity(activity(title="Right")))
    observed = materialize(tmp_path, [initial, left, right])
    deletion = revision([left, right], observed, snapshot())
    assert materialize(tmp_path, [deletion]) == snapshot()


def test_month_parent_must_not_belong_to_another_month(tmp_path, caplog):
    initial = root(snapshot())
    bad = sync.make_revision("2026-10", [initial["id"]], snapshot(), snapshot("bad"))
    loaded = graph(tmp_path, [initial, bad])
    assert not loaded.complete("2026-10")
    assert loaded.complete(MONTH)
    assert "another month" in caplog.text


def test_missing_parent_and_invalid_base_are_deferred(tmp_path, caplog):
    initial = root(snapshot())
    child = revision([initial], snapshot(), snapshot("later"))
    loaded = graph(tmp_path, [child])
    assert not loaded.complete(MONTH)
    assert "parents" in caplog.text
    loaded = graph(tmp_path, [initial])
    assert loaded.complete(MONTH)
    assert loaded.materialize(loaded.heads(MONTH)) == snapshot("later")
    bad = revision([child], snapshot("wrong baseline"), snapshot("bad"))
    loaded = graph(tmp_path, [bad])
    assert not loaded.complete(MONTH)
    assert "complete observed parent snapshot" in caplog.text


def test_any_json_filename_deduplicated_by_payload_id(tmp_path):
    initial = root(snapshot())
    directory = tmp_path / sync.REVISION_DIR
    directory.mkdir()
    for name in ("drive-conflicted-copy.json", "unrelated-name.json"):
        (directory / name).write_text(sync.canonical_json(initial), encoding="utf-8")
    loaded = sync.read_graph(tmp_path)
    assert list(loaded.records) == [initial["id"]]
    assert loaded.materialize(loaded.heads(MONTH)) == snapshot()


def test_immutable_malformed_target_is_never_overwritten(tmp_path):
    record = root(snapshot("Unicode \u00e3 \u4e2d"))
    directory = tmp_path / sync.REVISION_DIR
    directory.mkdir()
    path = directory / f"{MONTH}.{record['id']}.json"
    path.write_text("partial", encoding="utf-8")
    with pytest.raises(OSError, match="Refusing to overwrite"):
        sync.write_revision(tmp_path, record)
    assert path.read_text(encoding="utf-8") == "partial"


def test_atomic_fsync_failure_retains_original(tmp_path, monkeypatch):
    path = tmp_path / "cache.txt"
    path.write_bytes(b"original")

    def fail(_fd):
        raise OSError("fsync failed")

    monkeypatch.setattr(sync.os, "fsync", fail)
    with pytest.raises(OSError, match="fsync failed"):
        sync.atomic_write(path, b"new")
    assert path.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("mutation", ["hash", "version", "day", "activity", "duplicate-json-key"])
def test_malformed_records_logged_and_untouched(tmp_path, caplog, mutation):
    record = root(snapshot())
    if mutation == "hash":
        record["content"]["1"]["text"] = "changed"
    elif mutation == "version":
        record["version"] = True
    elif mutation == "day":
        record["content"] = {"31": {"text": "invalid"}}
    elif mutation == "activity":
        record["content"] = snapshot(**{activities.ACTIVITIES_KEY: {"broken": None}})
    else:
        text = sync.canonical_json(record).replace('"version":1', '"version":1,"version":1')
    if mutation != "duplicate-json-key":
        payload = {key: value for key, value in record.items() if key != "id"}
        if mutation != "hash":
            record["id"] = hashlib.sha256(sync.canonical_json(payload).encode()).hexdigest()
        text = sync.canonical_json(record)
    directory = tmp_path / sync.REVISION_DIR
    directory.mkdir()
    path = directory / "2026-09.partial.json"
    path.write_text(text, encoding="utf-8")
    loaded = sync.read_graph(tmp_path)
    assert MONTH in loaded.observed
    assert not loaded.complete(MONTH)
    assert path.read_text(encoding="utf-8") == text
    assert "Deferring revision" in caplog.text
