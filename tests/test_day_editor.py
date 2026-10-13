import datetime
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import Mock

import gi
import pytest


gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
from gi.repository import GtkSource

from rednotebook.data import Month
from rednotebook.gui.main_window import DayEditor, MainWindow


DATE = datetime.date(2026, 9, 1)


def buffer_text(buffer):
    return buffer.get_text(*buffer.get_bounds(), True)


def make_buffer(text, suffix=""):
    buffer = GtkSource.Buffer.new()
    buffer.begin_not_undoable_action()
    buffer.set_text(text)
    buffer.end_not_undoable_action()
    if suffix:
        buffer.begin_user_action()
        buffer.insert(buffer.get_end_iter(), suffix)
        buffer.end_user_action()
    return buffer


def make_editor(buffer, active=True):
    unrelated = make_buffer("unrelated", " local edit")
    template = make_buffer("template", " local edit")
    current = buffer if active else unrelated
    editor = SimpleNamespace(
        day=SimpleNamespace(date=DATE if active else DATE + datetime.timedelta(days=1)),
        day_text_buffer=current,
        day_text_view=Mock(),
        recent_buffers=OrderedDict(
            [
                (DATE, buffer),
                (DATE + datetime.timedelta(days=1), unrelated),
                (("template", "Meeting"), template),
            ]
        ),
    )
    editor.day_text_view.get_buffer.return_value = current
    editor.reconcile_day = DayEditor.reconcile_day.__get__(editor)
    return editor


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("history", ["undo", "redo"])
def test_unchanged_cached_buffer_keeps_marks_selection_and_undo(active, history):
    buffer = make_buffer("original\n", "local edit\n")
    if history == "redo":
        buffer.undo()
    text = buffer_text(buffer)
    buffer.select_range(buffer.get_iter_at_offset(2), buffer.get_iter_at_offset(5))
    mark = buffer.create_mark("saved-position", buffer.get_iter_at_offset(3), True)
    buffer.set_modified(False)
    changed = Mock()
    buffer.connect("changed", changed)
    editor = make_editor(buffer, active)
    cached = list(editor.recent_buffers.items())

    editor.reconcile_day(SimpleNamespace(date=DATE, text=text))

    assert list(editor.recent_buffers.items()) == cached
    assert editor.recent_buffers[DATE] is buffer
    assert buffer_text(buffer) == text
    assert buffer.get_iter_at_mark(mark).get_offset() == 3
    assert [iter.get_offset() for iter in buffer.get_selection_bounds()] == [2, 5]
    assert not buffer.get_modified()
    assert buffer.can_undo() == (history == "undo")
    assert buffer.can_redo() == (history == "redo")
    changed.assert_not_called()
    editor.day_text_view.set_buffer.assert_not_called()
    if history == "undo":
        buffer.undo()
        assert buffer_text(buffer) == "original\n"
    else:
        buffer.redo()
        assert buffer_text(buffer) == "original\nlocal edit\n"


