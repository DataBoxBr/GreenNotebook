import copy
import datetime
import importlib
import json
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import gi
import pytest


gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
from gi.repository import Gtk, GtkSource

from rednotebook import activities, configuration, data, storage, sync, templates
from rednotebook.gui import activities as activity_ui
from rednotebook.gui import browser, options
from rednotebook.gui.main_window import DayEditor, MainWindow, Statusbar
from rednotebook.util import filesystem


KEY = "2026-09"


@pytest.fixture
def journal_module(tmp_path, monkeypatch):
    # Importing journal parses argv, creates user files, and replaces logging streams.
    with monkeypatch.context() as isolated:
        home = str(tmp_path / "import-home")
        for name in (
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
        ):
            isolated.setenv(name, home)
        isolated.setattr(filesystem, "user_home_dir", home)
        isolated.setattr(sys, "argv", ["pytest"])
        isolated.setattr(sys, "path", sys.path[:])
        isolated.setattr(sys, "stdout", sys.stdout)
        isolated.setattr(sys, "stderr", sys.stderr)
        logger = logging.getLogger()
        isolated.setattr(logger, "handlers", logger.handlers[:])
        isolated.setattr(logger, "level", logger.level)
        module = importlib.import_module("rednotebook.journal")
        stream = sys.stdout.streams[0] if hasattr(sys.stdout, "streams") else None
    if stream is not None:
        stream.close()
    return module


def make_journal(module, directory, months=None):
    months = (
        months
        if months is not None
        else {KEY: data.Month(2026, 9, {1: {"text": "saved", "Work": {"old": None}}})}
    )
    month = months[KEY]
    day = month.get_day(1)
    buffer = GtkSource.Buffer.new()
    buffer.begin_not_undoable_action()
    buffer.set_text(day.text)
    buffer.end_not_undoable_action()
    editor = SimpleNamespace(
        day=day,
        recent_buffers={day.date: buffer},
        clear_buffers=Mock(),
    )
    editor.reconcile_day = DayEditor.reconcile_day.__get__(editor)
    frame = SimpleNamespace(
        day=day,
        day_text_field=editor,
        template_manager=SimpleNamespace(tmp_title=None),
        categories_tree_view=Mock(),
        calendar=Mock(),
        search_box=Mock(),
        cloud=Mock(),
        activities_panel=Mock(),
        preview_mode=False,
        html_editor=Mock(),
        show_save_error_dialog=Mock(),
        set_journal_editable=Mock(),
        get_day_text=Mock(side_effect=lambda: buffer.get_text(*buffer.get_bounds(), True)),
    )
    frame.categories_tree_view.get_day_content.side_effect = lambda: copy.deepcopy(
        data.get_category_content(frame.day.content)
    )
    journal = SimpleNamespace(
        dirs=SimpleNamespace(
            data_dir=str(directory),
            portable=False,
            is_valid_journal_path=Mock(side_effect=os.path.isdir),
        ),
        config=MagicMock(),
        month=month,
        months=months,
        day=day,
        date=day.date,
        actual_date=day.date,
        frame=frame,
        is_first_start=False,
        categories=["Work"],
        _saving=False,
        _options_dialog_open=False,
        _last_save_success=True,
        _last_alert_check="previous check",
        _sync_status=None,
        show_message=Mock(),
        check_alerts=Mock(),
        set_frame_title=Mock(),
        get_escaped_tags=Mock(return_value=["#work"]),
    )
    frame.journal = journal
    frame.reconcile_journal = Mock(side_effect=MainWindow.reconcile_journal.__get__(frame))
    for name in (
        "save_old_day",
        "save_to_disk",
        "open_journal",
        "change_data_directory",
        "reconcile_external_changes",
        "reload_from_disk",
        "get_month",
    ):
        setattr(journal, name, getattr(module.Journal, name).__get__(journal))

    def load_day(date):
        journal.date = date
        journal.month = journal.months[storage.format_year_and_month(date.year, date.month)]
        journal.day = journal.month.get_day(date.day)

    journal.load_day = Mock(side_effect=load_day)
    return journal


def seed_journal(directory, text="base\nsecond\n"):
    directory.mkdir()
    root = sync.make_revision(KEY, [], {}, {1: {"text": text, "Work": {"old": None}}})
    sync.write_revision(directory, root)
    return storage.load_all_months_from_disk(directory)


@pytest.mark.parametrize("conflicting", [False, True])
def test_save_captures_local_editor_branch_before_incorporating_remote(
    journal_module, tmp_path, conflicting
):
    directory = tmp_path / "journal"
    months = seed_journal(directory)
    journal = make_journal(journal_module, directory, months)
    month, day = journal.month, journal.day
    observed = month._sync_heads
    baseline = copy.deepcopy(month._sync_baseline)
    local_text = "LOCAL\nsecond\n"
    remote_text = "REMOTE\nsecond\n" if conflicting else "base\nREMOTE\n"
    remote = sync.make_revision(
        KEY, observed, baseline, {1: {"text": remote_text, "Work": {"old": None}}}
    )
    sync.write_revision(directory, remote)
    buffer = journal.frame.day_text_field.recent_buffers[day.date]
    buffer.begin_user_action()
    buffer.set_text(local_text)
    buffer.end_user_action()
    assert day.text == "base\nsecond\n"

    assert journal.save_to_disk(silent=True, background=True) is True

    graph = sync.read_graph(directory)
    local = [
        record for record in graph.records.values() if record["content"]["1"]["text"] == local_text
    ]
    assert len(local) == 1
    assert local[0]["parents"] == list(observed)
    assert local[0]["base"] == baseline
    assert remote["id"] not in local[0]["parents"]
    assert journal.month is month and journal.day is day
    assert "LOCAL" in day.text and "REMOTE" in day.text
    if conflicting:
        assert "sync conflict" in day.text
        assert local_text in day.text and remote_text in day.text
    else:
        assert day.text == "LOCAL\nREMOTE\n"
    assert journal.frame.get_day_text() == day.text
    journal.frame.reconcile_journal.assert_called_once_with()
    journal.frame.set_journal_editable.assert_called_once_with()
    journal.frame.day_text_field.clear_buffers.assert_not_called()
    assert journal._last_save_success and not journal._saving
    buffer.undo()
    assert journal.frame.get_day_text() == local_text


def test_unchanged_save_does_not_reconcile_or_reset_editor(journal_module, tmp_path):
    directory = tmp_path / "journal"
    journal = make_journal(journal_module, directory, seed_journal(directory))
    buffer = journal.frame.day_text_field.recent_buffers[journal.date]
    changed = Mock()
    buffer.connect("changed", changed)

    assert journal.save_to_disk(silent=True, background=True) is True

    changed.assert_not_called()
    journal.frame.reconcile_journal.assert_not_called()
    journal.frame.day_text_field.clear_buffers.assert_not_called()
    assert journal._last_save_success and not journal._saving


def test_open_after_failed_prior_save_keeps_model_path_and_editor(
    journal_module, tmp_path, monkeypatch
):
    current, selected = tmp_path / "current", tmp_path / "selected"
    current.mkdir()
    selected.mkdir()
    journal = make_journal(journal_module, current)
    months, month, day = journal.months, journal.month, journal.day
    buffers = journal.frame.day_text_field.recent_buffers
    buffers[day.date].set_text("unsaved editor text")
    save = Mock(side_effect=OSError("revision write failed"))
    load = Mock()
    monkeypatch.setattr(storage, "save_months_to_disk", save)
    monkeypatch.setattr(storage, "load_all_months_from_disk", load)

    assert journal.open_journal(str(selected)) is False

    load.assert_not_called()
    assert journal.months is months and journal.month is month and journal.day is day
    assert journal.dirs.data_dir == str(current)
    assert day.text == "unsaved editor text" and month.edited
    assert journal.frame.day_text_field.recent_buffers is buffers
    assert journal.frame.get_day_text() == "unsaved editor text"
    journal.frame.day_text_field.clear_buffers.assert_not_called()
    journal.frame.search_box.clear.assert_not_called()
    journal.load_day.assert_not_called()
    journal.config.__setitem__.assert_not_called()
    journal.config.save_to_disk.assert_not_called()
    journal.frame.show_save_error_dialog.assert_called_once_with(False)
    assert not journal._last_save_success and not journal._saving


