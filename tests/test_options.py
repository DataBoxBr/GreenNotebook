import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rednotebook.gui import options


def make_manager(tmp_path, path=None, success=True):
    config = Mock()
    config.values = {"dataDir": "portable/data", "autoIndent": 1}
    config.__setitem__ = Mock(side_effect=config.values.__setitem__)
    config.read.side_effect = lambda name, default=None: config.values.get(name, default)
    journal = SimpleNamespace(
        config=config,
        dirs=SimpleNamespace(data_dir=str(tmp_path / "active")),
        change_data_directory=Mock(return_value=success),
        show_message=Mock(),
    )
    manager = options.OptionsManager.__new__(options.OptionsManager)
    manager.journal = journal
    manager.config = config
    manager.main_window = SimpleNamespace(
        cloud=Mock(), set_auto_indent=Mock(), set_font=Mock(), tray_icon=None
    )
    manager.dialog = Mock(dialog=object())
    manager.journal_directory_option = SimpleNamespace(
        get_value=lambda: journal.dirs.data_dir if path is None else path
    )
    manager.options = [SimpleNamespace(option_name="autoIndent", get_value=lambda: 0)]
    return manager


def mock_action_dialog(monkeypatch, response):
    dialog = Mock()
    dialog.run.return_value = response
    factory = Mock(return_value=dialog)
    monkeypatch.setattr(options.Gtk, "MessageDialog", factory)
    return dialog, factory


@pytest.mark.parametrize("path", ["", " ", "\t\n"])
def test_empty_directory_is_rejected(tmp_path, monkeypatch, path):
    manager = make_manager(tmp_path, path)
    dialog, factory = mock_action_dialog(monkeypatch, options.Gtk.ResponseType.ACCEPT)

    assert not manager.apply_journal_directory()

    manager.journal.show_message.assert_called_once_with(
        "Please select a journal folder.", error=True
    )
    factory.assert_not_called()
    manager.journal.change_data_directory.assert_not_called()
    manager.config.save_to_disk.assert_not_called()
    assert manager.config.values["dataDir"] == "portable/data"


@pytest.mark.parametrize("suffix", ["", os.sep, os.sep + "."])
def test_unchanged_active_directory_needs_no_action(tmp_path, monkeypatch, suffix):
    manager = make_manager(tmp_path, str(tmp_path / "active") + suffix)
    dialog, factory = mock_action_dialog(monkeypatch, options.Gtk.ResponseType.ACCEPT)

    assert manager.apply_journal_directory()

    factory.assert_not_called()
    manager.journal.change_data_directory.assert_not_called()
    assert manager.config.values["dataDir"] == "portable/data"


@pytest.mark.parametrize(
    "response", [options.Gtk.ResponseType.CANCEL, options.Gtk.ResponseType.DELETE_EVENT]
)
def test_cancel_directory_action_changes_nothing(tmp_path, monkeypatch, response):
    manager = make_manager(tmp_path, str(tmp_path / "selected"))
    dialog, factory = mock_action_dialog(monkeypatch, response)

    assert not manager.apply_journal_directory()

    manager.journal.change_data_directory.assert_not_called()
    manager.config.save_to_disk.assert_not_called()
    assert manager.journal.dirs.data_dir == str(tmp_path / "active")
    assert manager.config.values == {"dataDir": "portable/data", "autoIndent": 1}
    dialog.destroy.assert_called_once_with()
    assert factory.call_args.kwargs["transient_for"] is manager.dialog.dialog
    dialog.set_default_response.assert_called_once_with(options.Gtk.ResponseType.CANCEL)


@pytest.mark.parametrize(
    "response,copy_current",
    [(options.Gtk.ResponseType.ACCEPT, False), (options.Gtk.ResponseType.APPLY, True)],
)
@pytest.mark.parametrize("success", [False, True])
def test_selected_directory_action_uses_journal_validation(
    tmp_path, monkeypatch, response, copy_current, success
):
    path = str(tmp_path / "selected")
    manager = make_manager(tmp_path, path, success)
    dialog, factory = mock_action_dialog(monkeypatch, response)

    assert manager.apply_journal_directory() is success

    manager.journal.change_data_directory.assert_called_once_with(path, copy_current=copy_current)
    dialog.destroy.assert_called_once_with()
    manager.config.save_to_disk.assert_not_called()
    assert manager.config.values["dataDir"] == "portable/data"


