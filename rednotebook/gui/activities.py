# -----------------------------------------------------------------------
# Copyright (c) 2026 GreenNotebook contributors
#
# This file is part of GreenNotebook and is distributed under the terms
# of the GNU General Public License, version 3 or later.
# -----------------------------------------------------------------------

import datetime

from gi.repository import Gdk, GLib, Gtk, Pango

from rednotebook import activities as activity_data
from rednotebook.util import dates


class TimeInput(Gtk.Box):
    def __init__(self, value):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        hour, minute = (int(part) for part in value.split(":"))
        self.hour = self._new_spin_button(0, 23, hour)
        self.minute = self._new_spin_button(0, 59, minute)
        self.pack_start(self.hour, False, False, 0)
        self.pack_start(Gtk.Label(label=":"), False, False, 0)
        self.pack_start(self.minute, False, False, 0)

    @staticmethod
    def _new_spin_button(lower, upper, value):
        adjustment = Gtk.Adjustment(value, lower, upper, 1, 5, 0)
        spin = Gtk.SpinButton(adjustment=adjustment, climb_rate=1, digits=0)
        spin.set_numeric(True)
        spin.set_wrap(True)
        spin.set_width_chars(2)
        return spin

    def get_value(self):
        return f"{self.hour.get_value_as_int():02d}:{self.minute.get_value_as_int():02d}"


class ActivityDialog:
    def __init__(self, parent, activity=None):
        title = _("Edit activity") if activity else _("Add activity")
        self.dialog = Gtk.Dialog(title=title, transient_for=parent, modal=True)
        self.dialog.add_buttons(
            _("Cancel"), Gtk.ResponseType.CANCEL, _("Save"), Gtk.ResponseType.OK
        )
        self.dialog.set_default_response(Gtk.ResponseType.OK)
        self.dialog.set_default_size(480, 360)

        now = datetime.datetime.now().replace(second=0, microsecond=0)
        end_of_day = now.replace(hour=23, minute=59)
        default_end = min(now + datetime.timedelta(hours=1), end_of_day)
        default_start = now if now < default_end else now - datetime.timedelta(hours=1)

        self.title_entry = Gtk.Entry()
        self.title_entry.set_activates_default(True)
        self.description_view = Gtk.TextView()
        self.description_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.description_buffer = self.description_view.get_buffer()
        self.description_buffer.connect("changed", self._on_description_changed)
        self.description_count = Gtk.Label(xalign=1)
        self.description_count.get_style_context().add_class("dim-label")
        self._truncating_description = False
        self.start_input = TimeInput(
            activity.start if activity else default_start.strftime("%H:%M")
        )
        self.end_input = TimeInput(activity.end if activity else default_end.strftime("%H:%M"))
        self.alert_combo = Gtk.ComboBoxText()
        self.alert_combo.append("none", _("No alert"))
        for minutes in activity_data.ALERT_MINUTES_OPTIONS:
            self.alert_combo.append(str(minutes), _("%d minutes before") % minutes)
        alert_minutes = activity.alert_minutes_before if activity else 0
        self.alert_combo.set_active_id("none" if alert_minutes is None else str(alert_minutes))

        if activity:
            self.title_entry.set_text(activity.title)
            self.description_buffer.set_text(activity.description)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8, margin=12)
        grid.attach(Gtk.Label(label=_("Title"), xalign=0), 0, 0, 1, 1)
        grid.attach(self.title_entry, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label=_("Description"), xalign=0, yalign=0), 0, 1, 1, 1)
        description_scroller = Gtk.ScrolledWindow()
        description_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        description_scroller.set_min_content_height(110)
        description_scroller.add(self.description_view)
        grid.attach(description_scroller, 1, 1, 1, 1)
        grid.attach(self.description_count, 1, 2, 1, 1)
        grid.attach(Gtk.Label(label=_("Starts at"), xalign=0), 0, 3, 1, 1)
        grid.attach(self.start_input, 1, 3, 1, 1)
        grid.attach(Gtk.Label(label=_("Ends at"), xalign=0), 0, 4, 1, 1)
        grid.attach(self.end_input, 1, 4, 1, 1)
        grid.attach(Gtk.Label(label=_("Desktop alert"), xalign=0), 0, 5, 1, 1)
        grid.attach(self.alert_combo, 1, 5, 1, 1)

        self.error_label = Gtk.Label(xalign=0)
        self.error_label.set_line_wrap(True)
        self.error_label.get_style_context().add_class("error")
        grid.attach(self.error_label, 0, 6, 2, 1)
        self.dialog.get_content_area().add(grid)
        self.dialog.show_all()
        self._on_description_changed()
        self.title_entry.grab_focus()
        self.activity = activity

    def _on_description_changed(self, _buffer=None):
        if self._truncating_description:
            return
        start, end = self.description_buffer.get_bounds()
        description = self.description_buffer.get_text(start, end, True)
        if len(description) > activity_data.MAX_DESCRIPTION_LENGTH:
            self._truncating_description = True
            overflow_start = self.description_buffer.get_iter_at_offset(
                activity_data.MAX_DESCRIPTION_LENGTH
            )
            self.description_buffer.delete(overflow_start, end)
            self._truncating_description = False
            description = description[: activity_data.MAX_DESCRIPTION_LENGTH]
        self.description_count.set_text(
            _("%d / %d characters") % (len(description), activity_data.MAX_DESCRIPTION_LENGTH)
        )

    def run(self):
        while self.dialog.run() == Gtk.ResponseType.OK:
            alert_id = self.alert_combo.get_active_id()
            alert_minutes_before = None if alert_id == "none" else int(alert_id)
            notified_for = None
            if (
                self.activity
                and self.activity.alert_minutes_before == alert_minutes_before
                and self.activity.start == self.start_input.get_value()
            ):
                notified_for = self.activity.notified_for
            description_start, description_end = self.description_buffer.get_bounds()
            description = self.description_buffer.get_text(description_start, description_end, True)
            try:
                if self.activity:
                    result = activity_data.Activity(
                        id=self.activity.id,
                        title=self.title_entry.get_text(),
                        description=description,
                        start=self.start_input.get_value(),
                        end=self.end_input.get_value(),
                        alert_minutes_before=alert_minutes_before,
                        notified_for=notified_for,
                    )
                else:
                    result = activity_data.create_activity(
                        title=self.title_entry.get_text(),
                        description=description,
                        start=self.start_input.get_value(),
                        end=self.end_input.get_value(),
                        alert_minutes_before=alert_minutes_before,
                    )
            except ValueError as error:
                messages = {
                    "Activity title cannot be empty": _("Activity title cannot be empty"),
                    "Activity end time must be later than its start time": _(
                        "Activity end time must be later than its start time"
                    ),
                }
                self.error_label.set_text(messages.get(str(error), str(error)))
                continue
            self.dialog.destroy()
            return result

        self.dialog.destroy()
        return None