@pytest.mark.parametrize(
    "error",
    [
        OSError("unreadable"),
        ValueError("invalid"),
        TypeError("schema"),
        AssertionError("bad day"),
        SystemExit(1),
    ],
)
@pytest.mark.parametrize("save_current", [False, True])
def test_failed_destination_load_retains_current_path_models_and_buffers(
    journal_module, tmp_path, monkeypatch, error, save_current
):
    current, selected = tmp_path / "current", tmp_path / "selected"
    current.mkdir()
    selected.mkdir()
    journal = make_journal(journal_module, current)
    journal.save_to_disk = Mock(return_value=True)
    months, month, day = journal.months, journal.month, journal.day
    content = copy.deepcopy(day.content)
    buffers = journal.frame.day_text_field.recent_buffers.copy()
    buffer = buffers[day.date]
    buffer.begin_user_action()
    buffer.insert(buffer.get_end_iter(), " local editor edit")
    buffer.end_user_action()
    monkeypatch.setattr(storage, "load_all_months_from_disk", Mock(side_effect=error))

    assert journal.open_journal(str(selected), save_current=save_current) is False

    assert journal.dirs.data_dir == str(current)
    assert journal.months is months and journal.month is month and journal.day is day
    assert day.content == content
    assert journal.frame.day_text_field.recent_buffers == buffers
    assert journal.frame.get_day_text() == content["text"] + " local editor edit"
    assert buffer.can_undo() and not buffer.can_redo()
    assert journal._last_alert_check == "previous check"
    journal.frame.search_box.clear.assert_not_called()
    journal.frame.day_text_field.clear_buffers.assert_not_called()
    journal.load_day.assert_not_called()
    journal.config.__setitem__.assert_not_called()
    journal.config.save_to_disk.assert_not_called()
    assert journal.save_to_disk.call_count == int(save_current)
    journal.show_message.assert_called_once()
    assert journal.show_message.call_args.kwargs == {"error": True}
    buffer.undo()
    assert journal.frame.get_day_text() == content["text"]


def test_copy_journal_retains_whole_revision_history_and_other_files(
    journal_module, tmp_path, monkeypatch
):
    current, selected = tmp_path / "current", tmp_path / "selected"
    journal = make_journal(journal_module, current, seed_journal(current))
    selected.mkdir()
    for text in ("first edit", "second edit"):
        journal.frame.day_text_field.recent_buffers[journal.date].set_text(text)
        journal.save_to_disk()
    attachments = current / "attachments" / "nested"
    attachments.mkdir(parents=True)
    (attachments / "picture.bin").write_bytes(b"attachment contents")
    journal.frame.day_text_field.recent_buffers[journal.date].set_text("copy-time editor edit")
    monkeypatch.setattr(journal_module, "Statistics", Mock())

    assert journal.change_data_directory(str(selected), copy_current=True) is True

    source = {
        path.relative_to(current): path.read_bytes()
        for path in current.rglob("*")
        if path.is_file()
    }
    destination = {
        path.relative_to(selected): path.read_bytes()
        for path in selected.rglob("*")
        if path.is_file()
    }
    assert source == destination
    original_graph = sync.read_graph(current)
    copied_graph = sync.read_graph(selected)
    assert len(original_graph.records) == 4
    assert copied_graph.records == original_graph.records
    assert copied_graph.heads(KEY) == original_graph.heads(KEY)
    assert copied_graph.complete(KEY)
    assert journal.dirs.data_dir == str(selected)
    assert journal.day.text == "copy-time editor edit"
    assert journal.month._sync_directory == os.path.normcase(os.path.abspath(selected))
    journal.config.__setitem__.assert_called_once_with("dataDir", str(selected))
    journal.load_day.assert_called_once_with(journal.date)
    copied_heads = copied_graph.heads(KEY)
    journal.frame.day_text_field.recent_buffers[journal.date].set_text("edit in copied journal")
    assert journal.save_to_disk() is True
    copied_graph = sync.read_graph(selected)
    assert len(copied_graph.records) == 5
    assert copied_graph.records[journal.month._sync_heads[0]]["parents"] == list(copied_heads)
    assert sync.read_graph(current).records == original_graph.records


@pytest.mark.parametrize("kind", ["nonempty", "child", "parent"])
def test_copy_rejects_nonempty_and_nested_destinations_before_saving(
    journal_module, tmp_path, kind
):
    current = tmp_path / "current"
    current.mkdir()
    journal = make_journal(journal_module, current)
    if kind == "nonempty":
        selected = tmp_path / "selected"
        selected.mkdir()
        (selected / "existing.txt").write_text("keep", encoding="utf-8")
    elif kind == "child":
        selected = current / "nested"
        selected.mkdir()
    else:
        selected = tmp_path
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    journal.save_to_disk = Mock()
    journal.open_journal = Mock()

    assert journal.change_data_directory(str(selected), copy_current=True) is False

    assert journal.dirs.data_dir == str(current)
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before
    journal.save_to_disk.assert_not_called()
    journal.open_journal.assert_not_called()
    journal.show_message.assert_called_once()


@pytest.mark.parametrize("reason", ["save", "pending", "copy"])
def test_copy_failure_does_not_switch_journals(journal_module, tmp_path, monkeypatch, reason):
    current, selected = tmp_path / "current", tmp_path / "selected"
    current.mkdir()
    selected.mkdir()
    journal = make_journal(journal_module, current)
    journal._last_save_success = reason != "save"
    journal.save_to_disk = Mock(return_value=True)
    journal.open_journal = Mock()
    monkeypatch.setattr(storage, "pending_revisions", Mock(return_value=reason == "pending"))
    copytree = Mock(side_effect=OSError("copy failed"))
    monkeypatch.setattr(journal_module.shutil, "copytree", copytree)

    assert journal.change_data_directory(str(selected), copy_current=True) is False

    assert journal.dirs.data_dir == str(current)
    journal.save_to_disk.assert_called_once_with(changing_journal=True)
    journal.open_journal.assert_not_called()
    assert copytree.call_count == int(reason == "copy")


@pytest.mark.parametrize("path", ["", " ", "\t\n"])
def test_empty_directory_request_does_not_save_or_switch(journal_module, tmp_path, path):
    journal = make_journal(journal_module, tmp_path)
    journal.save_to_disk = Mock()
    journal.open_journal = Mock()

    assert journal.change_data_directory(path, copy_current=True) is False

    journal.dirs.is_valid_journal_path.assert_not_called()
    journal.save_to_disk.assert_not_called()
    journal.open_journal.assert_not_called()
    assert journal.dirs.data_dir == str(tmp_path)


@pytest.mark.parametrize("title", ["Meeting", ""])
def test_template_edit_cannot_be_captured_as_day_content(journal_module, tmp_path, title):
    journal = make_journal(journal_module, tmp_path)
    journal.frame.template_manager.tmp_title = title
    journal.frame.day_text_field.recent_buffers[journal.date].set_text("template-only text")
    before = copy.deepcopy(journal.day.content)
    journal.month._sync_durable = True

    journal.save_old_day()

    assert journal.day.content == before
    assert not journal.month.edited and journal.month._sync_durable
    journal.frame.get_day_text.assert_not_called()
    journal.frame.categories_tree_view.get_day_content.assert_not_called()
    journal.frame.calendar.set_day_edited.assert_not_called()


@pytest.mark.parametrize("copy_current", [False, True])
def test_template_edit_blocks_folder_changes(journal_module, tmp_path, copy_current):
    current, selected = tmp_path / "current", tmp_path / "selected"
    current.mkdir()
    selected.mkdir()
    journal = make_journal(journal_module, current)
    journal.frame.template_manager.tmp_title = "Meeting"
    journal.save_to_disk = Mock()
    journal.open_journal = Mock()

    assert journal.change_data_directory(str(selected), copy_current=copy_current) is False

    assert journal.dirs.data_dir == str(current)
    journal.save_to_disk.assert_not_called()
    journal.open_journal.assert_not_called()