@pytest.mark.parametrize("path", ["~/selected/../journal", "relative/../journal"])
def test_typed_directory_is_expanded_and_absolute(tmp_path, monkeypatch, path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    manager = make_manager(tmp_path, "  " + path + "  ")
    mock_action_dialog(monkeypatch, options.Gtk.ResponseType.ACCEPT)

    assert manager.apply_journal_directory()

    manager.journal.change_data_directory.assert_called_once_with(
        str(tmp_path / "journal"), copy_current=False
    )


def test_saving_options_preserves_journal_managed_portable_path(tmp_path):
    manager = make_manager(tmp_path)

    manager.save_options()

    assert manager.config.values == {"dataDir": "portable/data", "autoIndent": 0}
    manager.config.save_to_disk.assert_called_once_with()


def test_directory_entry_shows_active_path_not_config(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    option = SimpleNamespace(pack_start=Mock(), on_choose_folder=Mock())
    entry = Mock()
    monkeypatch.setattr(options.Option, "__init__", Mock(return_value=None))
    monkeypatch.setattr(options.Gtk, "Entry", Mock(return_value=entry))
    monkeypatch.setattr(options, "ActionButton", Mock())

    options.JournalDirectoryOption.__init__(option, manager)

    entry.set_text.assert_called_once_with(manager.journal.dirs.data_dir)
    assert manager.config.values["dataDir"] == "portable/data"


@pytest.mark.parametrize(
    "response,filename,expected",
    [
        (options.Gtk.ResponseType.CANCEL, "selected", "active"),
        (options.Gtk.ResponseType.OK, None, "active"),
        (options.Gtk.ResponseType.OK, "selected", "selected"),
    ],
)
def test_folder_chooser_only_updates_entry(tmp_path, monkeypatch, response, filename, expected):
    manager = make_manager(tmp_path)
    option = SimpleNamespace(manager=manager, entry=Mock())
    chooser = Mock()
    chooser.run.return_value = response
    chooser.get_filename.return_value = str(tmp_path / filename) if filename else None
    factory = Mock(return_value=chooser)
    monkeypatch.setattr(options.Gtk, "FileChooserDialog", factory)

    options.JournalDirectoryOption.on_choose_folder(option, None)

    if expected == "active":
        option.entry.set_text.assert_not_called()
    else:
        option.entry.set_text.assert_called_once_with(str(tmp_path / expected))
    assert factory.call_args.kwargs["transient_for"] is manager.dialog.dialog
    chooser.destroy.assert_called_once_with()
    manager.journal.change_data_directory.assert_not_called()
    manager.config.save_to_disk.assert_not_called()


def prepare_preferences(monkeypatch, manager):
    directory_option = manager.journal_directory_option
    monkeypatch.setattr(options, "JournalDirectoryOption", lambda manager: directory_option)
    monkeypatch.setattr(options.Gtk, "Label", Mock())
    monkeypatch.setattr(options.platform, "system", lambda: "Windows")
    monkeypatch.setattr(options.filesystem, "has_system_tray", lambda: False)
    for name in ("TickOption", "FontOption", "TextOption", "DateFormatOption", "IntegerOption"):
        monkeypatch.setattr(
            options,
            name,
            lambda text, option_name, **kwargs: SimpleNamespace(
                option_name=option_name, get_value=lambda: 0, pack_start=Mock()
            ),
        )
    monkeypatch.setattr(options, "ActionButton", Mock())


def test_preferences_cancel_does_not_apply_options(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, str(tmp_path / "selected"))
    prepare_preferences(monkeypatch, manager)
    manager.dialog.run.return_value = options.Gtk.ResponseType.CANCEL

    manager.on_options_dialog()

    manager.journal.change_data_directory.assert_not_called()
    manager.config.__setitem__.assert_not_called()
    manager.config.save_to_disk.assert_not_called()
    manager.main_window.cloud.update.assert_not_called()
    manager.main_window.set_font.assert_called_once_with(options.editor.DEFAULT_FONT)
    assert not manager.journal._options_dialog_open
    manager.dialog.hide.assert_called_once_with()


@pytest.mark.parametrize(
    "first_action", [options.Gtk.ResponseType.CANCEL, options.Gtk.ResponseType.APPLY]
)
def test_preferences_retries_before_applying_and_persisting(tmp_path, monkeypatch, first_action):
    manager = make_manager(tmp_path, str(tmp_path / "selected"))
    prepare_preferences(monkeypatch, manager)
    action_dialog, factory = mock_action_dialog(monkeypatch, first_action)
    action_dialog.run.side_effect = [first_action, options.Gtk.ResponseType.ACCEPT]
    attempts = []

    def run_preferences():
        assert manager.journal._options_dialog_open
        manager.config.__setitem__.assert_not_called()
        manager.config.save_to_disk.assert_not_called()
        manager.main_window.cloud.update.assert_not_called()
        assert not manager.dialog.hide.called
        attempts.append(None)
        return options.Gtk.ResponseType.OK

    def change_directory(path, copy_current=False):
        assert manager.journal._options_dialog_open
        if copy_current:
            return False
        manager.journal.dirs.data_dir = path
        manager.config.values["dataDir"] = "portable/selected"
        return True

    manager.dialog.run.side_effect = run_preferences
    manager.journal.change_data_directory.side_effect = change_directory

    manager.on_options_dialog()

    assert len(attempts) == 2
    assert manager.config.values["dataDir"] == "portable/selected"
    assert manager.journal_directory_option not in manager.options
    manager.config.save_to_disk.assert_called_once_with()
    manager.main_window.cloud.update.assert_called_once_with(force_update=True)
    manager.main_window.set_auto_indent.assert_called_once_with()
    manager.main_window.set_font.assert_not_called()
    assert not manager.journal._options_dialog_open
    manager.dialog.hide.assert_called_once_with()


def test_preferences_resets_polling_flag_on_exception(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    prepare_preferences(monkeypatch, manager)
    manager.dialog.run.side_effect = RuntimeError("dialog failed")

    with pytest.raises(RuntimeError, match="dialog failed"):
        manager.on_options_dialog()

    assert not manager.journal._options_dialog_open
    manager.dialog.hide.assert_called_once_with()
    manager.config.save_to_disk.assert_not_called()
