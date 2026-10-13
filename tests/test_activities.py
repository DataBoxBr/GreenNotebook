import datetime
import json
from dataclasses import replace

import pytest
import yaml

from rednotebook import activities, storage, sync
from rednotebook.data import Month


def make_activity(**overrides):
    values = {
        "id": "activity-1",
        "title": "Focus session",
        "description": "Work without interruptions",
        "start": "09:00",
        "end": "10:00",
        "alert_minutes_before": 5,
    }
    values.update(overrides)
    return activities.Activity(**values)


@pytest.fixture
def conflicted_record():
    first = json.loads(activities.encode_activity(make_activity()))
    second = json.loads(activities.encode_activity(make_activity(start="09:30", end="10:30")))
    second["variant_extension"] = {"nested": [None, False, {"source": "remote"}]}
    return {
        **first,
        "title": "[Sync conflict] " + first["title"],
        sync.CONFLICT_KEY: {"revisions": ["left", "right"], "variants": [first, second]},
        "extension": {"nested": [1, "value", None, False]},
        "empty": {},
        "text": "Unknown v2 text field",
        "alert": {"unknown": True},
    }


@pytest.mark.parametrize("value", ["00:00", "09:05", "23:59"])
def test_parse_time_accepts_valid_values(value):
    assert activities.parse_time(value).strftime("%H:%M") == value


@pytest.mark.parametrize("value", ["9:00", "24:00", "12:60", "noon", None])
def test_parse_time_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        activities.parse_time(value)


def test_activity_validates_title_description_and_interval():
    with pytest.raises(ValueError):
        make_activity(title="  ")
    with pytest.raises(ValueError):
        make_activity(description="x" * (activities.MAX_DESCRIPTION_LENGTH + 1))
    with pytest.raises(ValueError):
        make_activity(start="10:00", end="10:00")
    with pytest.raises(ValueError):
        make_activity(start="11:00", end="10:00")
    with pytest.raises(ValueError):
        make_activity(alert_minutes_before=20)


def test_activity_json_round_trip_preserves_unicode():
    activity = make_activity(title="Reunião", description="Revisão do planejamento")
    assert activities.decode_activity(activities.encode_activity(activity)) == activity


def test_legacy_activity_is_migrated_to_nearest_alert_interval():
    record = json.dumps(
        {
            "alert": "08:53",
            "end": "10:00",
            "id": "legacy",
            "notified_for": None,
            "start": "09:00",
            "text": "Legacy title",
            "v": 1,
        }
    )

    activity = activities.decode_activity(record)

    assert activity.title == "Legacy title"
    assert activity.description == ""
    assert activity.alert_minutes_before == 5


def test_day_activities_do_not_become_categories():
    month = Month(2026, 9)
    day = month.get_day(27)
    day.upsert_activity(make_activity())

    assert day.activities == [make_activity()]
    assert activities.ACTIVITIES_KEY not in day.categories
    assert activities.ACTIVITIES_KEY not in day.get_category_content_pairs()
    assert not day.empty
    assert month.edited


def test_day_content_assignment_preserves_activities():
    month = Month(2026, 9)
    day = month.get_day(27)
    day.upsert_activity(make_activity())

    day.content = {"text": "New text", "Work": None}

    assert day.text == "New text"
    assert day.activities == [make_activity()]
    assert day.categories == ["Work"]


def test_invalid_activity_records_are_preserved_when_updating():
    invalid = "not-json"
    records = {invalid: None}
    updated = activities.upsert_activity(records, make_activity())

    assert invalid in updated
    assert activities.decode_activities(updated) == [make_activity()]


@pytest.mark.parametrize("has_conflict", [False, True])
@pytest.mark.parametrize("notified_for", [None, "2026-09-27T08:55"])
def test_upsert_preserves_extensions_without_payload_changes(
    conflicted_record, has_conflict, notified_for
):
    record = conflicted_record
    if not has_conflict:
        del record[sync.CONFLICT_KEY]
        record["title"] = "Focus session"
    encoded = json.dumps(record)
    records = {encoded: None}
    activity = replace(activities.decode_activity(encoded), notified_for=notified_for)

    updated = activities.upsert_activity(records, activity)

    assert len(updated) == 1
    assert json.loads(next(iter(updated))) == {**record, "notified_for": notified_for}
    assert records == {encoded: None}


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "Resolved session"},
        {"title": "[Sync conflict] Resolved session"},
        {"title": "Focus session"},
        {"description": "Updated notes"},
        {"start": "09:15"},
        {"end": "10:30"},
        {"alert_minutes_before": None},
    ],
)
def test_upsert_resolves_conflict_only_for_explicit_payload_changes(conflicted_record, changes):
    encoded = json.dumps(conflicted_record)
    records = {encoded: None, "not-json": {"preserve": True}}
    activity = replace(activities.decode_activity(encoded), **changes)
    expected = {**conflicted_record, **changes}
    del expected[sync.CONFLICT_KEY]
    prefix = "[Sync conflict] "
    if expected["title"].startswith(prefix):
        expected["title"] = expected["title"][len(prefix) :]

    updated = activities.upsert_activity(records, activity)

    assert updated["not-json"] == {"preserve": True}
    assert len(updated) == 2
    assert json.loads(next(value for value in updated if value != "not-json")) == expected
    assert records == {encoded: None, "not-json": {"preserve": True}}


