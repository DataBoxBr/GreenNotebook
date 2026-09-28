# -----------------------------------------------------------------------
# Copyright (c) 2026 GreenNotebook contributors
#
# This file is part of GreenNotebook and is distributed under the terms
# of the GNU General Public License, version 3 or later.
# -----------------------------------------------------------------------

import logging
import sys


class DesktopNotifier:
    def __init__(self, application, icon_path=None):
        self.application = application
        self.icon_path = icon_path

    def notify(self, title, body):
        try:
            if sys.platform.startswith("win"):
                from winotify import Notification

                Notification(
                    app_id="GreenNotebook",
                    title=title,
                    msg=body,
                    icon=self.icon_path or "",
                ).show()
            else:
                from gi.repository import Gio

                notification = Gio.Notification.new(title)
                notification.set_body(body)
                self.application.send_notification(None, notification)
        except Exception:
            logging.exception("Could not display desktop notification")
            return False
        return True

    def close(self):
        pass
