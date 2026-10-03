"""Authentication, via a real browser with a saved profile.

UTD is SSO-locked: imleagues.com hands off to a UT Dallas NetID page.
Rather than handling credentials, this keeps a persistent Chromium
profile. You sign in once, by hand; the profile then holds both the
IMLeagues auth cookie and the identity provider's trusted-device cookie,
so later re-authentication is a silent redirect round trip with no
password involved.

The browser is only ever used to *obtain* cookies. Everything
time-critical runs on plain HTTP with those cookies, because launching a
browser takes seconds we do not have.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import constants as C

log = logging.getLogger(__name__)

#: Anonymous visitors already get ASP.NET_SessionId, so no cookie name is
#: proof of login. Having *any* cookie is only a cheap gate to avoid an
#: API call on a blank profile; is_authenticated is what actually decides.

SignInProgress = Callable[[str], None]


def _browser_error(exc: Exception) -> str:
    """Turn Playwright's browser-missing error into an actionable message."""
    text = str(exc)
    if "Executable doesn't exist" in text or "playwright install" in text:
        return ("The sign-in browser is not installed on this computer. "
                "Open Command Prompt and run:  pip install playwright  "
                "then:  playwright install chromium")
    return f"Browser error: {text}"


@dataclass
class SignInResult:
    ok: bool
    cookies: list[dict[str, Any]]
    detail: str = ""
    #: True when a human needs to intervene (password / MFA prompt).
    needs_human: bool = False


