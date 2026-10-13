# -----------------------------------------------------------------------
# Copyright (c) 2008-2024 Jendrik Seipp
#
# RedNotebook is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# RedNotebook is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <https://www.gnu.org/licenses/>.
# -----------------------------------------------------------------------

import copy
import datetime
import itertools
import logging
import os
import shutil
import sys
from collections import defaultdict


def _fix_webkit_compositing_on_kde():
    """
    Fix for black word cloud and preview panes on KDE environments.

    WebKit2 has compositing issues on KDE that cause HTML content to appear black.
    Setting WEBKIT_DISABLE_COMPOSITING_MODE=1 works around this issue.

    See: https://github.com/jendrikseipp/rednotebook/issues/828
    """
    # Check if running in KDE environment
    desktop_session = os.environ.get("DESKTOP_SESSION", "").lower()
    kde_session = os.environ.get("KDE_SESSION_VERSION", "")
    xdg_desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()

    is_kde = (
        "kde" in desktop_session
        or "plasma" in desktop_session
        or kde_session
        or "kde" in xdg_desktop
        or "plasma" in xdg_desktop
    )

    if is_kde:
        # Logging is not yet configured at this point, so we use a plain print.
        os.environ["WEBKIT_DISABLE_COMPOSITING_MODE"] = "1"
        print(
            f"WebKit compositing disabled for KDE environment "
            f"(DESKTOP_SESSION={desktop_session}, XDG_CURRENT_DESKTOP={xdg_desktop}, "
            f"KDE_SESSION_VERSION={kde_session})."
        )


# Apply WebKit compositing fix before any GUI initialization.
_fix_webkit_compositing_on_kde()

# Use basic stdout logging before we can initialize logging correctly.
logging.basicConfig(level=logging.DEBUG, format="%(levelname)-8s %(message)s", stream=sys.stdout)

try:
    import gi
except ImportError as err:
    logging.error(f'pygobject could not be imported: "{err}". Please install it (python3-gi).')
    sys.exit(1)

try:
    gi.require_version("Gtk", "3.0")
except ValueError as err:
    logging.error(err)
    sys.exit("Please install GTK (gir1.2-gtk-3.0).")

try:
    gi.require_version("GtkSource", "4")
    logging.info("Using GtkSourceView 4")
except ValueError:
    try:
        gi.require_version("GtkSource", "3.0")
        logging.info("Using GtkSourceView 3.0")
    except ValueError:
        sys.exit("Please install GtkSource (gir1.2-gtksource-3.0 or gir1.2-gtksource-4).")


if hasattr(sys, "frozen"):
    base_dir = sys._MEIPASS
else:
    app_dir = os.path.dirname(os.path.abspath(__file__))
    base_dir = os.path.dirname(app_dir)

print(f"Adding {base_dir} to sys.path")
sys.path.insert(0, base_dir)

# ---------------------- Enable i18n -------------------------------
from rednotebook.external import elibintl
from rednotebook.util import filesystem


LOCALE_PATH = filesystem.locale_dir
logging.info(f"Locale path: {LOCALE_PATH}")

GETTEXT_DOMAIN = "rednotebook"

"""
On Windows, translations currently only work with a hack. We need to
translate

  1) Strings in source code.
  2) GTK stock items.
  3) Strings in the Glade file.

1) works fine. For 3) we need a workaround in main_window.py. If we
initialize libintl with elibintl, the translations for 2) stop working.
Without the workaround we need to initialize libintl to avoid UTF-8
encoding errors.

When the problem is fixed upstream, we can pass libintl='libintl-8.dll'
to elibintl. See also

https://bugzilla.gnome.org/show_bug.cgi?id=574520
https://bugzilla.gnome.org/show_bug.cgi?id=753991
https://github.com/tobias47n9e/pygobject-locale
https://sourceforge.net/p/pygobjectwin32/tickets/27/

"""

