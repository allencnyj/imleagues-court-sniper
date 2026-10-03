"""Live runtime for one account: client, browser, keeper, sign-in state.

All the per-account session logic lives here, so the window can hold a
list of these and treat each account identically. Nothing here touches
Tk -- methods block and return, and the window runs them off its own
background thread, exactly as it did for a single account.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

import secrets_store
from accounts import Account
from config import account_paths
from imleagues_api import ImLeaguesClient
from session import BrowserSession, SessionKeeper

log = logging.getLogger(__name__)

Notify = Callable[[str, str], None]


class AccountSession:
    """One account's client + browser + keeper, plus its signed-in state."""

    def __init__(
        self,
        account: Account,
        *,
        notify: Notify,
        on_signed_in: Callable[["AccountSession"], None] | None = None,
        fire_imminent: Callable[[], bool] | None = None,
        on_form_saved: Callable[[], None] | None = None,
    ) -> None:
        self.account = account
        self.profile_dir, self.cookie_cache = account_paths(account.id)
        self.client = ImLeaguesClient()
        self.browser = BrowserSession(self.profile_dir)
        self._notify = notify
        self._on_signed_in = on_signed_in or (lambda _s: None)
        self._on_form_saved = on_form_saved or (lambda: None)
        self.signed_in = False
        self.email = ""
        self.keeper = SessionKeeper(
            check=self.session_alive,
            refresh=self.reauth,
            renew=self.renew,
            notify=lambda level, msg: notify(level, self._tag(msg)),
            defer=fire_imminent,
            interval_s=240.0,
            renew_every_s=2 * 3600.0,
        )

    # --- identity -----------------------------------------------------

    @property
    def id(self) -> str:
        return self.account.id

    @property
    def display(self) -> str:
        return self.email or self.account.display

    def _tag(self, msg: str) -> str:
        return f"[{self.display}] {msg}"

    def say(self, level: str, msg: str) -> None:
        self._notify(level, self._tag(msg))

    # --- cookies ------------------------------------------------------

    def cached_cookies(self) -> list[dict[str, Any]]:
        blob = secrets_store.read_secret(self.cookie_cache)
        if not blob:
            return []
        try:
            return json.loads(blob.decode("utf-8"))
        except ValueError:
            return []

    def _cache_cookies(self, cookies: list[dict[str, Any]]) -> None:
        try:
            secrets_store.write_secret(self.cookie_cache,
                                       json.dumps(cookies).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            log.warning("could not serialise cookies for %s: %s", self.id, exc)

    def _adopt(self, cookies: list[dict[str, Any]]) -> bool:
        """Load cookies, verify a real login, cache and record identity."""
        if not cookies:
            return False
        self.client.set_cookies(cookies)
        who = self.client.whoami()
        if who is None:
            return False
        self.email = who.get("email", "")
        if not self.account.label:
            self.account.label = self.email
        self.signed_in = True
        self._cache_cookies(cookies)
        return True

    # --- health / recovery -------------------------------------------

    def session_alive(self) -> bool:
        self.client.touch()
        alive = self.client.whoami() is not None
        self.signed_in = alive
        return alive

    def _refresh(self, *, headless: bool) -> bool:
        how = "silently" if headless else "in a visible window"
        self.say("info", f"refreshing the session {how}...")
        result = self.browser.refresh_cookies(is_authenticated=self._adopt,
                                              headless=headless,
                                              seed_cookies=self.cached_cookies())
        if result.ok:
            self.client.set_cookies(result.cookies)
            self._cache_cookies(result.cookies)
            self._on_signed_in(self)
            return True
        self.say("warn", f"refresh {how} did not take: {result.detail}")
        return False

    def reauth(self) -> bool:
        """Re-establish the session without any credentials.

        Tries a silent headless round trip first, then a visible one --
        the visible pass is what lets Duo/SSO re-prompt on the rare
        occasion the trusted-device cookie has finally lapsed.
        """
        for headless in (True, False):
            if self._refresh(headless=headless):
                return True
        self.signed_in = False
        return False

    def silent_reauth(self) -> bool:
        """Headless-only re-auth: never pops a window. Used at startup.

        The saved profile keeps the identity provider's persistent
        trusted-device cookie, so navigating to the reservation page
        round-trips through SSO and comes back signed in with no
        password and no interaction -- for as long as UTD keeps trusting
        this device. If SSO wants credentials it fails cleanly here and
        the user signs in by hand.
        """
        return self._refresh(headless=True)

    @property
    def has_prior_session(self) -> bool:
        """True once this account has been signed in at least once.

        The cookie cache is written on every successful adopt, so its
        existence means a silent reconnect is worth attempting rather
        than launching a browser against a never-used profile.
        """
        return self.cookie_cache.exists()

    def renew(self) -> bool:
        result = self.browser.refresh_cookies(is_authenticated=self._adopt,
                                              headless=True,
                                              seed_cookies=self.cached_cookies())
        if result.ok:
            self.client.set_cookies(result.cookies)
            self._cache_cookies(result.cookies)
            return True
        return False

    # --- startup / sign-in -------------------------------------------

    def try_cached_session(self) -> bool:
        cached = self.cached_cookies()
        if cached and self._adopt(cached):
            return True
        return self._adopt(self.browser.cached_cookies())

    def sign_in_interactive(self, progress: Callable[[str], None] | None = None):
        """Open a visible browser for the user to sign in. Blocking."""
        result = self.browser.sign_in_interactive(
            is_authenticated=self._adopt,
            progress=progress or (lambda m: self.say("info", m)))
        if result.ok:
            self.client.set_cookies(result.cookies)
            self._cache_cookies(result.cookies)
            self.keeper.resume()
        return result

    # --- form model / consent ----------------------------------------

    def remember_form(self, vm: dict[str, Any]) -> None:
        if not vm:
            return
        self.account.form_model = vm
        self._on_form_saved()

    # --- lifecycle ----------------------------------------------------

    def start_keeper(self) -> None:
        self.keeper.start()

    def stop_keeper(self) -> None:
        self.keeper.stop()