class BrowserSession:
    """Owns the on-disk Chromium profile used for authentication.

    Each operation launches a browser, does its work, and closes it. The
    profile directory is what persists, not the process -- which keeps
    Playwright's sync API safely confined to one call at a time.
    """

    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir
        self.profile_dir.mkdir(parents=True, exist_ok=True)

    # --- internals ----------------------------------------------------

    def _launch(self, playwright, headless: bool):
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=headless,
            viewport={"width": 1280, "height": 860},
            args=["--disable-blink-features=AutomationControlled"],
        )

    @staticmethod
    def _looks_authenticated(cookies: list[dict[str, Any]]) -> bool:
        return bool(cookies)

    @staticmethod
    def _on_login_page(url: str) -> bool:
        low = (url or "").lower()
        return ("/login" in low or "/account/login" in low
                or "adfs" in low or "microsoftonline" in low
                or "duosecurity" in low or "okta" in low)

    # --- public operations --------------------------------------------

    def sign_in_interactive(
        self,
        *,
        timeout_s: float = 600.0,
        is_authenticated: Callable[[list[dict[str, Any]]], bool] | None = None,
        progress: SignInProgress | None = None,
    ) -> SignInResult:
        """Open a visible browser and wait for the user to sign in.

        Returns once an authenticated session is detected, the window is
        closed, or the timeout expires. Nothing is typed on the user's
        behalf -- they do the whole sign-in themselves.
        """
        from playwright.sync_api import Error as PWError, sync_playwright

        def say(msg: str) -> None:
            log.info(msg)
            if progress:
                progress(msg)

        with sync_playwright() as pw:
            ctx = None
            try:
                ctx = self._launch(pw, headless=False)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                say("Opening IMLeagues. Sign in with your UTD NetID in the browser window.")
                page.goto(C.RESERVATION_HOME, wait_until="domcontentloaded", timeout=60_000)

                deadline = time.monotonic() + timeout_s
                last_note = 0.0
                last_check = 0.0
                last_fingerprint = None
                while time.monotonic() < deadline:
                    # The user closing the window is a deliberate cancel.
                    if not ctx.pages:
                        return SignInResult(False, [], "Browser window was closed.", True)

                    cookies = ctx.cookies()
                    fingerprint = {(c.get("name"), c.get("value")) for c in cookies}
                    # Verifying costs a round trip, so only spend one when
                    # the cookies actually moved -- or occasionally, in
                    # case login completed without a visible cookie change.
                    changed = fingerprint != last_fingerprint
                    due = time.monotonic() - last_check > 10.0
                    if self._looks_authenticated(cookies) and (changed or due):
                        last_fingerprint = fingerprint
                        last_check = time.monotonic()
                        if is_authenticated is None or is_authenticated(cookies):
                            say("Signed in. Saving session.")
                            return SignInResult(True, cookies, "Signed in.")

                    if time.monotonic() - last_note > 20:
                        last_note = time.monotonic()
                        say("Waiting for sign-in to complete...")
                    time.sleep(1.0)

                return SignInResult(False, [], "Timed out waiting for sign-in.", True)
            except PWError as exc:
                return SignInResult(False, [], _browser_error(exc), True)
            finally:
                try:
                    if ctx is not None:
                        ctx.close()
                except Exception:  # noqa: BLE001 - closing must never mask a result
                    pass

    def refresh_cookies(
        self,
        *,
        is_authenticated: Callable[[list[dict[str, Any]]], bool] | None = None,
        headless: bool = True,
        timeout_s: float = 90.0,
        seed_cookies: list[dict[str, Any]] | None = None,
    ) -> SignInResult:
        """Try to re-establish the session without asking for credentials.

        Important limitation, learned the hard way: every cookie that
        carries the login -- ASP.NET_SessionId, ApiTokenForSPA, and the
        Shibboleth SSO cookie -- is a *session* cookie, and Chromium
        never writes those to disk. So the saved profile contains no
        authentication at all and cannot resume anything by itself.

        seed_cookies (from the encrypted cache) is therefore the only
        thing that can make this work, and only while the server side of
        that session is still alive. Once it has expired, no amount of
        navigating will bring it back and a human has to sign in.

        Nothing is ever typed on the user's behalf, and this never
        retries: repeated failed logins risk locking the account.
        """
        from playwright.sync_api import Error as PWError, sync_playwright

        with sync_playwright() as pw:
            ctx = None
            try:
                ctx = self._launch(pw, headless=headless)
                if seed_cookies:
                    try:
                        ctx.add_cookies([c for c in seed_cookies if c.get("name")])
                    except PWError as exc:
                        log.warning("could not seed cookies: %s", exc)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                # The reservation page, not SSO_REDIRECT: with no
                # Shibboleth cookie that redirect only reaches
                # Default.aspx, which proves nothing.
                page.goto(C.RESERVATION_HOME, wait_until="domcontentloaded",
                          timeout=int(timeout_s * 1000))
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except PWError:
                    pass  # a busy SPA may never go fully idle; not fatal

                if self._on_login_page(page.url):
                    return SignInResult(
                        False, [],
                        "Sign-in required: the identity provider asked for credentials.",
                        needs_human=True,
                    )

                cookies = ctx.cookies()
                if self._looks_authenticated(cookies) and (
                        is_authenticated is None or is_authenticated(cookies)):
                    return SignInResult(True, cookies, "Session refreshed.")

                return SignInResult(
                    False, cookies,
                    "The saved session has expired; signing in again is the "
                    "only way back.",
                    needs_human=True,
                )
            except PWError as exc:
                return SignInResult(False, [], _browser_error(exc), needs_human=True)
            finally:
                try:
                    if ctx is not None:
                        ctx.close()
                except Exception:  # noqa: BLE001
                    pass

    def open_page(self, url: str, *, timeout_s: float = 300.0,
                  wait_for: Callable[[Any], bool] | None = None) -> SignInResult:
        """Show the user a real page and wait, e.g. to solve a captcha.

        Returns cookies once wait_for is satisfied or the window closes.
        """
        from playwright.sync_api import Error as PWError, sync_playwright

        with sync_playwright() as pw:
            ctx = None
            try:
                ctx = self._launch(pw, headless=False)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    if not ctx.pages:
                        return SignInResult(False, [], "Window closed.", True)
                    if wait_for is not None and wait_for(page):
                        return SignInResult(True, ctx.cookies(), "Done.")
                    time.sleep(0.5)
                return SignInResult(False, ctx.cookies(), "Timed out.", True)
            except PWError as exc:
                return SignInResult(False, [], _browser_error(exc), True)
            finally:
                try:
                    if ctx is not None:
                        ctx.close()
                except Exception:  # noqa: BLE001
                    pass

    def keep_alive_page(self) -> str:
        """A cheap authenticated URL, for keeping the session warm."""
        return C.RESERVATION_HOME

    def cached_cookies(self) -> list[dict[str, Any]]:
        """Read cookies straight out of the saved profile, no navigation.

        Fast path used at startup: if these still work, no browser
        round trip is needed at all.
        """
        from playwright.sync_api import Error as PWError, sync_playwright

        try:
            with sync_playwright() as pw:
                ctx = self._launch(pw, headless=True)
                try:
                    return ctx.cookies()
                finally:
                    ctx.close()
        except PWError as exc:
            log.warning("could not read cached cookies: %s", exc)
            return []