elibintl.install(GETTEXT_DOMAIN, LOCALE_PATH, libintl=None)

# ------------------- end Enable i18n -------------------------------


from rednotebook import configuration, data, info
from rednotebook.help import example_content
from rednotebook.util import markup, utils


args = info.get_commandline_parser().parse_args()

# ---------------------- Enable logging -------------------------------


def setup_logging(log_file):
    file_logging_stream = open(log_file, "w")

    # We want to have all stdout and stderr messages in the logfile.
    # In the frozen version we cannot log to sys.stderr because it's
    # broken on windows. Stdout doesn't work either it seems.
    stderr_streams = [file_logging_stream]
    stdout_streams = [file_logging_stream]
    if not filesystem.main_is_frozen():
        stderr_streams.append(sys.__stderr__)
        stdout_streams.append(sys.__stdout__)
    sys.stderr = utils.StreamDuplicator(stderr_streams)
    sys.stdout = utils.StreamDuplicator(stdout_streams)

    root_logger = logging.getLogger("")
    root_logger.setLevel(logging.DEBUG)

    # Python adds a default handler if some log is generated before here
    # Remove all handlers that have been added automatically
    for handler in root_logger.handlers:
        root_logger.removeHandler(handler)

    # define a Handler which writes messages to sys.stdout
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    # set a format which is simpler for console use
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    # tell the handler to use this format
    console.setFormatter(formatter)
    # add the handler to the root logger
    root_logger.addHandler(console)

    logging.info(f'Writing log to file "{log_file}"')


default_config_file = os.path.join(filesystem.app_dir, "files", "default.cfg")
default_config = configuration.Config(default_config_file)

dirs = filesystem.Filenames(default_config)
setup_logging(dirs.log_file)

# ------------------ end Enable logging -------------------------------

logging.info(f"System encoding: {filesystem.ENCODING}")
logging.info(f"Language code: {filesystem.LANGUAGE}")

try:
    import enchant

    logging.info(f"Spell checking languages: {enchant.list_languages()}")
    logging.info(f"Spell checking dictionaries: {enchant.list_dicts()}")
except ImportError:
    pass

try:
    from gi.repository import Gio, GLib, Gtk
except (ImportError, AssertionError) as e:
    logging.error(e)
    logging.error("GTK not found. Please install it (gir1.2-gtk-3.0).")
    sys.exit(1)


from rednotebook import activities, backup, storage, sync, themes
from rednotebook.data import Month
from rednotebook.gui.main_window import MainWindow
from rednotebook.util import dates
from rednotebook.util.notifications import DesktopNotifier
from rednotebook.util.statistics import Statistics


