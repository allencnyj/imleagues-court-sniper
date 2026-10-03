"""Thin client over IMLeagues' single generic AJAX endpoint.

Every SPA call goes through AjaxRequestHandler.ashx with the target
business object named in the query string, and comes back wrapped in a
fixed envelope. This module speaks exactly that protocol -- nothing
more clever.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date as _date, datetime
from typing import Any, Iterable, Sequence

import requests
from requests.adapters import HTTPAdapter

import constants as C
from models import FireResult, Slot

log = logging.getLogger(__name__)

# A current desktop Chrome UA. The site is cookie-authenticated and does
# not fingerprint beyond this, but a blank/py UA is needlessly odd.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_EPOCH = datetime.min.replace(tzinfo=C.SCHOOL_TZ)

#: What cleanedUserEmail reads when nobody is signed in.
GUEST_EMAIL = "guest"


class AuthError(RuntimeError):
    """The session is not (or no longer) signed in."""


@dataclass
class Envelope:
    """The isDone/code/message/data wrapper every BO call returns."""

    is_done: bool
    code: int
    message: str
    data: Any
    correlation_id: str = ""
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> "Envelope":
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            # A redirect to an HTML login page lands here.
            raise AuthError("Non-JSON response (session likely expired)")
        if not isinstance(obj, dict):
            raise AuthError("Unexpected response shape")
        # Some refusals leave the envelope code at 0 and put the real
        # reason in data.code -- code 205 ("you already hold this court")
        # is the one that matters here. Prefer the specific one.
        code = int(obj.get("code", -999) or 0)
        data = obj.get("data")
        if code == 0 and isinstance(data, dict):
            inner = data.get("code")
            if isinstance(inner, int) and inner:
                code = inner
        return cls(
            is_done=bool(obj.get("isDone")),
            code=code,
            message=str(obj.get("message") or ""),
            data=obj.get("data"),
            correlation_id=str(obj.get("correlationId") or ""),
            raw=obj,
        )

    def require(self) -> Any:
        """Return the data field, raising on failure.

        Auth failures raise AuthError so callers can trigger re-login.
        """
        if self.is_done:
            return self.data
        if self.code in C.AUTH_CODES:
            raise AuthError(self.message or "Not authenticated")
        raise RuntimeError(f"{self.message} (code {self.code})")


class ImLeaguesClient:
    """Stateless-ish HTTP client. Cookies live on the underlying session."""

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        # Keep a few pooled connections so a pre-warmed socket is never
        # evicted by a stray call on another thread.
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": C.BASE_URL,
            "Referer": C.RESERVATION_HOME,
        })

    # --- cookies ------------------------------------------------------

    def set_cookies(self, cookies: Iterable[dict[str, Any]]) -> None:
        """Load cookies exported from a Playwright context."""
        self.session.cookies.clear()
        for ck in cookies:
            name, value = ck.get("name"), ck.get("value")
            if not name or value is None:
                continue
            domain = (ck.get("domain") or "").lstrip(".") or "www.imleagues.com"
            self.session.cookies.set(name, value, domain=domain, path=ck.get("path") or "/")

    def has_cookies(self) -> bool:
        return len(self.session.cookies) > 0

    # --- transport ----------------------------------------------------

    def _url(self, bo: Sequence[str], writable: bool, referrer: str) -> str:
        cls_name, method, param_type = bo
        path = C.AJAX_RW_PATH if writable else C.AJAX_PATH
        quote = requests.utils.quote
        return (
            f"{C.BASE_URL}{path}"
            f"?class=imLeagues.Web.Members.Services.BO.{quote(cls_name)}"
            f"&method={quote(method)}"
            f"&paramType=imLeagues.Internal.API.VO.Input.{quote(param_type)}"
            f"&urlReferrer={quote(referrer, safe='')}"
        )

    def call_bo(
        self,
        bo: Sequence[str],
        data: dict[str, Any] | None = None,
        *,
        writable: bool = False,
        referrer: str = C.RESERVATION_HOME,
        timeout: float | None = None,
    ) -> Envelope:
        url = self._url(bo, writable, referrer)
        body = json.dumps(data or {}, separators=(",", ":"))
        resp = self.session.post(url, data=body, timeout=timeout or self.timeout)
        return Envelope.parse(resp.text)

    def prewarm(self) -> float | None:
        """Complete a TCP+TLS handshake so the real shot is one round trip.

        Returns the round-trip time in ms, or None if it failed.
        """
        try:
            t0 = time.perf_counter()
            self.session.get(f"{C.BASE_URL}/spa/images/iml-logo.svg", timeout=self.timeout)
            return (time.perf_counter() - t0) * 1000.0
        except requests.RequestException as exc:
            log.warning("prewarm failed: %s", exc)
            return None

    # --- the three calls that matter ----------------------------------

    def search_slots(self, start: _date, end: _date) -> list[Slot]:
        """All badminton court slots in the inclusive date range.

        Works unauthenticated (the calendar is public); the is_registered
        flag is only meaningful when signed in.
        """
        env = self.call_bo(C.BO_SEARCH_SESSIONS, {
            "schoolId": C.SCHOOL_ID,
            "startDate": f"{start.month}/{start.day}/{start.year}",
            "endDate": f"{end.month}/{end.day}/{end.year}",
            "classID": None,
            "enableReservation": 1,
        })
        days = env.require() or []
        slots: list[Slot] = []
        for day in days:
            for ev in day.get("eventsList") or []:
                slot = Slot.from_api(ev)
                if slot is not None:
                    slots.append(slot)
        slots.sort(key=lambda s: (s.start or _EPOCH, s.court))
        return slots

    def init_register(self, event_id: int) -> dict[str, Any]:
        """Load the registration form model for one event.

        Requires authentication. Returns the registerFitnessEventVM dict.
        """
        env = self.call_bo(C.BO_REGISTER_INIT, {
            "entityType": "reservation",
            "entityId": C.SCHOOL_ID,
            "pageType": "Reservation",
            "eventId": event_id,
        }, referrer=C.register_page(event_id))
        data = env.require() or {}
        return data.get("registerFitnessEventVM") or {}

    def register_session(self, payload: dict[str, Any], *, court: int) -> FireResult:
        """Fire the actual booking. This is the request that counts."""
        event_id = int(payload.get("eventId", 0))
        t0 = time.perf_counter()
        env = self.call_bo(
            C.BO_REGISTER_SESSION, payload,
            referrer=C.register_page(event_id),
            timeout=10.0,
        )
        elapsed = (time.perf_counter() - t0) * 1000.0
        return FireResult(
            ok=env.is_done,
            code=env.code,
            message=env.message,
            court=court,
            event_id=event_id,
            elapsed_ms=elapsed,
            raw=env.raw,
        )

    # --- reservations: status + unregister ---------------------------

    def reservation_info(self, event_id: int) -> dict[str, Any]:
        """Look up one held reservation: its member id and waitlist state.

        Returns {"event_member_id": int|None, "waitlisted": bool,
        "registered": bool}. The member id is what unregister() needs, and
        the waitlist flag distinguishes a confirmed booking from a
        waitlist spot -- the two look the same in the calendar.
        """
        try:
            env = self.call_bo(C.BO_VIEW_EVENT_INIT, {
                "entityType": "reservation", "entityId": C.SCHOOL_ID,
                "pageType": "Reservation", "eventId": event_id,
            }, referrer=C.view_event_page(event_id))
        except (AuthError, requests.RequestException) as exc:
            log.info("reservation_info(%s) failed: %s", event_id, exc)
            return {"event_member_id": None, "waitlisted": False, "registered": False}

        vm = env.data.get("sessionOutVM") if isinstance(env.data, dict) else None
        if not isinstance(vm, dict):
            return {"event_member_id": None, "waitlisted": False, "registered": False}

        member_id = vm.get("eventMemberId")
        # We are waitlisted if our own entry in the waitlist list is
        # flagged in-waitlist.
        waitlisted = False
        for entry in vm.get("waitlist") or []:
            if isinstance(entry, dict) and entry.get("id") == member_id \
                    and entry.get("isInWaitlist"):
                waitlisted = True
                break
        return {
            "event_member_id": int(member_id) if member_id else None,
            "waitlisted": waitlisted,
            "registered": member_id is not None,
        }

    def unregister(self, event_member_id: int) -> tuple[bool, str]:
        """Cancel one reservation (or leave its waitlist). Returns (ok, msg).

        Uses the legacy page-BO endpoint the site's own Leave button uses.
        """
        quote = requests.utils.quote
        url = (f"{C.BASE_URL}{C.UNREGISTER_PATH}"
               f"?class={quote(C.UNREGISTER_CLASS)}"
               f"&method={quote(C.UNREGISTER_METHOD)}"
               f"&ID={int(event_member_id)}")
        try:
            resp = self.session.post(url, data="", timeout=self.timeout)
        except requests.RequestException as exc:
            return False, str(exc)
        try:
            obj = json.loads(resp.text)
        except (ValueError, TypeError):
            # Some legacy handlers return a bare string/HTML on success.
            ok = resp.status_code < 400 and "error" not in resp.text.lower()
            return ok, resp.text.strip()[:200]
        if isinstance(obj, dict):
            done = obj.get("IsDone", obj.get("isDone"))
            msg = obj.get("Message") or obj.get("message") or ""
            return bool(done), str(msg)
        return bool(obj), ""

    # --- session health ----------------------------------------------

    def touch(self) -> bool:
        """Load a real page, to reset the server's idle timer.

        The BO endpoints go through AjaxRequestHandler.ashx, and the
        evidence says those do not slide the ASP.NET session timeout: a
        session that was pinged every 10 minutes still died 20 minutes
        after the last genuine page load. A full page request is what
        the server counts as activity.
        """
        try:
            resp = self.session.get(C.RESERVATION_HOME, timeout=self.timeout)
            return resp.status_code < 400
        except requests.RequestException as exc:
            log.debug("keep-alive touch failed: %s", exc)
            return False

    def whoami(self) -> dict[str, Any] | None:
        """Who is this session? Returns identity, or None if signed out.

        Careful: this endpoint answers anonymous callers perfectly happily
        -- the reservation calendar is public. A successful response is
        therefore NOT proof of login. The actual signal is
        cleanedUserEmail, which reads "guest" when nobody is signed in.

        Doubles as the keep-alive ping.
        """
        try:
            env = self.call_bo(C.BO_RESERVATION_HOME_INIT, {
                "entityType": "reservation",
                "entityId": C.SCHOOL_ID,
                "pageType": "Reservation",
            })
        except (AuthError, requests.RequestException):
            return None
        if not env.is_done or not isinstance(env.data, dict):
            return None

        email = str(env.data.get("cleanedUserEmail") or "").strip()
        if not email or email.lower() == GUEST_EMAIL:
            return None
        return {"email": email, "data": env.data}
