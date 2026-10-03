"""Silent reconnect at startup, and the waiver cached-form fallback.

Both are about the same promise: sign in once, and never be blocked by
"nothing is open" again. No network or browser is touched.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402
from accounts import Account  # noqa: E402
from payload import read_requirements  # noqa: E402
from session import SignInResult  # noqa: E402


@pytest.fixture
def make_session(tmp_path, monkeypatch):
    """An AccountSession whose browser/client are fully stubbed."""
    monkeypatch.setattr(config, "ACCOUNTS_DIR", tmp_path / "accounts")

    import account_session

    def build(account, *, refresh_ok_headless=False, adopt_ok=True):
        s = account_session.AccountSession(
            account, notify=lambda *a: None,
            on_signed_in=lambda _s: None, fire_imminent=lambda: False)

        def refresh_cookies(*, is_authenticated=None, headless=True,
                            timeout_s=90.0, seed_cookies=None):
            ok = bool(refresh_ok_headless)
            cookies = [{"name": "ASP.NET_SessionId", "value": "x"}] if ok else []
            # Mirror the real browser: it confirms via is_authenticated,
            # which is what records signed_in and the email.
            if ok and is_authenticated is not None:
                ok = is_authenticated(cookies)
            return SignInResult(ok, cookies, "ok" if ok else "expired",
                                needs_human=not ok)

        monkeypatch.setattr(s.browser, "refresh_cookies", refresh_cookies)
        monkeypatch.setattr(s.client, "set_cookies", lambda c: None)
        monkeypatch.setattr(s.client, "whoami",
                            lambda: {"email": account.label or "x@utd.edu"} if adopt_ok
                            else None)
        return s

    return build


def test_has_prior_session_reflects_the_cookie_cache(make_session):
    s = make_session(Account(id="primary", label="a@utd.edu"))
    assert not s.has_prior_session
    s.cookie_cache.parent.mkdir(parents=True, exist_ok=True)
    s.cookie_cache.write_bytes(b"blob")
    assert s.has_prior_session


def test_silent_reauth_reconnects_without_a_window(make_session):
    s = make_session(Account(id="primary", label="a@utd.edu"),
                     refresh_ok_headless=True)
    assert s.silent_reauth() is True
    assert s.signed_in


def test_silent_reauth_gives_up_quietly_when_sso_wants_a_password(make_session):
    # Headless fails (trusted-device cookie lapsed). silent_reauth must
    # NOT fall through to a visible window -- that is for interactive
    # reauth only.
    windows = []
    s = make_session(Account(id="primary", label="a@utd.edu"),
                     refresh_ok_headless=False)

    orig = s.browser.refresh_cookies

    def spy(**kw):
        windows.append(kw.get("headless"))
        return orig(**kw)

    s.browser.refresh_cookies = spy
    assert s.silent_reauth() is False
    assert windows == [True], "silent reconnect must stay headless"


def test_interactive_reauth_falls_through_to_a_visible_window(make_session):
    # Headless fails, visible succeeds: the one path allowed to prompt.
    seen = []
    s = make_session(Account(id="primary", label="a@utd.edu"))

    def refresh(*, is_authenticated=None, headless=True, timeout_s=90.0,
                seed_cookies=None):
        seen.append(headless)
        ok = not headless
        cookies = [{"name": "x", "value": "1"}] if ok else []
        return SignInResult(ok, cookies, "", needs_human=not ok)

    s.browser.refresh_cookies = refresh
    assert s.reauth() is True
    assert seen == [True, False], "must try headless first, then visible"


# --- the waiver cached-form fallback ---------------------------------

def test_read_requirements_from_a_saved_form_matches_a_live_read():
    """The saved form_model is a real registerFitnessEventVM, so the
    dialog can be built from it verbatim when no court is open."""
    vm = {"waiver": {"show": True, "text": "Hold harmless..."},
          "showDigitalSignature": True, "waiverQuestions": [{"question": "Age?"}],
          "sid": {"show": False}, "phone": {"show": False}}
    reqs = read_requirements(vm)
    assert reqs.waiver_shown and reqs.waiver_text.startswith("Hold harmless")
    assert reqs.needs_signature
    assert len(reqs.waiver_questions) == 1
