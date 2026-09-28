# -----------------------------------------------------------------------
# Copyright (c) 2026 GreenNotebook contributors
#
# This file is part of GreenNotebook and is distributed under the terms
# of the GNU General Public License, version 3 or later.
# -----------------------------------------------------------------------

import logging
import os

import gi


gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from rednotebook.util import filesystem


THEME_SYSTEM = "system"
THEME_LIGHT = "light"
THEME_DARK = "dark"
THEMES = {THEME_SYSTEM, THEME_LIGHT, THEME_DARK}


def normalize_theme(theme):
    return theme if theme in THEMES else THEME_SYSTEM


class ThemeManager:
    def __init__(self):
        self.provider = None

    def apply(self, theme):
        theme = normalize_theme(theme)
        screen = Gdk.Screen.get_default()
        settings = Gtk.Settings.get_default()

        if self.provider is not None and screen is not None:
            Gtk.StyleContext.remove_provider_for_screen(screen, self.provider)
            self.provider = None

        if settings is not None:
            if theme == THEME_SYSTEM:
                settings.reset_property("gtk-application-prefer-dark-theme")
            else:
                settings.set_property("gtk-application-prefer-dark-theme", theme == THEME_DARK)

        if theme != THEME_SYSTEM and screen is not None:
            css_path = os.path.join(filesystem.files_dir, f"theme-{theme}.css")
            provider = Gtk.CssProvider()
            try:
                provider.load_from_path(css_path)
            except GLib.Error as error:
                logging.error(f"Could not load theme stylesheet {css_path}: {error}")
            else:
                Gtk.StyleContext.add_provider_for_screen(
                    screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
                self.provider = provider

        return theme
