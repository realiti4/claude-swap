import pytest


@pytest.mark.parametrize("language", ["en", "zh-Hans"])
def test_draw_account_without_alias(language, tmp_path):
    appkit = pytest.importorskip("AppKit")
    rumps = pytest.importorskip("rumps")
    import time
    from types import SimpleNamespace
    from claude_swap.menubar_cards import attach_card
    from claude_swap.menubar_locale import translate
    from claude_swap.settings import load_settings

    class Owner:
        settings = SimpleNamespace(language=language)

        def _t(self, message, **values):
            return translate(language, message, **values)

    owner = Owner()
    appkit.NSApplication.sharedApplication()
    item = rumps.MenuItem("Example")
    attach_card(item, owner, 1, "person@example.com", True,
                {"seven_day": {"pct": 91}, "scoped": [{"name": "Fable", "pct": 24}]}, None, False, time.time() + 86400, False,
                load_settings(tmp_path))
    view = item._menuitem.view()
    assert view.data["name"] == ("Account 1" if language == "en" else "账号 1")
    assert view.data["expiry"]
    assert view.frame().size == (354, 94)
    host = appkit.NSView.alloc().initWithFrame_(((0, 0), (540, 94)))
    host.addSubview_(view)
    assert view.frame().size.width == 540
    image = appkit.NSImage.alloc().initWithSize_(view.bounds().size)
    image.lockFocusFlipped_(True)
    try:
        view.drawRect_(view.bounds())
    finally:
        image.unlockFocus()
    assert image.TIFFRepresentation()