@pytest.mark.parametrize("active", [False, True])
def test_changed_cached_buffer_is_one_undoable_merge_without_touching_other_buffers(active):
    original = "original\nkept line\n"
    local = original + "local note\n"
    merged = "REMOTE\nkept line\nlocal note\nextra\n"
    buffer = make_buffer(original, "local note\n")
    offset = local.index("kept line")
    buffer.select_range(buffer.get_iter_at_offset(offset), buffer.get_iter_at_offset(offset + 9))
    mark = buffer.create_mark("saved-position", buffer.get_iter_at_offset(offset + 2), True)
    tag = buffer.create_tag("highlight")
    buffer.apply_tag(tag, buffer.get_iter_at_offset(offset), buffer.get_iter_at_offset(offset + 9))
    editor = make_editor(buffer, active)
    cached = list(editor.recent_buffers.items())
    untouched = [item for item in editor.recent_buffers.values() if item is not buffer]
    changes = [Mock() for _ in untouched]
    for other, changed in zip(untouched, changes):
        other.set_modified(False)
        other.connect("changed", changed)

    editor.reconcile_day(SimpleNamespace(date=DATE, text=merged))

    assert list(editor.recent_buffers.items()) == cached
    assert editor.recent_buffers[DATE] is buffer
    assert editor.day_text_buffer is (buffer if active else untouched[0])
    assert buffer_text(buffer) == merged
    assert buffer.get_iter_at_mark(mark).get_offset() == merged.index("kept line") + 2
    assert buffer.get_text(*buffer.get_selection_bounds(), True) == "kept line"
    assert buffer.get_iter_at_mark(mark).has_tag(tag)
    editor.day_text_view.set_buffer.assert_not_called()
    for other, changed in zip(untouched, changes):
        changed.assert_not_called()
        assert not other.get_modified()
        assert other.can_undo() and not other.can_redo()
    buffer.undo()
    assert buffer_text(buffer) == local
    assert buffer.can_undo()
    buffer.undo()
    assert buffer_text(buffer) == original
    buffer.redo()
    assert buffer_text(buffer) == local
    buffer.redo()
    assert buffer_text(buffer) == merged
    for other, original_text in zip(untouched, ("unrelated", "template")):
        other.undo()
        assert buffer_text(other) == original_text


@pytest.mark.parametrize(
    "before,after",
    [
        ("", "new day"),
        ("deleted remotely", ""),
        ("start middle end", "START middle END"),
        ("one\none\none\n", "one\ninserted\none\n"),
        ("caf\u00e9 \u4e2d old", "caf\u00e9 \u4e2d new \U0001f331"),
    ],
)
def test_reconcile_handles_insert_delete_multiple_hunks_and_unicode(before, after):
    buffer = make_buffer(before)
    editor = make_editor(buffer)

    editor.reconcile_day(SimpleNamespace(date=DATE, text=after))

    assert buffer_text(buffer) == after
    buffer.undo()
    assert buffer_text(buffer) == before
    assert not buffer.can_undo()
    buffer.redo()
    assert buffer_text(buffer) == after


def test_uncached_day_does_not_allocate_or_evict_buffers():
    buffer = make_buffer("current", " edit")
    editor = make_editor(buffer)
    cached = list(editor.recent_buffers.items())
    changed = Mock()
    buffer.connect("changed", changed)

    editor.reconcile_day(SimpleNamespace(date=datetime.date(2026, 10, 1), text="remote"))

    assert list(editor.recent_buffers.items()) == cached
    assert buffer_text(buffer) == "current edit"
    changed.assert_not_called()