class ActivitiesPanel(Gtk.Box):
    def __init__(self, main_window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.main_window = main_window
        self.journal = main_window.journal
        self.day = None
        self.set_size_request(280, 220)

        self.date_label = Gtk.Label(xalign=0)
        self.date_label.set_margin_start(8)
        self.date_label.set_margin_end(8)
        self.date_label.set_margin_top(8)
        self.pack_start(self.date_label, False, False, 0)

        self.model = Gtk.ListStore(str, str, str, str, str, str)
        self.tree_view = Gtk.TreeView(model=self.model)
        self.tree_view.get_style_context().add_class("activities-list")
        self.tree_view.set_headers_visible(True)
        self.tree_view.set_tooltip_column(5)
        self.tree_view.connect("row-activated", self._on_edit)
        self.tree_view.connect("key-press-event", self._on_key_press)
        self.tree_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self._add_column(_("Activity"), 1, expand=True, markup=True)
        self._add_column(_("From"), 2)
        self._add_column(_("To"), 3)
        self._add_column(_("Alert"), 4)

        selection = self.tree_view.get_selection()
        selection.connect("changed", self._on_selection_changed)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.tree_view)
        self.pack_start(scroller, True, True, 0)

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_margin_start(6)
        button_box.set_margin_end(6)
        button_box.set_margin_bottom(6)
        self.add_button = self._new_button("list-add", _("Add activity"), self._on_add)
        self.edit_button = self._new_button("document-edit", _("Edit activity"), self._on_edit)
        self.copy_button = self._new_button(
            "edit-copy", _("Copy activities (Ctrl+C)"), self._on_copy
        )
        self.paste_button = self._new_button(
            "edit-paste", _("Paste activities (Ctrl+V)"), self._on_paste
        )
        self.remove_button = self._new_button("edit-delete", _("Delete activity"), self._on_remove)
        button_box.pack_start(self.add_button, True, True, 0)
        button_box.pack_start(self.edit_button, True, True, 0)
        button_box.pack_start(self.copy_button, True, True, 0)
        button_box.pack_start(self.paste_button, True, True, 0)
        button_box.pack_start(self.remove_button, True, True, 0)
        self.pack_start(button_box, False, False, 0)
        self._on_selection_changed()

    def _add_column(self, title, model_index, expand=False, markup=False):
        renderer = Gtk.CellRendererText()
        if expand:
            renderer.set_property("wrap-mode", Pango.WrapMode.WORD_CHAR)
            renderer.set_property("wrap-width", 220)
        attribute = "markup" if markup else "text"
        column = Gtk.TreeViewColumn(title, renderer, **{attribute: model_index})
        column.set_expand(expand)
        column.set_resizable(True)
        self.tree_view.append_column(column)

    @staticmethod
    def _new_button(icon_name, tooltip, callback):
        button = Gtk.Button.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        button.set_tooltip_text(tooltip)
        button.connect("clicked", callback)
        return button

    def show_day(self, day):
        self.day = day
        date_text = dates.format_date(self.journal.config.read("exportDateFormat"), day.date)
        self.date_label.set_markup(
            "<b>" + GLib.markup_escape_text(_("Activities for %s") % date_text) + "</b>"
        )
        self.model.clear()
        for activity in day.activities:
            description = activity.description.replace("\n", " ")
            if len(description) > 180:
                description = description[:177].rstrip() + "..."
            markup = f"<b>{GLib.markup_escape_text(activity.title)}</b>"
            if description:
                markup += f"\n<small>{GLib.markup_escape_text(description)}</small>"
            self.model.append(
                [
                    activity.id,
                    markup,
                    activity.start,
                    activity.end,
                    self._format_alert(activity.alert_minutes_before),
                    "\n".join(filter(None, (activity.title, activity.description))),
                ]
            )
        self._on_selection_changed()

    @staticmethod
    def _format_alert(minutes):
        return "-" if minutes is None else _("%d min before") % minutes

    def _selected_activities(self):
        if self.day is None:
            return []
        model, paths = self.tree_view.get_selection().get_selected_rows()
        activity_ids = {model[path][0] for path in paths}
        return [activity for activity in self.day.activities if activity.id in activity_ids]

    def _selected_activity(self):
        selected = self._selected_activities()
        return selected[0] if len(selected) == 1 else None

    def _on_selection_changed(self, _selection=None):
        selected = self._selected_activities()
        self.edit_button.set_sensitive(len(selected) == 1)
        self.remove_button.set_sensitive(bool(selected))
        self.copy_button.set_sensitive(bool(self.day and self.day.activities))

    def _on_add(self, _widget):
        activity = ActivityDialog(self.main_window.main_frame).run()
        if activity:
            self._save_activity(activity)

    def _on_edit(self, _widget, _path=None, _column=None):
        activity = self._selected_activity()
        if activity:
            updated = ActivityDialog(self.main_window.main_frame, activity).run()
            if updated:
                self._save_activity(updated)

    def _save_activity(self, activity):
        self.day.upsert_activity(activity)
        self._save_changes(check_alerts=True)

    def _save_changes(self, check_alerts=False):
        self.journal.save_to_disk(silent=True)
        self.show_day(self.day)
        self.main_window.calendar.set_day_edited(self.day.date.day, not self.day.empty)
        if check_alerts:
            self.journal.check_alerts(reset=True)

    def _on_copy(self, _widget=None):
        selected = self._selected_activities()
        copied = selected or (self.day.activities if self.day else [])
        if not copied:
            return
        payload = activity_data.serialize_activities_for_clipboard(copied)
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(payload, -1)
        self.journal.show_message(_("Copied %d activities") % len(copied), error=False)

    def _on_paste(self, _widget=None):
        payload = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text()
        pasted = activity_data.deserialize_activities_from_clipboard(payload)
        if not pasted:
            self.journal.show_message(
                _("The clipboard does not contain GreenNotebook activities"), error=True
            )
            return
        for activity in pasted:
            self.day.upsert_activity(activity)
        self._save_changes(check_alerts=True)
        self.journal.show_message(_("Pasted %d activities") % len(pasted), error=False)

    def _on_key_press(self, _widget, event):
        if not event.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        key_name = (Gdk.keyval_name(event.keyval) or "").lower()
        if key_name == "c":
            self._on_copy()
            return True
        if key_name == "v":
            self._on_paste()
            return True
        return False

    def _on_remove(self, _widget):
        selected = self._selected_activities()
        if not selected:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self.main_window.main_frame,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.CANCEL,
            text=_("Delete selected activities?"),
        )
        dialog.format_secondary_text("\n".join(activity.title for activity in selected[:5]))
        dialog.add_button(_("Delete"), Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            for activity in selected:
                self.day.remove_activity(activity.id)
            self._save_changes()