def test_upsert_does_not_strip_conflict_like_title_without_metadata():
    original = make_activity(title="[Sync conflict] User title")
    records = {activities.encode_activity(original): None}
    edited = replace(original, description="Updated notes")

    assert activities.decode_activities(activities.upsert_activity(records, edited)) == [edited]


@pytest.mark.parametrize("version", [1, activities.ACTIVITY_VERSION])
def test_mark_notified_preserves_raw_extensions_and_conflict_variants(conflicted_record, version):
    record = conflicted_record
    record["v"] = version
    if version == 1:
        record["text"] = record.pop("title")
        record["alert"] = "08:55"
        del record["alert_minutes_before"]
    else:
        record["title"] = "  " + record["title"] + "  "
        record["description"] = "  " + record["description"] + "  "
    encoded = json.dumps(record)
    other = activities.encode_activity(make_activity(id="activity-2"))
    invalid = json.dumps({**record, "start": "11:00", "end": "10:00"})
    records = {encoded: None, other: None, invalid: {"preserve": True}, "not-json": None}
    token = "2026-09-27T08:55"

    updated = activities.mark_notified(records, "activity-1", token)

    assert len(updated) == 4
    assert updated[other] is None
    assert updated[invalid] == {"preserve": True}
    assert "not-json" in updated
    notified = next(value for value in updated if value not in (other, invalid, "not-json"))
    assert json.loads(notified) == {**record, "notified_for": token}
    assert records == {encoded: None, other: None, invalid: {"preserve": True}, "not-json": None}


def test_mark_notified_leaves_records_unchanged_for_missing_activity(conflicted_record):
    records = {json.dumps(conflicted_record): None, "not-json": None}

    assert activities.mark_notified(records, "missing", "2026-09-27T08:55") == records


def test_remove_last_activity_makes_day_empty():
    month = Month(2026, 9)
    day = month.get_day(27)
    day.upsert_activity(make_activity())

    day.remove_activity("activity-1")

    assert day.activities == []
    assert activities.ACTIVITIES_KEY not in day.content
    assert day.empty


def test_due_activity_is_returned_once():
    month = Month(2026, 9)
    day = month.get_day(27)
    activity = make_activity(alert_minutes_before=5)
    day.upsert_activity(activity)
    now = datetime.datetime(2026, 9, 27, 8, 55, 20)
    previous = datetime.datetime(2026, 9, 27, 8, 54, 50)

    due = activities.get_due_activities([day], previous, now)
    assert due == [(day, activity, "2026-09-27T08:55")]

    day.mark_activity_notified("activity-1", "2026-09-27T08:55")
    assert activities.get_due_activities([day], previous, now) == []


def test_startup_grace_ignores_old_alerts():
    month = Month(2026, 9)
    day = month.get_day(27)
    day.upsert_activity(make_activity(alert_minutes_before=10))

    now = datetime.datetime(2026, 9, 27, 8, 55)

    assert activities.get_due_activities([day], None, now) == []


def test_alert_can_fire_on_previous_day():
    month = Month(2026, 9)
    day = month.get_day(28)
    activity = make_activity(start="00:15", end="01:00", alert_minutes_before=30)
    day.upsert_activity(activity)
    now = datetime.datetime(2026, 9, 27, 23, 45, 20)
    previous = datetime.datetime(2026, 9, 27, 23, 44, 50)

    assert activities.get_due_activities([day], previous, now) == [
        (day, activity, "2026-09-27T23:45")
    ]


def test_clipboard_round_trip_creates_new_activity_ids():
    originals = [make_activity(), make_activity(id="activity-2", title="Second")]

    copied = activities.deserialize_activities_from_clipboard(
        activities.serialize_activities_for_clipboard(originals)
    )

    assert [item.title for item in copied] == ["Focus session", "Second"]
    assert [item.description for item in copied] == [
        "Work without interruptions",
        "Work without interruptions",
    ]
    assert {item.id for item in copied}.isdisjoint({item.id for item in originals})
    assert all(item.notified_for is None for item in copied)


def test_clipboard_rejects_plain_text():
    assert activities.deserialize_activities_from_clipboard("plain text") == []


def test_activity_storage_round_trip(tmp_path):
    month = Month(2026, 9)
    day = month.get_day(27)
    activity = make_activity(title="Planejamento")
    day.upsert_activity(activity)

    assert storage.save_months_to_disk({"2026-09": month}, str(tmp_path))
    loaded = storage.load_all_months_from_disk(str(tmp_path))["2026-09"].get_day(27)

    assert loaded.activities == [activity]
    raw = yaml.safe_load((tmp_path / "2026-09.txt").read_text(encoding="utf-8"))
    records = raw[27][activities.ACTIVITIES_KEY]
    assert isinstance(records, dict)
    assert all(value is None for value in records.values())
