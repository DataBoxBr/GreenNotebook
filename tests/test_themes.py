from rednotebook import themes


def test_normalize_theme_accepts_supported_modes():
    assert themes.normalize_theme("system") == "system"
    assert themes.normalize_theme("light") == "light"
    assert themes.normalize_theme("dark") == "dark"


def test_normalize_theme_rejects_unknown_mode():
    assert themes.normalize_theme("unknown") == "system"
