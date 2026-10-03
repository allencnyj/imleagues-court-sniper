"""System tray presence, so closing the window doesn't stop a booking.

The app is meant to sit armed for hours. Making the window's X button
quit outright would throw away a queued booking on a misclick, so it
hides to the tray instead and only really exits from the tray menu.

Everything here runs on pystray's own thread. Callbacks must therefore
hand work back to the Tk thread rather than touching widgets directly --
see SniperApp._callbacks.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)


#: Rendered large and downscaled: crisper than drawing at 16px directly.
_ICO_SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]


def _draw_shuttlecock(d, size: int, body, outline=None) -> None:
    """Shuttlecock on a 64-unit grid, scaled to size."""
    s = size / 64.0
    w = max(1, round(1.5 * s))

    # Skirt: a trapezoid, not a triangle. A pointed apex disappears at
    # 16px and the icon reads as an hourglass, so the base is flat and
    # wide enough to visibly meet the cork.
    d.polygon([(25 * s, 38 * s), (39 * s, 38 * s), (55 * s, 7 * s), (9 * s, 7 * s)],
              fill=body, outline=outline, width=w)
    # Creases, to read as feathers rather than a plain shape. Dropped at
    # tiny sizes, where they only muddy the silhouette.
    if size >= 32:
        for x in (18, 27, 37, 46):
            d.line([(32 * s, 36 * s), (x * s, 9 * s)], fill=(0, 0, 0, 70),
                   width=max(1, round(1.2 * s)))
    # Cork, deliberately chunky: at 16px a dainty one vanishes and the
    # icon reads as a plain funnel.
    d.ellipse([(21 * s, 34 * s), (43 * s, 57 * s)], fill=body,
              outline=outline, width=w)


def make_icon_image(size: int = 64, *, armed: bool = False):
    """Tray icon: white shuttlecock, red while armed."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    body = (248, 113, 113, 255) if armed else (250, 250, 250, 255)
    _draw_shuttlecock(ImageDraw.Draw(img), size, body)
    return img


def make_app_icon(path, *, size: int = 256) -> bool:
    """Write a multi-resolution .ico for the window and taskbar.

    Drawn with a dark outline as well as a white body so it stays legible
    against both light and dark taskbars.
    """
    from PIL import Image, ImageDraw

    try:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        _draw_shuttlecock(ImageDraw.Draw(img), size,
                          (250, 250, 250, 255), outline=(18, 18, 20, 235))
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, format="ICO", sizes=_ICO_SIZES)
        return True
    except Exception as exc:  # noqa: BLE001 - an icon is cosmetic
        log.warning("could not write app icon: %s", exc)
        return False


class TrayIcon:
    """A tray icon that outlives the visible window.

    Degrades to a no-op if pystray cannot start (headless session, or a
    shell that has no notification area) -- the app must still work.
    """

    def __init__(
        self,
        *,
        on_open: Callable[[], None],
        on_quit: Callable[[], None],
        on_disarm: Callable[[], None] | None = None,
        is_armed: Callable[[], bool] | None = None,
    ) -> None:
        self._on_open = on_open
        self._on_quit = on_quit
        self._on_disarm = on_disarm
        self._is_armed = is_armed or (lambda: False)
        self._icon = None
        self._thread: threading.Thread | None = None
        self.available = False

    # --- lifecycle ----------------------------------------------------

    def start(self) -> bool:
        if self._icon is not None:
            return self.available
        try:
            import pystray

            menu = pystray.Menu(
                pystray.MenuItem("Open Badminton Sniper",
                                 lambda *_: self._on_open(), default=True),
                pystray.MenuItem("Disarm",
                                 lambda *_: self._on_disarm and self._on_disarm(),
                                 visible=lambda _item: self._is_armed()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda *_: self._on_quit()),
            )
            self._icon = pystray.Icon(
                "badminton-sniper",
                make_icon_image(),
                "Badminton Sniper",
                menu,
            )
            self._thread = threading.Thread(target=self._icon.run, name="tray", daemon=True)
            self._thread.start()
            self.available = True
        except Exception as exc:  # noqa: BLE001 - tray is a convenience
            log.warning("system tray unavailable (%s); the window will minimise instead", exc)
            self._icon = None
            self.available = False
        return self.available

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
            self._icon = None
        self.available = False

    # --- state --------------------------------------------------------

    def set_armed(self, armed: bool) -> None:
        """Turn the icon red while a booking is queued."""
        if self._icon is None:
            return
        try:
            self._icon.icon = make_icon_image(armed=armed)
            self._icon.title = ("Badminton Sniper - armed" if armed
                                else "Badminton Sniper")
            self._icon.update_menu()
        except Exception as exc:  # noqa: BLE001
            log.debug("could not update tray icon: %s", exc)

    def notify(self, message: str, title: str = "Badminton Sniper") -> None:
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title)
        except Exception as exc:  # noqa: BLE001 - notifications are optional
            log.debug("tray notification failed: %s", exc)