@pytest.mark.parametrize("blocked", ["saving", "options", "no-month", "template", "empty-template"])
def test_poll_skips_unsafe_capture_states(journal_module, tmp_path, blocked):
    journal = make_journal(journal_module, tmp_path)
    journal._saving = blocked == "saving"
    journal._options_dialog_open = blocked == "options"
    if blocked == "no-month":
        journal.month = None
    if blocked in ("template", "empty-template"):
        journal.frame.template_manager.tmp_title = "Meeting" if blocked == "template" else ""
    journal.save_to_disk = Mock()

    assert journal.reconcile_external_changes() is True

    journal.save_to_disk.assert_not_called()


def test_poll_uses_silent_background_save(journal_module, tmp_path):
    journal = make_journal(journal_module, tmp_path)
    journal.save_to_disk = Mock(return_value=True)

    assert journal.reconcile_external_changes() is True

    journal.save_to_disk.assert_called_once_with(silent=True, background=True)


@pytest.mark.parametrize(
    "content,pending,message",
    [
        ({"text": "saved"}, False, "Saved locally"),
        ({"text": "saved"}, True, "Waiting for complete revision files"),
        ({"text": "<<<<<<< revision abc\nlocal\n"}, False, "Sync conflicts preserved"),
        (
            {"text": "saved", sync.CONFLICT_KEY: {"version": 1, "conflicts": []}},
            False,
            "Sync conflicts preserved",
        ),
        (
            {"text": "saved", sync.CONFLICT_KEY: {"version": 1, "conflicts": []}},
            True,
            "Waiting for complete revision files",
        ),
        (
            {
                "text": "saved",
                activities.ACTIVITIES_KEY: {
                    sync.canonical_json(
                        {
                            "v": 2,
                            "id": "one",
                            "title": "[Sync conflict] Meeting",
                            "description": "",
                            "start": "09:00",
                            "end": "10:00",
                            "alert_minutes_before": None,
                            "notified_for": None,
                            sync.CONFLICT_KEY: {"variants": []},
                        }
                    ): None,
                },
            },
            False,
            "Sync conflicts preserved",
        ),
    ],
)
def test_background_save_reports_pending_and_conflicts_without_repeat_notifications(
    journal_module, tmp_path, monkeypatch, content, pending, message
):
    journal = make_journal(journal_module, tmp_path)
    journal.day._content = copy.deepcopy(content)
    journal.frame.day_text_field.recent_buffers[journal.date].set_text(content["text"])
    journal.month._sync_pending = pending
    monkeypatch.setattr(storage, "save_months_to_disk", Mock(return_value=False))

    assert journal.save_to_disk(silent=True, background=True) is True
    assert journal._last_save_success
    journal.show_message.assert_called_once()
    assert message in journal.show_message.call_args.args[0]
    assert journal.show_message.call_args.kwargs == {"error": False}
    journal.show_message.reset_mock()

    assert journal.save_to_disk(silent=True, background=True) is True

    journal.show_message.assert_not_called()
    journal.frame.show_save_error_dialog.assert_not_called()


def test_background_save_failure_is_nonmodal_retains_edits_and_resets_guard(
    journal_module, tmp_path, monkeypatch
):
    journal = make_journal(journal_module, tmp_path)
    journal.frame.day_text_field.recent_buffers[journal.date].set_text("local unsaved")
    monkeypatch.setattr(storage, "save_months_to_disk", Mock(side_effect=OSError("offline")))

    assert journal.save_to_disk(silent=True, background=True) is True

    assert not journal._last_save_success and not journal._saving
    assert journal.day.text == "local unsaved" and journal.month.edited
    assert journal.frame.get_day_text() == "local unsaved"
    journal.frame.show_save_error_dialog.assert_not_called()
    journal.show_message.assert_called_once()
    assert journal.show_message.call_args.kwargs == {"error": True}
    journal.show_message.reset_mock()

    assert journal.save_to_disk(silent=True, background=True) is True

    journal.show_message.assert_not_called()


def test_reentrant_save_does_not_capture_or_write(journal_module, tmp_path, monkeypatch):
    journal = make_journal(journal_module, tmp_path)
    journal._saving = True
    journal._last_save_success = False
    journal.save_old_day = Mock()
    save = Mock()
    monkeypatch.setattr(storage, "save_months_to_disk", save)

    assert journal.save_to_disk() is True

    journal.save_old_day.assert_not_called()
    save.assert_not_called()
    assert journal._saving and not journal._last_save_success


@pytest.mark.parametrize("modal", [False, True])
@pytest.mark.parametrize("visible", [False, True])
def test_poll_is_blocked_only_by_visible_modal_dialogs(journal_module, tmp_path, modal, visible):
    journal = make_journal(journal_module, tmp_path)
    journal.save_to_disk = Mock(return_value=True)
    dialog = Gtk.Dialog(modal=modal)
    try:
        if visible:
            dialog.show_all()
        assert dialog in Gtk.Window.list_toplevels()
        assert dialog.get_visible() is visible and dialog.get_modal() is modal

        assert journal.reconcile_external_changes() is True

        if modal and visible:
            journal.save_to_disk.assert_not_called()
            dialog.hide()
            assert journal.reconcile_external_changes() is True
        journal.save_to_disk.assert_called_once_with(silent=True, background=True)
    finally:
        dialog.destroy()


@pytest.mark.parametrize("conflicting", [False, True])
def test_activity_dialog_commits_stale_local_fields_before_merging_remote(
    journal_module, tmp_path, monkeypatch, conflicting
):
    directory = tmp_path / "journal"
    directory.mkdir()
    original = activities.Activity("one", "Meeting", "Base notes", "09:00", "10:00", 5)
    initial = sync.make_revision(
        KEY,
        [],
        {},
        {
            1: {
                "text": "saved",
                activities.ACTIVITIES_KEY: {activities.encode_activity(original): None},
            }
        },
    )
    sync.write_revision(directory, initial)
    journal = make_journal(journal_module, directory, storage.load_all_months_from_disk(directory))
    observed = journal.month._sync_heads
    baseline = copy.deepcopy(journal.month._sync_baseline)
    remote_activity = activities.Activity(
        "one",
        "Remote meeting" if conflicting else "Meeting",
        "Remote notes",
        "09:00" if conflicting else "09:30",
        "10:00" if conflicting else "10:30",
        5,
    )
    remote_content = copy.deepcopy(baseline)
    remote_content["1"][activities.ACTIVITIES_KEY] = {
        activities.encode_activity(remote_activity): None,
    }
    remote = sync.make_revision(KEY, observed, baseline, remote_content)
    local_activity = activities.Activity(
        "one",
        "Local meeting",
        "Local notes" if conflicting else "Base notes",
        "09:00",
        "10:00",
        5,
    )
    journal.save_to_disk = Mock(side_effect=journal.save_to_disk)
    parent = Gtk.Window()
    journal.frame.main_frame = parent
    dialog = activity_ui.ActivityDialog(parent, original)
    panel = SimpleNamespace(
        main_window=journal.frame,
        journal=journal,
        day=journal.day,
        _selected_activity=Mock(return_value=original),
        show_day=Mock(),
    )
    panel._save_activity = activity_ui.ActivitiesPanel._save_activity.__get__(panel)
    panel._save_changes = activity_ui.ActivitiesPanel._save_changes.__get__(panel)
    factory = Mock(return_value=dialog)
    monkeypatch.setattr(activity_ui, "ActivityDialog", factory)
    buffer = journal.frame.day_text_field.recent_buffers[journal.date]
    buffer_changed = Mock()
    buffer.connect("changed", buffer_changed)

    def save_dialog(window):
        assert window is dialog.dialog
        assert window.get_visible() and window.get_modal()
        assert dialog.title_entry.get_text() == original.title
        assert (
            dialog.description_buffer.get_text(*dialog.description_buffer.get_bounds(), True)
            == original.description
        )
        sync.write_revision(directory, remote)

        assert journal.reconcile_external_changes() is True

        journal.save_to_disk.assert_not_called()
        journal.frame.reconcile_journal.assert_not_called()
        buffer_changed.assert_not_called()
        assert journal.day.activities == [original]
        assert journal.month._sync_heads == observed
        dialog.title_entry.set_text(local_activity.title)
        dialog.description_buffer.set_text(local_activity.description)
        assert dialog.start_input.get_value() == original.start
        assert dialog.end_input.get_value() == original.end
        return Gtk.ResponseType.OK

    monkeypatch.setattr(Gtk.Dialog, "run", save_dialog)
    try:
        activity_ui.ActivitiesPanel._on_edit(panel, None)

        factory.assert_called_once_with(parent, original)
        journal.save_to_disk.assert_called_once_with(silent=True)
        journal.frame.reconcile_journal.assert_called_once_with()
        journal.check_alerts.assert_called_once_with(reset=True)
        assert dialog.dialog not in Gtk.Window.list_toplevels()
        graph = sync.read_graph(directory)
        local_record = [
            record
            for record in graph.records.values()
            if record["id"] not in (initial["id"], remote["id"])
        ]
        assert len(local_record) == 1
        local_record = local_record[0]
        assert local_record["parents"] == list(observed)
        assert local_record["base"] == baseline
        assert local_record["content"]["1"][activities.ACTIVITIES_KEY] == {
            activities.encode_activity(local_activity): None,
        }
        assert remote["id"] not in local_record["parents"]
        assert set(graph.heads(KEY)) == {local_record["id"], remote["id"]}
        assert len(journal.day.activities) == 1
        merged = json.loads(next(iter(journal.day.content[activities.ACTIVITIES_KEY])))
        if conflicting:
            variants = {sync.canonical_json(item) for item in merged[sync.CONFLICT_KEY]["variants"]}
            assert activities.encode_activity(local_activity) in variants
            assert activities.encode_activity(remote_activity) in variants
        else:
            assert journal.day.activities == [
                activities.Activity(
                    "one",
                    "Local meeting",
                    "Remote notes",
                    "09:30",
                    "10:30",
                    5,
                )
            ]
            assert sync.CONFLICT_KEY not in merged
        assert journal._last_save_success and not journal._saving
        assert journal.frame.get_day_text() == "saved"
        journal.frame.day_text_field.clear_buffers.assert_not_called()
        buffer_changed.assert_not_called()
    finally:
        dialog.dialog.destroy()
        parent.destroy()


