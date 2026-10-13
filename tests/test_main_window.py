from types import SimpleNamespace

import pytest

from rednotebook.activities import ACTIVITIES_KEY
from rednotebook.gui.main_window import MainWindow


def make_window(*, edited=False, editor_text="Saved text", categories=None):
    categories = {"Work": None} if categories is None else categories
    day = SimpleNamespace(
        text="Saved text",
        content={"text": "Saved text", "Work": None, ACTIVITIES_KEY: {}},
    )
    return SimpleNamespace(
        journal=SimpleNamespace(months={"2026-09": SimpleNamespace(edited=edited)}),
        day=day,
        get_day_text=lambda: editor_text,
        categories_tree_view=SimpleNamespace(get_day_content=lambda: categories),
    )


def test_refresh_has_no_warning_when_visible_content_is_saved():
    window = make_window()

    assert not MainWindow._has_unsaved_changes(window)


@pytest.mark.parametrize(
    "window",
    [
        make_window(edited=True),
        make_window(editor_text="Unsaved text"),
        make_window(categories={"Personal": None}),
    ],
)
def test_refresh_warns_before_discarding_local_changes(window):
    assert MainWindow._has_unsaved_changes(window)