@pytest.mark.parametrize("preview", [False, True])
def test_reconcile_journal_updates_cached_days_and_dependent_views(preview):
    september = Month(2026, 9, {1: {"text": "active remote"}, 2: {"text": "unchanged"}})
    october = Month(2026, 10, {3: {"text": "inactive remote"}, 4: {"text": "uncached"}})
    active = september.get_day(1)
    inactive = october.get_day(3)
    active_buffer = make_buffer("active", " local")
    unchanged_buffer = make_buffer("un", "changed")
    inactive_buffer = make_buffer("inactive", " local")
    template_buffer = make_buffer("template", " edit")
    editor = make_editor(active_buffer)
    editor.day = SimpleNamespace(date=active.date, text="outdated model reference")
    editor.recent_buffers = OrderedDict(
        [
            (active.date, active_buffer),
            (september.get_day(2).date, unchanged_buffer),
            (inactive.date, inactive_buffer),
            (("template", "Meeting"), template_buffer),
        ]
    )
    unchanged = Mock()
    unchanged_buffer.connect("changed", unchanged)
    template_changed = Mock()
    template_buffer.connect("changed", template_changed)
    window = SimpleNamespace(
        journal=SimpleNamespace(
            months={"2026-09": september, "2026-10": october},
            month=september,
            day=active,
            get_escaped_tags=Mock(return_value=["#remote"]),
        ),
        day=editor.day,
        day_text_field=editor,
        categories_tree_view=Mock(),
        calendar=Mock(),
        activities_panel=Mock(),
        preview_mode=preview,
        html_editor=Mock(),
        cloud=Mock(),
        search_box=Mock(),
        set_journal_editable=Mock(),
    )
    cached = list(editor.recent_buffers.items())

    MainWindow.reconcile_journal(window)

    assert window.day is active and editor.day is active
    window.set_journal_editable.assert_called_once_with()
    assert list(editor.recent_buffers.items()) == cached
    assert editor.day_text_buffer is active_buffer
    assert buffer_text(active_buffer) == active.text
    assert buffer_text(inactive_buffer) == inactive.text
    unchanged.assert_not_called()
    template_changed.assert_not_called()
    window.categories_tree_view.clear.assert_called_once_with()
    window.categories_tree_view.set_day_content.assert_called_once_with(active)
    window.calendar.set_month.assert_called_once_with(september)
    window.activities_panel.show_day.assert_called_once_with(active)
    window.cloud.update.assert_called_once_with(force_update=True)
    window.search_box.set_entries.assert_called_once_with(["#remote"])
    if preview:
        window.html_editor.show_day.assert_called_once_with(active)
    else:
        window.html_editor.show_day.assert_not_called()
    active_buffer.undo()
    assert buffer_text(active_buffer) == "active local"
    inactive_buffer.undo()
    assert buffer_text(inactive_buffer) == "inactive local"
    unchanged_buffer.undo()
    assert buffer_text(unchanged_buffer) == "un"


@pytest.mark.parametrize(
    "pending,baseline,editable",
    [(False, False, True), (False, True, True), (True, False, False), (True, True, True)],
)
def test_set_journal_editable_blocks_only_pending_unbased_months(pending, baseline, editable):
    month = SimpleNamespace(_sync_pending=pending)
    if baseline:
        month._sync_baseline = {}
    toolbar = Mock()
    add_entry = Mock()
    refresh = Mock()
    details = Mock()
    actions = [Mock(), Mock(), Mock()]
    toolbar.get_children.return_value = [actions[0], refresh, actions[1], details, actions[2]]
    builder = Mock()
    builder.get_object.side_effect = {
        "edit_toolbar": toolbar,
        "add_new_entry_button": add_entry,
    }.__getitem__
    window = SimpleNamespace(
        journal=SimpleNamespace(month=month),
        day_text_field=SimpleNamespace(day_text_view=Mock()),
        categories_tree_view=SimpleNamespace(tree_view=Mock()),
        activities_panel=Mock(),
        builder=builder,
        refresh_button=refresh,
        sync_details_button=details,
    )

    MainWindow.set_journal_editable(window)

    window.day_text_field.day_text_view.set_editable.assert_called_once_with(editable)
    window.categories_tree_view.tree_view.set_sensitive.assert_called_once_with(editable)
    window.activities_panel.set_sensitive.assert_called_once_with(editable)
    add_entry.set_sensitive.assert_called_once_with(editable)
    for action in actions:
        action.set_sensitive.assert_called_once_with(editable)
    refresh.set_sensitive.assert_not_called()
    details.set_sensitive.assert_not_called()


def test_set_date_updates_editability_after_loading_day():
    month = Month(2026, 9, {1: {"text": "pending day"}})
    day = month.get_day(1)
    window = SimpleNamespace(
        journal=SimpleNamespace(config=SimpleNamespace(read=Mock(return_value=False))),
        day=None,
        categories_tree_view=Mock(),
        calendar=Mock(),
        day_text_field=Mock(),
        activities_panel=Mock(),
        html_editor=Mock(),
        preview_mode=False,
        set_journal_editable=Mock(),
    )

    MainWindow.set_date(window, month, day.date, day)

    assert window.day is day
    window.day_text_field.show_day.assert_called_once_with(day)
    window.categories_tree_view.set_day_content.assert_called_once_with(day)
    window.activities_panel.show_day.assert_called_once_with(day)
    window.set_journal_editable.assert_called_once_with()