@pytest.mark.parametrize("title", ["Meeting", ""])
def test_reload_rejects_template_edit_without_discarding_buffers(journal_module, tmp_path, title):
    journal = make_journal(journal_module, tmp_path)
    journal.frame.template_manager.tmp_title = title
    template = GtkSource.Buffer.new()
    template.set_text("unsaved template text")
    journal.frame.day_text_field.recent_buffers[("template", title)] = template
    buffers = journal.frame.day_text_field.recent_buffers.copy()
    content = copy.deepcopy(journal.day.content)
    journal.open_journal = Mock()

    journal.reload_from_disk()

    journal.open_journal.assert_not_called()
    journal.check_alerts.assert_not_called()
    assert journal.day.content == content
    assert journal.frame.day_text_field.recent_buffers == buffers
    assert template.get_text(*template.get_bounds(), True) == "unsaved template text"
    journal.frame.day_text_field.clear_buffers.assert_not_called()
    journal.frame.search_box.clear.assert_not_called()
    journal.show_message.assert_called_once_with(
        "Close the template editor before reloading the journal.",
        error=True,
    )


@pytest.mark.parametrize("title", ["Meeting", ""])
def test_template_refresh_bypasses_destructive_warning(
    journal_module, tmp_path, monkeypatch, title
):
    journal = make_journal(journal_module, tmp_path)
    journal.frame.template_manager.tmp_title = title
    journal.open_journal = Mock()
    journal.reload_from_disk = Mock(side_effect=journal.reload_from_disk)
    journal.frame._has_unsaved_changes = Mock(
        side_effect=AssertionError("must not check day edits")
    )
    warning = Mock()
    monkeypatch.setattr(Gtk, "MessageDialog", warning)
    buffers = journal.frame.day_text_field.recent_buffers.copy()

    MainWindow.on_refresh_button_clicked(journal.frame, None)

    journal.frame._has_unsaved_changes.assert_not_called()
    warning.assert_not_called()
    journal.reload_from_disk.assert_called_once_with()
    journal.open_journal.assert_not_called()
    journal.check_alerts.assert_not_called()
    assert journal.frame.day_text_field.recent_buffers == buffers
    journal.frame.day_text_field.clear_buffers.assert_not_called()
    journal.show_message.assert_called_once_with(
        "Close the template editor before reloading the journal.",
        error=True,
    )


@pytest.mark.parametrize("opened", [False, None, True])
def test_reload_reports_success_and_resets_alerts_only_after_successful_reopen(
    journal_module, tmp_path, opened
):
    journal = make_journal(journal_module, tmp_path)
    journal.open_journal = Mock(return_value=opened)

    journal.reload_from_disk()

    journal.open_journal.assert_called_once_with(
        str(tmp_path),
        save_current=False,
        start_date=journal.date,
    )
    if opened:
        journal.check_alerts.assert_called_once_with(reset=True)
        journal.show_message.assert_called_once_with("Journal data reloaded from disk", error=False)
    else:
        journal.check_alerts.assert_not_called()
        journal.show_message.assert_not_called()


def test_sync_details_dialog_shows_both_activity_variants_without_changing_buffers(
    journal_module, tmp_path, monkeypatch
):
    journal = make_journal(journal_module, tmp_path)
    left = json.loads(
        activities.encode_activity(
            activities.Activity(
                "one",
                "Local meeting",
                "Local notes",
                "09:00",
                "10:00",
                5,
            )
        )
    )
    right = json.loads(
        activities.encode_activity(
            activities.Activity(
                "one",
                "Remote meeting",
                "Remote notes",
                "09:00",
                "10:00",
                5,
            )
        )
    )
    record = {
        **left,
        "title": "[Sync conflict] Local meeting",
        sync.CONFLICT_KEY: {"variants": [left, right], "revisions": ["left", "right"]},
    }
    journal.day.content[activities.ACTIVITIES_KEY] = {sync.canonical_json(record): None}
    journal.day.content[sync.CONFLICT_KEY] = {"version": 1, "conflicts": []}
    journal.month._sync_pending = True
    before = copy.deepcopy(journal.day.content)
    buffer = journal.frame.day_text_field.recent_buffers[journal.date]
    buffer.begin_user_action()
    buffer.insert(buffer.get_end_iter(), " unsaved editor edit")
    buffer.end_user_action()
    buffer.select_range(buffer.get_iter_at_offset(1), buffer.get_iter_at_offset(3))
    changed = Mock()
    buffer.connect("changed", changed)
    journal.save_to_disk = Mock()
    parent = journal.frame.main_frame = Gtk.Window()
    shown = []

    def close_dialog(dialog):
        assert isinstance(dialog, Gtk.Dialog)
        assert dialog.get_visible() and dialog.get_modal()
        assert dialog.get_transient_for() is parent
        scroller = next(
            child
            for child in dialog.get_content_area().get_children()
            if isinstance(child, Gtk.ScrolledWindow)
        )
        view = scroller.get_child()
        assert isinstance(view, Gtk.TextView) and not view.get_editable()
        details = json.loads(view.get_buffer().get_text(*view.get_buffer().get_bounds(), True))
        shown.append(dialog)
        assert details == {
            "folder": str(tmp_path),
            "date": str(journal.date),
            "pending_revisions": True,
            "google_drive_upload_verified": False,
            "conflict_metadata_key": sync.CONFLICT_KEY,
            "day": {**before, activities.ACTIVITIES_KEY: [record]},
        }
        assert details["day"][activities.ACTIVITIES_KEY][0][sync.CONFLICT_KEY]["variants"] == [
            left,
            right,
        ]
        assert journal.reconcile_external_changes() is True
        journal.save_to_disk.assert_not_called()
        return Gtk.ResponseType.CLOSE

    monkeypatch.setattr(Gtk.Dialog, "run", close_dialog)
    try:
        MainWindow.on_sync_details_clicked(journal.frame, None)

        assert len(shown) == 1 and shown[0] not in Gtk.Window.list_toplevels()
        assert journal.day.content == before
        assert journal.frame.day_text_field.recent_buffers[journal.date] is buffer
        assert journal.frame.get_day_text() == "saved unsaved editor edit"
        assert [iter.get_offset() for iter in buffer.get_selection_bounds()] == [1, 3]
        changed.assert_not_called()
        journal.frame.day_text_field.clear_buffers.assert_not_called()
        journal.frame.reconcile_journal.assert_not_called()
        buffer.undo()
        assert journal.frame.get_day_text() == "saved"
    finally:
        for dialog in shown:
            dialog.destroy()
        parent.destroy()


