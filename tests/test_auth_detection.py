"""Tests for telling a signed-in session from an anonymous one.

Regression guard for a real bug: whoami() used to treat any successful
response as proof of login. But the reservation endpoints answer
anonymous callers perfectly happily -- the calendar is public -- so the
app showed a green "signed in" dot with no login at all, and only fell
over later when it hit a genuinely gated endpoint.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import constants as C  # noqa: E402
from imleagues_api import AuthError, Envelope, ImLeaguesClient  # noqa: E402


def client_returning(payload: dict) -> ImLeaguesClient:
    """A client whose transport is replaced by a canned envelope."""
    client = ImLeaguesClient()
    client.call_bo = lambda *a, **k: Envelope.parse(json.dumps(payload))  # type: ignore[method-assign]
    return client


# The real anonymous response, trimmed: the call succeeds, but the
# identity field says guest.
ANON = {"isDone": True, "code": 0, "message": "",
        "data": {"cleanedUserEmail": "guest", "classes": [], "color": "#fff"}}

SIGNED_IN = {"isDone": True, "code": 0, "message": "",
             "data": {"cleanedUserEmail": "abc123456@utdallas.edu", "classes": []}}


def test_anonymous_session_is_not_treated_as_signed_in():
    assert client_returning(ANON).whoami() is None


def test_signed_in_session_reports_the_account_email():
    who = client_returning(SIGNED_IN).whoami()
    assert who is not None
    assert who["email"] == "abc123456@utdallas.edu"


@pytest.mark.parametrize("email", ["guest", "GUEST", "Guest", "", "   ", None])
def test_every_flavour_of_guest_reads_as_signed_out(email):
    payload = {"isDone": True, "code": 0, "message": "",
               "data": {"cleanedUserEmail": email}}
    assert client_returning(payload).whoami() is None


def test_failed_envelope_reads_as_signed_out():
    payload = {"isDone": False, "code": C.CODE_ACCESS_VIOLATION,
               "message": "denied", "data": None}
    assert client_returning(payload).whoami() is None


def test_non_dict_data_reads_as_signed_out():
    payload = {"isDone": True, "code": 0, "message": "", "data": "not-a-dict"}
    assert client_returning(payload).whoami() is None


def test_transport_failure_reads_as_signed_out():
    client = ImLeaguesClient()

    def boom(*_a, **_k):
        raise AuthError("session expired")

    client.call_bo = boom  # type: ignore[method-assign]
    assert client.whoami() is None


# --- cookie handling --------------------------------------------------

def test_playwright_domain_cookies_are_sent_to_www():
    """Playwright reports .imleagues.com; the request goes to www."""
    import requests

    client = ImLeaguesClient()
    client.set_cookies([
        {"name": "ASP.NET_SessionId", "value": "abc", "domain": ".imleagues.com", "path": "/"},
        {"name": "OTHER", "value": "def", "domain": "www.imleagues.com", "path": "/"},
    ])
    prepped = client.session.prepare_request(
        requests.Request("POST", f"{C.BASE_URL}{C.AJAX_PATH}"))
    header = prepped.headers.get("Cookie") or ""
    assert "ASP.NET_SessionId=abc" in header
    assert "OTHER=def" in header


def test_set_cookies_replaces_rather_than_accumulates():
    client = ImLeaguesClient()
    client.set_cookies([{"name": "A", "value": "1", "domain": ".imleagues.com", "path": "/"}])
    client.set_cookies([{"name": "B", "value": "2", "domain": ".imleagues.com", "path": "/"}])
    names = {c.name for c in client.session.cookies}
    assert names == {"B"}, "stale cookies from a dead session must not linger"


def test_cookies_without_a_name_are_skipped():
    client = ImLeaguesClient()
    client.set_cookies([{"value": "1"}, {"name": "OK", "value": "2"}])
    assert {c.name for c in client.session.cookies} == {"OK"}
