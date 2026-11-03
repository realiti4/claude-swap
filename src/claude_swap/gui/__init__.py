"""Desktop GUI for claude-swap.

A single HTML page (``web/index.html``) rendered by the platform's own web
engine through pywebview (WebView2 on Windows, WKWebView on macOS), driving
``GuiApi``. Where no webview is available it falls back to serving the same
page on localhost and opening the default browser (see ``gui.server``).

Entry points: ``cswap-gui`` (console script) and the packaged GUI executable.
"""

from __future__ import annotations

import html
import io
import os
import sys
import tempfile
import traceback
from importlib import resources
from pathlib import Path


def _page() -> str:
    return resources.files("claude_swap.gui").joinpath("web/index.html").read_text("utf-8")


def _prepare_process() -> None:
    # A windowed (no-console) build has no stdout/stderr at all; core code
    # prints freely, so give it somewhere harmless to write. stderr goes to a
    # log file so webview/runtime failures stay diagnosable.
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        try:
            log = Path(tempfile.gettempdir()) / "claude-swap-gui.log"
            sys.stderr = open(log, "w", encoding="utf-8", buffering=1)
        except OSError:
            sys.stderr = io.StringIO()
    # Captured output is shown as plain text; keep the core from emitting ANSI.
    if "NO_COLOR" not in os.environ:
        os.environ["NO_COLOR"] = "1"
        os.environ["CSWAP_GUI_NO_COLOR"] = "1"
    try:
        import truststore

        truststore.inject_into_ssl()  # same native-TLS trust as the CLI
    except Exception:
        pass


def _error_page(message: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>claude-swap</title>"
        "<body style='font:14px system-ui;padding:24px;background:#fff;color:#222'>"
        "<h2>claude-swap 启动失败</h2><pre style='white-space:pre-wrap'>"
        f"{html.escape(message)}</pre></body>"
    )


def _run_webview(api, page: str) -> bool:
    """Run the native window; False when no webview could be started."""
    try:
        import webview
    except ImportError:
        traceback.print_exc()
        return False

    window = webview.create_window(
        "claude-swap",
        html=page,
        js_api=api,
        width=960,
        height=660,
        min_size=(680, 460),
        background_color="#f6f6f4",
        text_select=True,
    )
    if api is not None:
        api._attach_window(window)
    try:
        webview.start(private_mode=True)
    except Exception:
        traceback.print_exc()
        return False
    finally:
        if api is not None:
            api.shutdown()
    return True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    _prepare_process()
    force_browser = "--browser" in argv or os.environ.get("CSWAP_GUI_BROWSER") == "1"

    from claude_swap.gui.api import GuiApi
    from claude_swap.switcher import ClaudeAccountSwitcher

    mode = "browser" if force_browser else "webview"
    try:
        switcher = ClaudeAccountSwitcher(debug="--debug" in argv)
    except Exception as e:
        page = _error_page(f"{type(e).__name__}: {e}")
        if force_browser or not _run_webview(None, page):
            print(page, file=sys.stderr)
        return 1

    page = _page()
    if not force_browser:
        api = GuiApi(switcher, mode="webview")
        if _run_webview(api, page.replace("__CSWAP_TOKEN__", "")):
            return 0
        mode = "browser"

    from claude_swap.gui.server import serve

    return serve(GuiApi(switcher, mode=mode), page)  # the server injects the token