class SessionKeeper:
    """Keeps the login warm while the app sits armed.

    A queued booking may be many hours away. Discovering a dead session
    a minute before the slot opens is too late to do anything about it,
    so this checks periodically and repairs the session quietly, long
    before it matters. If a repair needs a human it says so loudly and
    early, rather than failing silently at 3am.
    """

    def __init__(
        self,
        *,
        check: Callable[[], bool],
        refresh: Callable[[], bool],
        notify: Callable[[str, str], None],
        interval_s: float = 600.0,
        defer: Callable[[], bool] | None = None,
        renew: Callable[[], bool] | None = None,
        renew_every_s: float = 6 * 3600.0,
    ) -> None:
        self._check = check
        self._refresh = refresh
        self._notify = notify
        self._interval = interval_s
        self._defer = defer
        self._renew = renew
        self._renew_every = renew_every_s
        self._last_renew = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.healthy = True
        #: Set once recovery has failed. Nothing the keeper can do will
        #: help until a human signs in, so it goes quiet rather than
        #: retrying forever -- an earlier version launched a browser
        #: window every 10 minutes, 35 times overnight.
        self.needs_user = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="session-keeper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None

    def resume(self) -> None:
        """Called after a successful manual sign-in."""
        self.needs_user = False
        self.healthy = True
        self._last_renew = time.monotonic()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            # Recovery already failed; only a person can fix this.
            if self.needs_user:
                continue
            # Never compete with an imminent shot: a stray request could
            # evict the pre-warmed connection the fire depends on.
            if self._defer is not None and self._defer():
                continue
            try:
                if self._check():
                    self.healthy = True
                    self._maybe_renew()
                    continue
                self._notify("warn", "Session went stale; refreshing it now, "
                                     "well before the next booking.")
                if self._refresh():
                    self.healthy = True
                    self._notify("success", "Session refreshed automatically.")
                else:
                    self.healthy = False
                    self.needs_user = True
                    self._notify("error",
                                 "Signed out, and the session cannot be restored "
                                 "automatically. Click Sign in - an armed booking "
                                 "WILL fail until you do. (Not retrying further.)")
            except Exception as exc:  # noqa: BLE001 - a dead keeper is worse
                log.warning("session keep-alive failed: %s", exc)

    def _maybe_renew(self) -> None:
        """Slide the identity session forward before it can lapse.

        Waiting for expiry means eventually needing the user, and Duo,
        at whatever hour that happens to be. Re-walking the SSO redirect
        while the session is still healthy refreshes both the IMLeagues
        cookie and the identity provider's, so the window keeps moving
        and a manual sign-in stays rare.

        Silent and headless only. A renewal that fails is not a problem
        worth interrupting anyone over -- the session is still alive, and
        the ordinary check will catch it later if that changes.
        """
        if self._renew is None:
            return
        if time.monotonic() - self._last_renew < self._renew_every:
            return
        self._last_renew = time.monotonic()
        try:
            if self._renew():
                log.info("identity session renewed proactively")
            else:
                log.info("proactive renewal did not take; session still healthy")
        except Exception as exc:  # noqa: BLE001
            log.warning("proactive renewal failed: %s", exc)
