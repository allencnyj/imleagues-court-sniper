"""Entry point.

Normally launched with pythonw.exe so no console window appears. That
has two consequences worth handling explicitly: sys.stdout is None, so a
StreamHandler cannot be attached blindly; and an unhandled exception has
nowhere to print, so the app would simply vanish. Both are dealt with
below -- everything goes to config/sniper.log, and a crash raises a
dialog rather than disappearing.
"""
from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import CONFIG_DIR, LOG_PATH  # noqa: E402

log = logging.getLogger(__name__)

#: Must match System.AppUserModel.ID on the shortcut (see
#: tools/make_shortcut.py). Windows 11's taskbar sources a button's icon
#: from the app identity rather than from WM_SETICON, so without the
#: pairing the button shows pythonw's Python-logo icon no matter what the
#: window itself carries.
APP_ID = "AllenC.BadmintonSniper"


def locate_browsers() -> None:
    """Point Playwright at the shared per-user browser install.

    Frozen by PyInstaller, Playwright resolves browsers relative to the
    bundled package -- _internal/playwright/driver/package/.local-browsers
    -- which is empty, so sign-in fails with "Executable doesn't exist".
    The actual install (about 700 MB, far too large to bundle) lives in
    the user's LocalAppData and is shared across every Playwright app.
    """
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if local.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local)
        log.info("using browsers at %s", local)
    else:
        log.warning("no Playwright browsers found at %s; sign-in will fail "
                    "until 'playwright install chromium' is run", local)


def claim_app_identity() -> None:
    """Declare our AppUserModelID before any window exists.

    Source runs only. A pythonw process would otherwise share pythonw's
    identity (and Python-logo taskbar icon), so it pairs with the
    shortcut from tools/make_shortcut.py that carries the same ID.

    The frozen exe must NOT do this: its own path already is a unique
    identity with the shuttlecock embedded. Claiming the ID instead
    made Windows look up whichever shortcut carried it -- a stale
    pythonw one from before the exe existed -- and take the taskbar icon
    from there. That is how the birdie "reverted".
    """
    if getattr(sys, "frozen", False):
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception as exc:  # noqa: BLE001 - cosmetic
        log.debug("could not set AppUserModelID: %s", exc)


def setup_logging() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
    # Under pythonw.exe there is no stdout, and handing None to a
    # StreamHandler makes it fall back to stderr -- which is also None.
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _show_crash(text: str) -> None:
    """Surface a fatal error even with no console attached."""
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(
            "Badminton Sniper crashed",
            f"{text}\n\nFull details were written to:\n{LOG_PATH}",
        )
        root.destroy()
    except Exception:  # noqa: BLE001 - last resort; the log already has it
        pass


def install_crash_handler() -> None:
    def hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        logging.critical("unhandled exception",
                         exc_info=(exc_type, exc, tb))
        summary = "".join(traceback.format_exception_only(exc_type, exc)).strip()
        _show_crash(summary)

    sys.excepthook = hook


def main() -> int:
    setup_logging()
    install_crash_handler()
    claim_app_identity()
    locate_browsers()
    log.info("starting (console=%s)", sys.stdout is not None)
    try:
        from app import SniperApp
        SniperApp().mainloop()
    except Exception as exc:  # noqa: BLE001 - never die silently
        logging.critical("failed to start", exc_info=True)
        _show_crash(f"{type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
