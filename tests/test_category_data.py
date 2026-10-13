import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rednotebook import activities, data, sync
from tests.test_journal_sync import journal_module as journal_module


@pytest.mark.parametrize(
    "value",
    [None, {}, {"entry": None}, {"entry": {"nested": None, "empty": {}}}],
)
def test_category_content_accepts_only_recursive_string_keyed_trees(value):
    content = {"text": "journal text", "Work": value}
    original = copy.deepcopy(content)

    assert data.get_category_content(content) == {"Work": value}
    assert content == original


@pytest.mark.parametrize(
    "value",
    [
        "unknown string",
        5,
        True,
        1.5,
        ["unknown", None],
        {"entry": "scalar"},
        {"entry": [None]},
        {"entry": {"nested": False}},
        {"entry": {1: None}},
        {1: None},
    ],
)
def test_category_content_excludes_extensions_instead_of_exposing_partial_trees(value):
    content = {"text": "journal text", "Tag": None, "extension": value}
    original = copy.deepcopy(content)

    assert data.get_category_content(content) == {"Tag": None}
    assert content == original


def test_reserved_activity_conflict_and_future_metadata_are_not_categories():
    content = {
        "text": "saved",
        "Work": {"entry": None},
        activities.ACTIVITIES_KEY: {"encoded activity": None},
        sync.CONFLICT_KEY: {"version": 1, "conflicts": []},
        "__greennotebook_future_v1__": {"otherwise valid tree": None},
    }

    assert data.get_category_content(content) == {"Work": {"entry": None}}


def test_day_categories_search_and_words_ignore_structured_extensions():
    month = data.Month(
        2026,
        9,
        {
            1: {
                "text": "saved #hashtag",
                "Work": {"visible entry": None},
                "extension": {"private extension": [1, "hidden word"]},
                sync.CONFLICT_KEY: {"version": 1, "conflicts": []},
                "__greennotebook_future_v1__": {"hidden metadata": None},
            }
        },
    )
    day = month.get_day(1)

    assert day.get_category_content_pairs() == {"Work": ["visible entry"], "hashtag": []}
    assert day.categories == ["Work", "hashtag"]
    assert day.search_in_categories("hidden") == []
    assert "hidden" not in day.get_words()
    assert "extension" not in day.get_words()


@pytest.mark.parametrize("changed", [False, True])
def test_save_old_day_preserves_unknown_fields_activities_and_conflicts(journal_module, changed):
    extension = {"nested": {"values": [1, True, None, "kept"]}, "scalar": 3}
    conflict = {
        "version": 1,
        "conflicts": [
            {
                "path": ["extension", "scalar"],
                "versions": [{"revision": "one", "value": 3}, {"revision": "two", "value": 4}],
            }
        ],
    }
    activity = activities.Activity("meeting", "Meeting", "Notes", "09:00", "10:00")
    content = {
        "text": "saved",
        "Work": {"old": None},
        "Removed": None,
        "extension": extension,
        "scalar": "unknown value",
        "list": [1, None],
        "__greennotebook_future_v1__": {"entry": None},
        activities.ACTIVITIES_KEY: {activities.encode_activity(activity): None},
        sync.CONFLICT_KEY: conflict,
    }
    month = data.Month(2026, 9, {1: copy.deepcopy(content)})
    month._sync_durable = True
    day = month.get_day(1)
    old_content = day.content
    categories = {"Work": {"new": None}} if changed else data.get_category_content(content)
    text = "editor edit" if changed else "saved"
    frame = SimpleNamespace(
        template_manager=SimpleNamespace(tmp_title=None),
        categories_tree_view=SimpleNamespace(
            get_day_content=Mock(side_effect=lambda: copy.deepcopy(categories))
        ),
        get_day_text=Mock(return_value=text),
        calendar=Mock(),
    )
    journal = SimpleNamespace(month=month, day=day, date=day.date, frame=frame)

    journal_module.Journal.save_old_day(journal)

    expected = copy.deepcopy(content)
    expected.update(categories)
    expected["text"] = text
    if changed:
        del expected["Removed"]
    assert day.content == expected
    assert old_content == content
    assert day.content["extension"] is not old_content["extension"]
    assert day.content[sync.CONFLICT_KEY] is not old_content[sync.CONFLICT_KEY]
    assert day.activities == [activity]
    assert month.edited is changed
    assert month._sync_durable is (not changed)
    frame.calendar.set_day_edited.assert_called_once_with(1, True)
    categories["mutated after capture"] = None
    old_content["extension"]["nested"]["values"].append("old model mutation")
    assert day.content == expected