@pytest.fixture
def real_journal_window(journal_module, tmp_path, monkeypatch):
    home = str(tmp_path / "smoke-home")
    for name in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.setenv(name, home)
    for name, value in {
        "GSETTINGS_BACKEND": "memory",
        "GIO_USE_VFS": "local",
        "GTK_USE_PORTAL": "0",
        "DBUS_SESSION_BUS_ADDRESS": "disabled:",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(filesystem, "user_home_dir", home)
    defaults = configuration.Config(journal_module.default_config_file)
    defaults["userDir"] = str(tmp_path / "smoke-user")
    defaults["portable"] = 0
    dirs = filesystem.Filenames(defaults)
    config = configuration.Config(dirs.config_file)
    config.update(firstStart=0, useInternalPreview=0, showTagsPane=1, checkForNewVersion=0)
    config.save_to_disk()
    monkeypatch.setattr(journal_module, "dirs", dirs)
    monkeypatch.setattr(journal_module, "default_config", defaults)
    monkeypatch.setattr(browser, "WebKit2", None)
    monkeypatch.setattr(Statusbar, "start_countdown", Mock())
    # These UI globals are populated or formatted by their real constructors.
    monkeypatch.setattr(templates, "help_text", templates.help_text)
    for name in ("config", "main_window"):
        monkeypatch.setattr(
            options.Option, name, getattr(options.Option, name, None), raising=False
        )
    unexpected_dialog = Mock(side_effect=AssertionError("unexpected interactive GTK dialog"))
    monkeypatch.setattr(Gtk.Dialog, "run", unexpected_dialog)
    notifier = Mock(side_effect=AssertionError("activation must not start a notifier"))
    check_version = Mock(side_effect=AssertionError("smoke test must not check the network"))
    monkeypatch.setattr(journal_module, "DesktopNotifier", notifier)
    monkeypatch.setattr(journal_module.utils, "check_new_version", check_version)
    existing_windows = Gtk.Window.list_toplevels()
    settings = Gtk.Settings.get_default()
    prefer_dark = settings.get_property("gtk-application-prefer-dark-theme")
    journal = journal_module.Journal()
    journal.set_flags(journal.get_flags() | journal_module.Gio.ApplicationFlags.NON_UNIQUE)
    try:
        assert journal.register(None)
        assert journal.get_is_registered() and not journal.get_is_remote()
        assert journal.frame is None
        assert journal.dirs is dirs
        journal.actual_date = datetime.date(2026, 9, 1)
        journal.frame = MainWindow(journal)
        assert journal.frame.main_frame.get_application() is journal
        yield journal
        assert journal.notifier is None
        assert journal._sync_timer_id is None and journal.alert_timer_id is None
        assert not hasattr(journal, "archiver")
        unexpected_dialog.assert_not_called()
        notifier.assert_not_called()
        check_version.assert_not_called()
    finally:
        if journal.frame is not None and journal.frame.tray_icon is not None:
            journal.frame.tray_icon.set_visible(False)
        for window in Gtk.Window.list_toplevels():
            if window not in existing_windows:
                window.destroy()
        journal.quit()
        settings.set_property("gtk-application-prefer-dark-theme", prefer_dark)


def test_real_main_window_smoke_preferences_save_merge_and_pending_recovery(
    real_journal_window, tmp_path, monkeypatch
):
    journal = real_journal_window
    window = journal.frame
    directory = tmp_path / "normal-journal"
    directory.mkdir()
    initial = sync.make_revision(
        KEY,
        [],
        {},
        {
            1: {"text": "first\nsecond\n", "Work": {"base": None}},
            2: {"text": "cached first\ncached second\n", "Other": None},
            3: {"text": "unchanged", "Unchanged": None},
        },
    )
    sync.write_revision(directory, initial)

    assert journal.open_journal(str(directory), save_current=False, start_date=journal.actual_date)
    assert window.main_frame.get_visible()
    assert window.get_day_text() == journal.day.text == "first\nsecond\n"
    assert window.categories_tree_view.get_day_content() == {"Work": {"base": None}}
    assert isinstance(window.categories_tree_view.cell, Gtk.CellRendererText)
    assert window.categories_tree_view.cell.get_property("editing") is False
    assert window.day_text_field.day_text_view.get_editable()
    assert window.activities_panel.day is journal.day

    cancelled = []

    def cancel_preferences(dialog):
        assert dialog is window.options_manager.dialog
        assert isinstance(dialog.dialog, Gtk.Dialog)
        assert dialog.dialog.get_transient_for() is window.main_frame
        assert journal._options_dialog_open
        manager = window.options_manager
        assert isinstance(manager.journal_directory_option, options.JournalDirectoryOption)
        assert manager.journal_directory_option.get_value() == str(directory)
        assert manager.journal_directory_option not in manager.options
        widgets = dialog.categories["general"].get_children()
        assert manager.journal_directory_option in widgets
        assert any(isinstance(widget, options.DateFormatOption) for widget in widgets)
        assert any(isinstance(widget, options.FontOption) for widget in widgets)
        assert any(isinstance(widget, options.IntegerOption) for widget in widgets)
        dialog.dialog.show_all()
        cancelled.append(dialog)
        return Gtk.ResponseType.CANCEL

    monkeypatch.setattr(options.OptionsDialog, "run", cancel_preferences, raising=False)
    window.options_manager.on_options_dialog()
    assert cancelled == [window.options_manager.dialog]
    assert not window.options_manager.dialog.dialog.get_visible()
    assert not journal._options_dialog_open
    assert journal.dirs.data_dir == str(directory)

    active_date = journal.date
    inactive_date = active_date.replace(day=2)
    unchanged_date = active_date.replace(day=3)
    active_buffer = window.day_text_field.day_text_buffer
    journal.change_date(inactive_date)
    inactive_buffer = window.day_text_field.day_text_buffer
    inactive_buffer.begin_user_action()
    inactive_buffer.set_text("LOCAL cached\ncached second\n")
    inactive_buffer.end_user_action()
    journal.change_date(unchanged_date)
    unchanged_buffer = window.day_text_field.day_text_buffer
    unchanged_buffer.begin_user_action()
    unchanged_buffer.insert(unchanged_buffer.get_end_iter(), " temporary edit")
    unchanged_buffer.end_user_action()
    unchanged_buffer.undo()
    assert unchanged_buffer.can_redo()
    journal.change_date(active_date)
    assert window.day_text_field.day_text_buffer is active_buffer
    active_buffer.begin_user_action()
    active_buffer.set_text("LOCAL\nsecond\n")
    active_buffer.end_user_action()
    window.categories_tree_view.add_entry("Local", "entry")

    assert journal.save_to_disk(silent=True)
    assert journal._last_save_success
    local_heads = journal.month._sync_heads
    local_graph = sync.read_graph(directory)
    assert local_graph.records[local_heads[0]]["parents"] == [initial["id"]]
    assert local_graph.records[local_heads[0]]["content"]["1"]["text"] == "LOCAL\nsecond\n"
    assert local_graph.records[local_heads[0]]["content"]["2"]["text"] == (
        "LOCAL cached\ncached second\n"
    )
    remote_content = copy.deepcopy(initial["content"])
    remote_content["1"]["text"] = "first\nREMOTE\n"
    remote_content["1"]["Remote"] = {"entry": None}
    remote_content["2"]["text"] = "cached first\nREMOTE cached\n"
    remote_content["4"] = {"text": "new remote day", "New": None}
    remote = sync.make_revision(KEY, [initial["id"]], initial["content"], remote_content)
    sync.write_revision(directory, remote)
    cached = dict(window.day_text_field.recent_buffers)
    unchanged = Mock()
    unchanged_buffer.connect("changed", unchanged)

    assert journal.reconcile_external_changes()

    assert journal._last_save_success
    assert window.day_text_field.day_text_buffer is active_buffer
    assert window.day_text_field.recent_buffers == cached
    assert window.get_day_text() == journal.day.text == "LOCAL\nREMOTE\n"
    assert inactive_buffer.get_text(*inactive_buffer.get_bounds(), True) == (
        "LOCAL cached\nREMOTE cached\n"
    )
    assert journal.month.get_day(2).text == "LOCAL cached\nREMOTE cached\n"
    assert journal.month.get_day(4).text == "new remote day"
    assert window.categories_tree_view.get_day_content() == {
        "Work": {"base": None},
        "Local": {"entry": None},
        "Remote": {"entry": None},
    }
    assert {"#local", "#remote", "#new"} <= window.search_box.entries
    assert set(journal.month._sync_heads) == {*local_heads, remote["id"]}
    unchanged.assert_not_called()
    assert unchanged_buffer.can_redo()
    active_buffer.undo()
    assert window.get_day_text() == "LOCAL\nsecond\n"
    active_buffer.redo()
    assert window.get_day_text() == "LOCAL\nREMOTE\n"
    inactive_buffer.undo()
    assert inactive_buffer.get_text(*inactive_buffer.get_bounds(), True) == (
        "LOCAL cached\ncached second\n"
    )
    inactive_buffer.redo()

    pending_directory = tmp_path / "pending-journal"
    pending_directory.mkdir()
    missing_parent = sync.make_revision(
        KEY,
        [],
        {},
        {
            1: {"text": "complete base", "Complete": None},
        },
    )
    child = sync.make_revision(
        KEY,
        [missing_parent["id"]],
        missing_parent["content"],
        {
            1: {"text": "complete remote", "Ready": None},
        },
    )
    sync.write_revision(pending_directory, child)
    (pending_directory / f"{KEY}.txt").write_text(
        "1:\n  text: stale cache\n  Stale: null\n",
        encoding="utf-8",
    )

    assert journal.open_journal(str(pending_directory), save_current=False, start_date=active_date)
    pending_day = journal.day
    pending_buffer = window.day_text_field.day_text_buffer
    assert journal.month._sync_pending and not hasattr(journal.month, "_sync_baseline")
    assert window.get_day_text() == ""
    assert window.categories_tree_view.get_day_content() == {}
    assert not window.day_text_field.day_text_view.get_editable()
    assert not window.categories_tree_view.tree_view.get_sensitive()
    assert not window.activities_panel.get_sensitive()
    assert not window.builder.get_object("add_new_entry_button").get_sensitive()
    for child_widget in window.builder.get_object("edit_toolbar").get_children():
        assert child_widget.get_sensitive() is (
            child_widget in (window.refresh_button, window.sync_details_button)
        )
    sync.write_revision(pending_directory, missing_parent)

    assert journal.reconcile_external_changes()

    assert journal._last_save_success
    assert journal.day is pending_day
    assert window.day_text_field.day_text_buffer is pending_buffer
    assert not journal.month._sync_pending
    assert journal.month._sync_baseline == child["content"]
    assert window.get_day_text() == journal.day.text == "complete remote"
    assert window.categories_tree_view.get_day_content() == {"Ready": None}
    assert window.day_text_field.day_text_view.get_editable()
    assert window.categories_tree_view.tree_view.get_sensitive()
    assert window.activities_panel.get_sensitive()
    assert window.builder.get_object("add_new_entry_button").get_sensitive()
    assert all(
        child_widget.get_sensitive()
        for child_widget in window.builder.get_object("edit_toolbar").get_children()
    )


def test_empty_pending_month_becomes_editable_without_resetting_buffer(
    real_journal_window, tmp_path
):
    journal = real_journal_window
    window = journal.frame
    directory = tmp_path / "empty-pending-journal"
    directory.mkdir()
    parent = sync.make_revision(KEY, [], {}, {1: {"text": "deleted"}})
    child = sync.make_revision(KEY, [parent["id"]], parent["content"], {})
    sync.write_revision(directory, child)

    assert journal.open_journal(str(directory), save_current=False, start_date=journal.actual_date)
    day = journal.day
    buffer = window.day_text_field.day_text_buffer
    changed = Mock()
    buffer.connect("changed", changed)
    assert journal.month._sync_pending
    assert not window.day_text_field.day_text_view.get_editable()
    assert not window.categories_tree_view.tree_view.get_sensitive()
    assert not window.activities_panel.get_sensitive()
    assert not window.builder.get_object("add_new_entry_button").get_sensitive()

    sync.write_revision(directory, parent)
    assert journal.reconcile_external_changes()

    assert journal._last_save_success
    assert not journal.month._sync_pending
    assert journal.month._sync_baseline == {}
    assert journal.day is day
    assert window.day_text_field.day_text_buffer is buffer
    assert window.get_day_text() == journal.day.text == ""
    changed.assert_not_called()
    assert window.day_text_field.day_text_view.get_editable()
    assert window.categories_tree_view.tree_view.get_sensitive()
    assert window.activities_panel.get_sensitive()
    assert window.builder.get_object("add_new_entry_button").get_sensitive()
    assert all(
        child_widget.get_sensitive()
        for child_widget in window.builder.get_object("edit_toolbar").get_children()
    )


def test_two_new_month_instances_keep_local_edits_when_remote_arrives_before_first_save(
    journal_module, tmp_path
):
    directory = tmp_path / "new-month"
    directory.mkdir()
    date = datetime.date(2026, 9, 1)
    peers = []
    for _ in range(2):
        observed = SimpleNamespace(months={}, dirs=SimpleNamespace(data_dir=str(directory)))
        observed.get_month = journal_module.Journal.get_month.__get__(observed)
        month = observed.get_month(date)
        assert month._sync_baseline == {} and month._sync_heads == ()
        assert month._sync_directory == os.path.normcase(os.path.abspath(directory))
        journal = make_journal(journal_module, directory, observed.months)
        assert journal.get_month(date) is month
        peers.append(journal)
    local, remote = peers
    local.frame.day_text_field.recent_buffers[date].set_text("first local month edit")
    local.frame.categories_tree_view.get_day_content.side_effect = lambda: {"Local": None}
    remote.frame.day_text_field.recent_buffers[date].set_text("first remote month edit")
    remote.frame.categories_tree_view.get_day_content.side_effect = lambda: {"Remote": None}

    assert remote.save_to_disk(silent=True)
    assert remote._last_save_success
    remote_head = remote.month._sync_heads[0]
    assert local.month._sync_baseline == {} and local.month._sync_heads == ()
    assert local.day.text == ""

    assert local.save_to_disk(silent=True)

    assert local._last_save_success
    local.frame.show_save_error_dialog.assert_not_called()
    graph = sync.read_graph(directory)
    assert len(graph.records) == 2 and graph.complete(KEY)
    local_record = next(record for key, record in graph.records.items() if key != remote_head)
    assert local_record["parents"] == [] and local_record["base"] == {}
    assert local_record["content"]["1"]["text"] == "first local month edit"
    assert remote_head not in local_record["parents"]
    assert set(graph.heads(KEY)) == {local_record["id"], remote_head}
    assert "first local month edit" in local.day.text
    assert "first remote month edit" in local.day.text
    assert "sync conflict" in local.day.text
    assert data.get_category_content(local.day.content) == {"Local": None, "Remote": None}
    assert local.frame.get_day_text() == local.day.text
    assert not local.month.edited and not local.month._sync_pending


@pytest.mark.parametrize("trigger", ["autosave", "notification"])
def test_activity_dialog_defers_direct_saves_until_stale_local_fields_are_captured(
    journal_module, tmp_path, monkeypatch, trigger
):
    directory = tmp_path / "journal"
    directory.mkdir()
    original = activities.Activity("editing", "Meeting", "Base notes", "10:00", "11:00")
    reminder = activities.Activity("reminder", "Reminder", "Reminder notes", "09:00", "10:00", 5)
    initial = sync.make_revision(
        KEY,
        [],
        {},
        {
            1: {
                "text": "saved",
                activities.ACTIVITIES_KEY: {
                    activities.encode_activity(original): None,
                    activities.encode_activity(reminder): None,
                },
            }
        },
    )
    sync.write_revision(directory, initial)
    journal = make_journal(journal_module, directory, storage.load_all_months_from_disk(directory))
    journal.check_alerts = journal_module.Journal.check_alerts.__get__(journal)
    journal.notifier = Mock()
    journal.notifier.notify.return_value = True
    now = datetime.datetime(2026, 9, 1, 8, 55, 30)
    monkeypatch.setattr(
        journal_module,
        "datetime",
        SimpleNamespace(datetime=SimpleNamespace(now=lambda: now)),
    )
    observed = journal.month._sync_heads
    baseline = copy.deepcopy(journal.month._sync_baseline)
    remote_activity = activities.Activity("editing", "Meeting", "Remote notes", "10:00", "11:00")
    remote_content = copy.deepcopy(baseline)
    remote_content["1"][activities.ACTIVITIES_KEY] = {
        activities.encode_activity(remote_activity): None,
        activities.encode_activity(reminder): None,
    }
    remote = sync.make_revision(KEY, observed, baseline, remote_content)
    buffer = journal.frame.day_text_field.recent_buffers[journal.date]
    buffer.set_text("uncaptured editor note")
    changed = Mock()
    buffer.connect("changed", changed)
    cache = directory / f"{KEY}.txt"
    cache_before = cache.read_bytes()
    journal.save_old_day = Mock(side_effect=journal.save_old_day)
    journal.save_to_disk = Mock(side_effect=journal.save_to_disk)
    parent = Gtk.Window()
    dialog = activity_ui.ActivityDialog(parent, original)

    def accept_stale_dialog(window):
        assert window is dialog.dialog
        assert window.get_visible() and window.get_modal()
        sync.write_revision(directory, remote)
        if trigger == "autosave":
            assert journal.save_to_disk() is True
            journal.notifier.notify.assert_not_called()
        else:
            assert journal.check_alerts(reset=True) is True
            journal.notifier.notify.assert_called_once_with(
                "Reminder",
                "09:00-10:00\nReminder notes",
            )
            notified = next(item for item in journal.day.activities if item.id == "reminder")
            assert notified.notified_for == "2026-09-01T08:55"
            assert journal._last_alert_check == now
            journal.save_to_disk.assert_called_once_with(silent=True)
        assert not journal._last_save_success and not journal._saving
        journal.save_old_day.assert_not_called()
        journal.config.save_to_disk.assert_not_called()
        journal.frame.reconcile_journal.assert_not_called()
        assert journal.month._sync_heads == observed
        assert journal.month._sync_baseline == baseline
        assert journal.month._sync_model_baseline == baseline
        assert journal.day.text == "saved"
        assert next(item for item in journal.day.activities if item.id == "editing") == original
        assert cache.read_bytes() == cache_before
        assert set(sync.read_graph(directory).records) == {initial["id"], remote["id"]}
        changed.assert_not_called()
        assert (
            dialog.description_buffer.get_text(*dialog.description_buffer.get_bounds(), True)
            == "Base notes"
        )
        dialog.title_entry.set_text("Local meeting")
        return Gtk.ResponseType.OK

    monkeypatch.setattr(Gtk.Dialog, "run", accept_stale_dialog)
    try:
        updated = dialog.run()
        assert updated == activities.Activity(
            "editing",
            "Local meeting",
            "Base notes",
            "10:00",
            "11:00",
        )
        assert dialog.dialog not in Gtk.Window.list_toplevels()
        journal.day.upsert_activity(updated)

        assert journal.save_to_disk(silent=True) is True

        assert journal._last_save_success
        journal.save_old_day.assert_called_once_with()
        graph = sync.read_graph(directory)
        local = [
            record
            for record in graph.records.values()
            if record["id"] not in (initial["id"], remote["id"])
        ]
        assert len(local) == 1
        assert local[0]["parents"] == list(observed)
        assert local[0]["base"] == baseline
        local_activities = {
            json.loads(value)["id"]: json.loads(value)
            for value in local[0]["content"]["1"][activities.ACTIVITIES_KEY]
        }
        assert local_activities["editing"] == json.loads(activities.encode_activity(updated))
        assert local_activities["reminder"]["notified_for"] == (
            "2026-09-01T08:55" if trigger == "notification" else None
        )
        assert remote["id"] not in local[0]["parents"]
        assert set(graph.heads(KEY)) == {local[0]["id"], remote["id"]}
        assert next(item for item in journal.day.activities if item.id == "editing") == (
            activities.Activity("editing", "Local meeting", "Remote notes", "10:00", "11:00")
        )
        assert journal.day.text == journal.frame.get_day_text() == "uncaptured editor note"
        journal.frame.reconcile_journal.assert_called_once_with()
        journal.frame.day_text_field.clear_buffers.assert_not_called()
        journal.frame.show_save_error_dialog.assert_not_called()
        changed.assert_not_called()
    finally:
        dialog.dialog.destroy()
        parent.destroy()


def test_deferred_exit_save_prevents_exit_and_retains_editor_for_retry(journal_module, tmp_path):
    directory = tmp_path / "journal"
    journal = make_journal(journal_module, directory, seed_journal(directory))
    journal.is_allowed_to_exit = True
    observed = journal.month._sync_heads
    content = copy.deepcopy(journal.day.content)
    buffer = journal.frame.day_text_field.recent_buffers[journal.date]
    buffer.set_text("local unsaved text")
    dialog = Gtk.Dialog(modal=True)
    dialog.show_all()
    try:
        assert journal.save_to_disk(exit_imminent=True) is True

        assert not journal._last_save_success and not journal.is_allowed_to_exit
        assert not journal._saving
        assert journal.day.content == content
        assert journal.month._sync_heads == observed
        assert set(sync.read_graph(directory).records) == set(observed)
        journal.frame.get_day_text.assert_not_called()
        journal.config.save_to_disk.assert_not_called()
        journal.frame.show_save_error_dialog.assert_not_called()
        journal.frame.reconcile_journal.assert_not_called()
        journal.show_message.assert_not_called()
    finally:
        dialog.destroy()
    assert journal.frame.get_day_text() == "local unsaved text"
    journal.is_allowed_to_exit = True

    assert journal.save_to_disk(exit_imminent=True) is True

    assert journal._last_save_success and journal.is_allowed_to_exit
    assert journal.day.text == "local unsaved text"
    assert journal.month._sync_heads != observed


def test_changing_journal_save_is_not_deferred_by_visible_modal_dialog(journal_module, tmp_path):
    directory = tmp_path / "journal"
    journal = make_journal(journal_module, directory, seed_journal(directory))
    observed = journal.month._sync_heads
    journal.frame.day_text_field.recent_buffers[journal.date].set_text("folder-change local edit")
    dialog = Gtk.Dialog(modal=True)
    dialog.show_all()
    try:
        assert journal.save_to_disk(changing_journal=True) is True

        assert journal._last_save_success
        assert journal.day.text == "folder-change local edit"
        assert journal.month._sync_heads != observed
        assert storage.load_all_months_from_disk(directory)[KEY].get_day(1).text == journal.day.text
    finally:
        dialog.destroy()


@pytest.mark.parametrize("pending_key", [KEY, "2026-10"])
def test_copy_refuses_remembered_missing_private_tip_even_when_disk_looks_complete(
    journal_module, tmp_path, monkeypatch, pending_key
):
    current, selected = tmp_path / "current", tmp_path / "selected"
    months = seed_journal(current, text="older source content")
    if pending_key != KEY:
        root = sync.make_revision(pending_key, [], {}, {1: {"text": "older inactive content"}})
        sync.write_revision(current, root)
        months = storage.load_all_months_from_disk(current)
    selected.mkdir()
    journal = make_journal(journal_module, current, months)
    private_month = journal.months[pending_key]
    private_day = private_month.get_day(1)
    cache = current / f"{pending_key}.txt"
    older_cache = cache.read_bytes()
    older_heads = private_month._sync_heads
    if pending_key == KEY:
        buffer = journal.frame.day_text_field.recent_buffers[private_day.date]
    else:
        buffer = GtkSource.Buffer.new()
        journal.frame.day_text_field.recent_buffers[private_day.date] = buffer
        private_day.text = "most recent private content"
        private_month.edited = True
    buffer.set_text("most recent private content")
    assert journal.save_to_disk(silent=True) is True
    assert journal._last_save_success
    private_heads = private_month._sync_heads
    assert private_heads != older_heads
    private_revision = next((current / sync.REVISION_DIR).glob(f"*{private_heads[0]}.json"))
    private_revision.unlink()
    cache.write_bytes(older_cache)

    assert not storage.pending_revisions(current)
    assert journal.save_to_disk(silent=True, background=True) is True
    assert journal._last_save_success
    assert private_month._sync_pending
    assert private_month._sync_heads == private_heads
    assert private_day.text == "most recent private content"
    assert not storage.pending_revisions(current)
    active_month, active_day = journal.month, journal.day
    buffers = journal.frame.day_text_field.recent_buffers.copy()
    content = {key: storage._get_dict(month) for key, month in journal.months.items()}
    changed = Mock()
    buffer.connect("changed", changed)
    journal.open_journal = Mock()
    copytree = Mock(side_effect=AssertionError("pending source must not be copied"))
    monkeypatch.setattr(journal_module.shutil, "copytree", copytree)

    assert journal.change_data_directory(str(selected), copy_current=True) is False

    assert journal.dirs.data_dir == str(current)
    assert journal.month is active_month and journal.day is active_day
    assert {key: storage._get_dict(month) for key, month in journal.months.items()} == content
    assert journal.frame.day_text_field.recent_buffers == buffers
    assert buffer.get_text(*buffer.get_bounds(), True) == "most recent private content"
    changed.assert_not_called()
    assert private_month._sync_heads == private_heads and private_month._sync_pending
    assert cache.read_bytes() == older_cache
    assert not storage.pending_revisions(current)
    assert list(selected.iterdir()) == []
    copytree.assert_not_called()
    journal.open_journal.assert_not_called()
    journal.frame.day_text_field.clear_buffers.assert_not_called()
    journal.config.__setitem__.assert_not_called()
    assert journal.show_message.call_args.kwargs == {"error": True}
    assert "Wait for complete revisions" in journal.show_message.call_args.args[0]


def test_copy_rechecks_destination_pending_state_before_opening(
    journal_module, tmp_path, monkeypatch
):
    current, selected = tmp_path / "current", tmp_path / "selected"
    journal = make_journal(journal_module, current, seed_journal(current))
    selected.mkdir()
    journal.open_journal = Mock()
    month, day = journal.month, journal.day
    content = copy.deepcopy(day.content)
    observed = month._sync_heads
    buffers = journal.frame.day_text_field.recent_buffers.copy()
    pending = Mock(side_effect=[False, True])
    monkeypatch.setattr(storage, "pending_revisions", pending)

    assert journal.change_data_directory(str(selected), copy_current=True) is False

    assert [call.args[0] for call in pending.call_args_list] == [str(current), str(selected)]
    assert any(selected.iterdir())
    assert journal.dirs.data_dir == str(current)
    assert journal.month is month and journal.day is day
    assert day.content == content and month._sync_heads == observed
    assert journal.frame.day_text_field.recent_buffers == buffers
    assert journal.frame.get_day_text() == content["text"]
    journal.open_journal.assert_not_called()
    journal.load_day.assert_not_called()
    journal.frame.day_text_field.clear_buffers.assert_not_called()
    journal.frame.search_box.clear.assert_not_called()
    journal.config.__setitem__.assert_not_called()
    assert journal.show_message.call_args.kwargs == {"error": True}
    assert "missing required revisions" in journal.show_message.call_args.args[0]


@pytest.mark.parametrize("callback", ["reconcile_external_changes", "save_to_disk"])
def test_inline_category_edit_defers_poll_and_autosave_until_local_snapshot_is_captured(
    journal_module, tmp_path, monkeypatch, callback
):
    directory = tmp_path / "journal"
    journal = make_journal(journal_module, directory, seed_journal(directory))
    month, day = journal.month, journal.day
    observed = month._sync_heads
    baseline = copy.deepcopy(month._sync_baseline)
    remote_content = copy.deepcopy(baseline)
    remote_content["1"]["text"] = "base\nREMOTE\n"
    remote_content["1"]["Work"]["remote entry"] = None
    remote = sync.make_revision(KEY, observed, baseline, remote_content)
    sync.write_revision(directory, remote)
    buffer = journal.frame.day_text_field.recent_buffers[day.date]
    buffer.set_text("LOCAL\nsecond\n")
    categories = {"Work": {"old": None, "local inline entry": None}}
    tree = journal.frame.categories_tree_view
    tree.get_day_content.side_effect = lambda: copy.deepcopy(categories)
    tree.cell.get_property.return_value = True
    cache = directory / f"{KEY}.txt"
    cache_before = cache.read_bytes()
    changed = Mock()
    buffer.connect("changed", changed)
    journal.save_old_day = Mock(side_effect=journal.save_old_day)
    journal.save_to_disk = Mock(side_effect=journal.save_to_disk)
    save = Mock(wraps=storage.save_months_to_disk)
    monkeypatch.setattr(storage, "save_months_to_disk", save)

    assert getattr(journal, callback)() is True

    tree.cell.get_property.assert_called_once_with("editing")
    journal.save_old_day.assert_not_called()
    save.assert_not_called()
    journal.config.save_to_disk.assert_not_called()
    tree.get_day_content.assert_not_called()
    tree.clear.assert_not_called()
    journal.frame.reconcile_journal.assert_not_called()
    journal.frame.show_save_error_dialog.assert_not_called()
    assert day.content == baseline["1"]
    assert month._sync_heads == observed
    assert month._sync_baseline == month._sync_model_baseline == baseline
    assert cache.read_bytes() == cache_before
    assert set(sync.read_graph(directory).records) == {*observed, remote["id"]}
    assert buffer.get_text(*buffer.get_bounds(), True) == "LOCAL\nsecond\n"
    assert not journal._saving
    changed.assert_not_called()
    if callback == "reconcile_external_changes":
        journal.save_to_disk.assert_not_called()
    else:
        journal.save_to_disk.assert_called_once_with()
        assert not journal._last_save_success
    tree.cell.get_property.return_value = False

    assert getattr(journal, callback)() is True

    assert journal._last_save_success and not journal._saving
    journal.save_old_day.assert_called_once_with()
    save.assert_called_once()
    graph = sync.read_graph(directory)
    local = [
        record for record in graph.records.values() if record["id"] not in {*observed, remote["id"]}
    ]
    assert len(local) == 1
    assert local[0]["parents"] == list(observed)
    assert local[0]["base"] == baseline
    assert local[0]["content"]["1"] == {"text": "LOCAL\nsecond\n", **categories}
    assert remote["id"] not in local[0]["parents"]
    assert set(graph.heads(KEY)) == {local[0]["id"], remote["id"]}
    assert journal.month is month and journal.day is day
    assert day.text == journal.frame.get_day_text() == "LOCAL\nREMOTE\n"
    assert day.content["Work"] == {
        "old": None,
        "local inline entry": None,
        "remote entry": None,
    }
    assert journal.frame.day_text_field.recent_buffers[day.date] is buffer
    journal.frame.reconcile_journal.assert_called_once_with()
    tree.clear.assert_called_once_with()
    journal.frame.day_text_field.clear_buffers.assert_not_called()