class Journal(Gtk.Application):
    def __init__(self, *args, **kwargs):
        super().__init__(
            *args,
            application_id="app.greennotebook.GreenNotebook",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
            **kwargs,
        )
        # Let components check if the MainWindow has been created.
        self.frame = None
        self.theme_manager = None
        self.notifier = None
        self.alert_timer_id = None
        self._last_alert_check = None
        self._sync_timer_id = None
        self._saving = False
        self._options_dialog_open = False
        self._last_save_success = True
        self._sync_status = None

    def do_startup(self):
        Gtk.Application.do_startup(self)

        self.dirs = dirs

        user_config = configuration.Config(self.dirs.config_file)
        # Apply defaults where no custom values have been set
        for key, value in default_config.items():
            if key not in user_config:
                user_config[key] = value
        self.config = user_config
        self.config["theme"] = themes.normalize_theme(self.config.read("theme"))
        self.theme_manager = themes.ThemeManager()
        self.theme_manager.apply(self.config["theme"])
        self.config.save_state()

        logging.info(f"Running in portable mode: {self.dirs.portable}")

        self.month = None
        self.date = None
        self.months = {}

        # The dir name is the title
        self.title = ""

        # show instructions at first start
        self.is_first_start = self.config.read("firstStart")
        self.config["firstStart"] = 0
        logging.info(f"First Start: {bool(self.is_first_start)}")

        logging.info(f"GreenNotebook version: {info.version}")
        logging.info(filesystem.get_platform_info())

        self.actual_date = self.get_start_date()

    def do_activate(self):
        if self.frame:
            self.frame.main_frame.present()
            return

        self.frame = MainWindow(self)
        self.frame.main_frame.present()

        journal_path = self.get_journal_path()
        if not self.dirs.is_valid_journal_path(journal_path):
            logging.error(f"Invalid directory: {journal_path}. Using default journal.")
            self.show_message(
                _("You cannot use this directory for your journal:")
                + f" {journal_path}"
                + ". "
                + _("Opening default journal."),
                error=True,
            )
            journal_path = self.dirs.default_data_dir
        self.open_journal(journal_path)

        icon_path = os.path.join(self.dirs.frame_icon_dir, "rn-128.png")
        self.notifier = DesktopNotifier(self, icon_path)
        self.check_alerts(reset=True)
        self.alert_timer_id = GLib.timeout_add_seconds(30, self.check_alerts)

        self.archiver = backup.Archiver(self)
        GLib.idle_add(self.archiver.check_last_backup_date)

        # Check for a new version
        if self.config.read("checkForNewVersion") == 1:
            utils.check_new_version(self, info.version, startup=True)

        # Automatically save the content after a period of time
        GLib.timeout_add_seconds(600, self.save_to_disk)
        self._sync_timer_id = GLib.timeout_add_seconds(10, self.reconcile_external_changes)
        self.frame.main_frame.connect("focus-in-event", self.on_focus_in)

    def do_command_line(self, _command_line):
        # Arguments are parsed elsewhere, so we only show the window here.
        self.activate()
        return 0  # Must return a number.

    def set_theme(self, theme):
        theme = self.theme_manager.apply(theme)
        self.config["theme"] = theme
        self.config.save_to_disk()
        if self.frame:
            self.frame.on_theme_changed(theme)

    def get_journal_path(self):
        """
        Retrieve the path from optional args or return standard value if args
        not present
        """
        if not args.journal:
            data_dir = self.config.read("dataDir", self.dirs.data_dir)
            if not os.path.isabs(data_dir):
                data_dir = os.path.join(self.dirs.app_dir, data_dir)
                data_dir = os.path.normpath(data_dir)
            return data_dir

        # path_arg can be e.g. data (under .greennotebook), data (elsewhere),
        # or an absolute path /home/username/myjournal
        # Try to find the journal under the standard location or at the given
        # absolute or relative location
        path_arg = args.journal

        logging.debug(f'Trying to find journal "{path_arg}"')

        paths_to_check = [path_arg, os.path.join(self.dirs.journal_user_dir, path_arg)]

        for path in paths_to_check:
            if os.path.exists(path):
                if os.path.isdir(path):
                    return path
                else:
                    logging.warning("To open a journal you must specify a directory, not a file.")

        logging.error(
            f'The path "{path_arg}" is not a valid journal directory. '
            'Execute "greennotebook -h" for instructions'
        )
        sys.exit(2)

    def get_start_date(self):
        """
        Retrieve the date from optional args or otherwise return 'today'
        """
        if not args.start_date:
            return datetime.date.today()

        try:
            return dates.get_date_from_date_string(args.start_date)
        except ValueError:
            logging.error(f"Invalid date: {args.start_date} (required format: YYYY-MM-DD).")
            sys.exit(2)

    def exit(self):
        self.frame.add_values_to_config()

        # Make it possible to stop the program from exiting
        # e.g. if the journal could not be saved
        self.is_allowed_to_exit = True
        self.save_to_disk(exit_imminent=True)

        if self.is_allowed_to_exit:
            if self._sync_timer_id is not None:
                GLib.source_remove(self._sync_timer_id)
                self._sync_timer_id = None
            if self.alert_timer_id is not None:
                GLib.source_remove(self.alert_timer_id)
                self.alert_timer_id = None
            if self.notifier is not None:
                self.notifier.close()
            logging.info("Goodbye!")
            # Informs the logging system to perform an orderly shutdown by
            # flushing and closing all handlers.
            logging.shutdown()
            self.quit()

    def convert(self, text, target, headers=None, options=None, use_gtk_theme=False):
        options = options or {}
        options["font"] = self.config.read("previewFont")
        if use_gtk_theme:
            bgcolor, fgcolor = utils.get_gtk_colors(self.frame.day_text_field.day_text_view)
            options["bgcolor"] = bgcolor
            options["fgcolor"] = fgcolor
        return markup.convert(text, target, self.dirs.data_dir, headers=headers, options=options)

    def save_to_disk(
        self,
        exit_imminent=False,
        changing_journal=False,
        saveas=False,
        silent=False,
        background=False,
    ):
        if self._saving:
            return True
        if not changing_journal and (
            self.frame.categories_tree_view.cell.get_property("editing") is True
            or any(
                window.get_visible() and window.get_modal()
                for window in Gtk.Window.list_toplevels()
            )
        ):
            # Autosave and reminder callbacks also run inside modal GTK event loops.
            self._last_save_success = False
            if exit_imminent:
                self.is_allowed_to_exit = False
            return True
        self._saving = True
        self._last_save_success = False
        error = None
        something_saved = False
        before = {}
        try:
            self.save_old_day()
            before = {
                day.date: copy.deepcopy(day.content)
                for month in self.months.values()
                for day in month.days.values()
            }
            filesystem.make_directory(self.dirs.data_dir)
            something_saved = storage.save_months_to_disk(
                self.months, self.dirs.data_dir, exit_imminent, saveas
            )
            self.config.save_to_disk()
            self._last_save_success = True
        except OSError as err:
            error = err
            logging.error(f"Saving journal failed: {err}")
        finally:
            after = {
                day.date: day.content
                for month in self.months.values()
                for day in month.days.values()
            }
            if before and before != after:
                self.frame.reconcile_journal()
            else:
                self.frame.set_journal_editable()
            self._saving = False

        if error is not None:
            status = ("error", str(error))
            if not background or status != self._sync_status:
                self.show_message(
                    _("The journal could not be saved. Local edits are still in memory: %s")
                    % error,
                    error=True,
                )
            self._sync_status = status
            if exit_imminent:
                self.is_allowed_to_exit = False
            if not background:
                self.frame.show_save_error_dialog(exit_imminent)
        else:
            pending = any(getattr(month, "_sync_pending", False) for month in self.months.values())
            conflict = any(
                sync.CONFLICT_KEY in day.content
                or "<<<<<<< revision " in day.text
                or sync.CONFLICT_KEY in str(day.content.get(activities.ACTIVITIES_KEY, {}))
                for month in self.months.values()
                for day in month.days.values()
            )
            status = (pending, conflict)
            if not silent or something_saved or status != self._sync_status:
                if pending:
                    message = _("Waiting for complete revision files from the synced folder.")
                elif conflict:
                    message = _("Sync conflicts preserved. Review both versions before resolving.")
                else:
                    message = _("Saved locally. Google Drive upload is not verified.")
                self.show_message(message, error=False)
            self._sync_status = status
        # GLib callbacks require True even on failure; folder changes use the success flag.
        return True

    def reconcile_external_changes(self):
        if (
            self._saving
            or self._options_dialog_open
            or self.month is None
            or self.frame.template_manager.tmp_title is not None
            or self.frame.categories_tree_view.cell.get_property("editing") is True
            or any(
                window.get_visible() and window.get_modal()
                for window in Gtk.Window.list_toplevels()
            )
        ):
            return True
        self.save_to_disk(silent=True, background=True)
        return True

    def on_focus_in(self, _window, _event):
        GLib.idle_add(self._reconcile_on_focus)
        return False

    def _reconcile_on_focus(self):
        self.reconcile_external_changes()
        return False

    def check_alerts(self, reset=False):
        if reset:
            self._last_alert_check = None
        if self.notifier is None:
            return True

        now = datetime.datetime.now()
        days = [day for month in self.months.values() for day in month.days.values()]
        due = activities.get_due_activities(days, self._last_alert_check, now)
        notification_failed = False
        notification_sent = False
        for day, activity, token in due:
            title = activity.title
            body = f"{activity.start}-{activity.end}"
            if activity.description:
                body += f"\n{activity.description}"
            if self.notifier.notify(title, body):
                day.mark_activity_notified(activity.id, token)
                notification_sent = True
            else:
                notification_failed = True

        if notification_sent:
            self.save_to_disk(silent=True)
        if not notification_failed:
            self._last_alert_check = now
        return True

    def open_journal(self, data_dir, save_current=True, start_date=None):
        if not os.path.exists(data_dir):
            logging.warning(f"The dir {data_dir} does not exist. Select a different dir.")
            return

        if self.months and save_current:
            self.save_to_disk(changing_journal=True)
            if not self._last_save_success:
                return False

        try:
            new_months = storage.load_all_months_from_disk(data_dir)
        except (OSError, ValueError, TypeError, AssertionError, SystemExit) as error:
            self.show_message(_("Could not open the selected journal: %s") % error, error=True)
            return False

        logging.info(f"Opening journal at {data_dir!r}")
        self.dirs.data_dir = data_dir
        self._last_alert_check = None

        self.month = None
        self.months.clear()
        self.frame.search_box.clear()
        self.frame.day_text_field.clear_buffers()

        self.months = new_months

        # Nothing to save before first day change
        self.load_day(start_date or self.actual_date)

        if self.is_first_start and not os.listdir(data_dir) and not self.days:
            self.add_instruction_content()

        self.stats = Statistics(self)

        self.frame.cloud.update(force_update=True)

        self.frame.categories_tree_view.categories = self.categories
        self.frame.search_box.set_entries(self.get_escaped_tags())

        self.title = filesystem.get_journal_title(data_dir)

        # Set frame title
        self.set_frame_title()

        # Save the folder for next start
        if not self.dirs.portable:
            self.config["dataDir"] = data_dir
        else:
            rel_data_dir = filesystem.get_relative_path(self.dirs.app_dir, data_dir)
            self.config["dataDir"] = rel_data_dir
        self.config.save_to_disk()
        self._sync_status = None
        if storage.pending_revisions(data_dir):
            self.show_message(
                _("Waiting for complete revision files from the synced folder."), error=False
            )
        return True

    def change_data_directory(self, path, copy_current=False):
        if not path.strip():
            self.show_message(_("Please select a journal folder."), error=True)
            return False
        path = os.path.abspath(os.path.expanduser(path))
        if not self.dirs.is_valid_journal_path(path) or not os.access(path, os.R_OK | os.W_OK):
            self.show_message(
                _("Select an existing readable and writable journal folder."), error=True
            )
            return False
        current = os.path.abspath(self.dirs.data_dir)
        if os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(current)):
            return True
        if self.frame.template_manager.tmp_title is not None:
            self.show_message(
                _("Close the template editor before changing journal folders."), error=True
            )
            return False
        if copy_current:
            try:
                common = os.path.commonpath([os.path.realpath(current), os.path.realpath(path)])
            except ValueError:
                common = None
            if common and os.path.normcase(common) in {
                os.path.normcase(os.path.realpath(current)),
                os.path.normcase(os.path.realpath(path)),
            }:
                self.show_message(
                    _("The copy destination must be outside the current journal folder."),
                    error=True,
                )
                return False
            if os.listdir(path):
                self.show_message(_("Please select an empty directory."), error=True)
                return False
            self.save_to_disk(changing_journal=True)
            if not self._last_save_success:
                return False
            if any(
                getattr(month, "_sync_pending", False) for month in self.months.values()
            ) or storage.pending_revisions(current):
                self.show_message(
                    _("Wait for complete revisions before copying this journal."), error=True
                )
                return False
            try:
                shutil.copytree(current, path, dirs_exist_ok=True)
                if storage.pending_revisions(path):
                    raise OSError("The copied journal is still missing required revisions")
            except OSError as error:
                self.show_message(_("Could not copy the journal: %s") % error, error=True)
                return False
            return self.open_journal(path, save_current=False, start_date=self.date)
        return self.open_journal(path, start_date=self.date)

    def reload_from_disk(self):
        if self.frame.template_manager.tmp_title is not None:
            self.show_message(
                _("Close the template editor before reloading the journal."), error=True
            )
            return
        current_date = self.date
        if not self.open_journal(
            self.dirs.data_dir,
            save_current=False,
            start_date=current_date,
        ):
            return
        self.check_alerts(reset=True)
        self.show_message(_("Journal data reloaded from disk"), error=False)

    def set_frame_title(self):
        parts = ["GreenNotebook"]
        if self.title != "data":
            parts.append(self.title)
        parts.append(dates.format_date(self.config.read("exportDateFormat"), self.date))
        self.frame.main_frame.set_title(" - ".join(parts))

    def get_month(self, date):
        """
        Returns the corresponding month if it has previously been visited,
        otherwise a new month is created and returned
        """

        year_and_month = dates.get_year_and_month_from_date(date)

        # Selected month has not been loaded or created yet
        if year_and_month not in self.months:
            self.months[year_and_month] = Month(date.year, date.month)
            # A genuinely new month has an observed empty branch, unlike a pending cache.
            storage._set_baseline(self.months[year_and_month], {}, [], self.dirs.data_dir)

        return self.months[year_and_month]

    def get_day(self, date):
        return self.get_month(date).get_day(date.day)

    def get_escaped_tags(self):
        return [f"#{data.escape_tag(tag)}" for tag in self.categories]

    def save_old_day(self):
        """Order is important"""
        if self.month is None or self.frame.template_manager.tmp_title is not None:
            return
        old_content = self.day.content
        new_content = self.frame.categories_tree_view.get_day_content()
        categories = data.get_category_content(old_content)
        for key, value in old_content.items():
            if key not in categories and key != "text":
                new_content[key] = copy.deepcopy(value)
        new_content["text"] = self.frame.get_day_text()
        self.day.content = new_content

        content_changed = old_content != new_content
        if content_changed:
            self.month.edited = True
            self.month._sync_durable = False

        self.frame.calendar.set_day_edited(self.date.day, not self.day.empty)

    def load_day(self, new_date):
        old_date = self.date
        self.date = new_date

        if not dates.same_month(new_date, old_date) or self.month is None:
            self.month = self.get_month(self.date)

        self.frame.set_date(self.month, self.date, self.day)

        self.set_frame_title()

    @property
    def day(self):
        return self.month.get_day(self.date.day)

    def change_date(self, new_date):
        if new_date < datetime.date(1900, 1, 1):
            self.show_message("Only dates after 1900 are supported.", title="Too Early", error=True)
            return

        if new_date == self.date:
            return

        self.save_old_day()
        self.load_day(new_date)

    def go_to_next_day(self):
        next_date = self.date + dates.one_day
        following_edited_days = self.get_days_in_date_range(start_date=next_date)
        if following_edited_days:
            next_date = following_edited_days[0].date
        self.change_date(next_date)

    def go_to_prev_day(self):
        prev_date = self.date - dates.one_day
        previous_edited_days = self.get_days_in_date_range(end_date=prev_date)
        if previous_edited_days:
            prev_date = previous_edited_days[-1].date
        self.change_date(prev_date)

    def show_message(self, msg, title=None, error=False):
        if error and not title:
            title = _("Error")

        if error:
            msg_type = Gtk.MessageType.ERROR
            log_level = logging.ERROR
        else:
            msg_type = Gtk.MessageType.INFO
            log_level = logging.INFO

        self.frame.show_message(title, msg, msg_type)
        logging.log(log_level, f"{title}. {msg}" if title else msg)

    @property
    def categories(self):
        return sorted(
            set(itertools.chain.from_iterable(day.categories for day in self.days)),
            key=utils.safe_strxfrm,
        )

    def get_entries(self, category):
        entries = set()
        for day in self.days:
            entries |= set(day.get_entries(category))
        return sorted(entries)

    def search(self, text, tags):
        results = []
        for day in reversed(self.get_days_with_tags(tags)):
            results.append(day.search(text, tags))
        return results

    def replace_all(self, old, new):
        """Replace all strings matching 'old' with the 'new' string."""
        total_replacements = 0

        for day in self.days:
            replacements = day.replace_all(old, new)
            if replacements > 0:
                day.month.edited = True
                total_replacements += replacements

        # Update current frame.
        self.frame.set_day_text(self.day.text)

        # Update the preview if it's active.
        if self.frame.preview_mode and self.frame.html_editor.internal:
            self.frame.html_editor.show_day(self.day)

        if total_replacements > 0:
            # Prevent saving data from the buffers instead of the replaced text.
            self.frame.day_text_field.clear_buffers()

            self.frame.cloud.update(force_update=True)
            self.frame.search_box.update_search_results()

            self.show_message(
                _(f"Replaced {total_replacements} occurrences of the search text"),
                error=False,
            )
        else:
            self.show_message(_("No text has been replaced"), error=False)

    def get_days_with_tags(self, tags):
        if not tags:
            return self.days
        days = []
        for day in self.days:
            day_tags = {data.escape_tag(tag) for tag in day.categories}
            if all(tag in day_tags for tag in tags):
                days.append(day)
        return days

    def get_word_count_dict(self):
        """
        Return a dictionary mapping the words to their number of appearance.
        """
        word_dict = defaultdict(int)
        for day in self.days:
            words = day.get_words()
            for word in words:
                word_dict[word.lower()] += 1
        return word_dict

    @property
    def days(self):
        """
        Returns all edited days ordered by their date
        """
        # The day being edited counts too
        if self.frame:
            self.save_old_day()

        days = []
        for month in self.months.values():
            # Filter out days without content.
            days_in_month = [day for day in month.days.values() if not day.empty]
            days.extend(days_in_month)

        # Sort days
        days = sorted(days, key=lambda day: day.date)
        return days

    def get_days_in_date_range(self, start_date=None, end_date=None):
        if not start_date:
            start_date = datetime.date.min
        if not end_date:
            end_date = datetime.date.max

        start_date, end_date = sorted([start_date, end_date])
        assert start_date <= end_date

        days_in_date_range = []
        for day in self.days:
            if day.date < start_date:
                continue
            elif start_date <= day.date <= end_date:
                days_in_date_range.append(day)
            elif day.date > end_date:
                break
        return days_in_date_range

    def add_instruction_content(self):
        self.change_date(datetime.date.today())
        current_date = self.date

        logging.info(f"Adding example content on {current_date}")

        for example_day in example_content:
            self.day.content = example_day
            self.frame.set_date(self.month, self.date, self.day)
            self.go_to_next_day()

        self.change_date(current_date)


def main():
    journal = Journal()
    utils.setup_signal_handlers(journal)
    journal.run(sys.argv)

    try:
        logging.info(f"Peak memory: {filesystem.get_peak_memory_in_kb()} KiB")
    except Warning:
        pass


if __name__ == "__main__":
    main()
