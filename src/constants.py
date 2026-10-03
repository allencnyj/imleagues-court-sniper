"""Static facts about UT Dallas' IMLeagues reservation network.

Everything here was read off the live site (the AngularJS SPA under
/spa/, plus its `constants.codes` table in /spa/index.js). Nothing is
guessed.
"""
from __future__ import annotations

import re
from zoneinfo import ZoneInfo

# --- Network identity -------------------------------------------------

SCHOOL_ID = "a2ff00c067844f4898ad8795d1bfde2d"
SCHOOL_NAME = "University of Texas at Dallas"
BASE_URL = "https://www.imleagues.com"

# The school's local time. Every date/time string the API hands back is
# in this zone, with no offset attached.
SCHOOL_TZ = ZoneInfo("America/Chicago")

# --- URLs -------------------------------------------------------------

AJAX_PATH = "/Services/AjaxRequestHandler.ashx"
# Login needs a *writable* session so the auth cookie can be set.
AJAX_RW_PATH = "/Services/AjaxRequestHandlerWithWritableSession.ashx"

RESERVATION_HOME = f"{BASE_URL}/spa/reservation/{SCHOOL_ID}/home"
LOGIN_PAGE = f"{BASE_URL}/spa/account/login"
SSO_REDIRECT = f"{BASE_URL}/account/ssoredirect?schoolId={SCHOOL_ID}"


def register_page(event_id: int) -> str:
    return f"{BASE_URL}/spa/reservation/{SCHOOL_ID}/registerevent?eventId={event_id}"


def view_event_page(event_id: int) -> str:
    return f"{BASE_URL}/spa/reservation/{SCHOOL_ID}/viewevent?eventId={event_id}"


# --- Business-object endpoints ---------------------------------------
# (class, method, paramType) triples. The handler prefixes them with
# imLeagues.Web.Members.Services.BO. and
# imLeagues.Internal.API.VO.Input. respectively.

BO_SEARCH_SESSIONS = (
    "Fitness.HomeBO",
    "SearchScheduledFitnessSessions",
    "Fitness.SearchScheduledSessionsInVO",
)
BO_REGISTER_INIT = (
    "Reservation.RegisterEventBO",
    "Initialize",
    "Fitness.GetViewEventVMInVO",
)
BO_REGISTER_SESSION = (
    "Reservation.RegisterEventBO",
    "RegisterSession",
    "Fitness.RegisterSessionInVO",
)
BO_RESERVATION_HOME_INIT = (
    "Reservation.HomeBO",
    "Initialize",
    "ViewInVO",
)
BO_VIEW_EVENT_INIT = (
    "Reservation.ViewEventBO",
    "Initialize",
    "Fitness.GetViewEventVMInVO",
)

#: Personal unregister is a legacy Pages.BO call on the ROOT handler
#: (not /Services/), taking the eventMemberId as an ID query parameter
#: and an empty body. class -> method.
UNREGISTER_PATH = "/AjaxRequestHandler.ashx"
UNREGISTER_CLASS = "imLeagues.Web.Members.Pages.BO.School.FitnessBO"
UNREGISTER_METHOD = "AjaxRemoveFitnessEventParticipant"
BO_LOGIN_INIT = (
    "Account.LoginBO",
    "Initialize",
    "InitLoginInViewVO",
)

# --- Courts -----------------------------------------------------------

COURTS = (1, 2, 3)
# "RCW Badminton Reservations Court 2" -> 2
_COURT_RE = re.compile(r"court\s*(\d+)", re.IGNORECASE)
# Only these events are ours; the calendar can carry other programs.
BADMINTON_NAME_HINT = "badminton"


def court_from_name(name: str) -> int | None:
    """Pull the court number out of an event name, or None if absent."""
    if not name or BADMINTON_NAME_HINT not in name.lower():
        return None
    m = _COURT_RE.search(name)
    return int(m.group(1)) if m else None


# --- Response envelope codes -----------------------------------------
# Verbatim from `constants.codes` in /spa/index.js.

CODE_OK = 0
CODE_SPA_CLIENT_OLD = -100
CODE_TO_BE_AUTHENTICATED = -1
CODE_ACCESS_VIOLATION = -2
CODE_SSO_VIOLATED = 108
CODE_ACCOUNT_NOT_EXIST = 100
CODE_INACTIVE_ACCOUNT = 101
CODE_NEED_PASS_QUIZ = 138
CODE_REQUIRE_PAYMENT = 141
CODE_REQUIRE_SIGN_WAIVERS = 142
CODE_REQUIRE_ACCEPT_TERM = 160
CODE_NEED_REDIRECT_TO_PAYMENT = 162
#: Undocumented, observed live: RegisterEventBO refuses an event whose
#: registration window has not opened yet. Expected, and the whole point
#: of firing early -- so it must never be treated as fatal.
CODE_REGISTRATION_NOT_OPEN = 147
CODE_RECAPTCHA_VALIDATION_FAIL = 157
CODE_NOT_SESSION_MEMBER = 185
CODE_REQUIRE_SIGNED_WAIVER = 203
#: Undocumented, observed live: returned by RegisterEventBO.Initialize
#: for a court the account already holds a reservation on. It arrives as
#: data.code with the envelope code left at 0, which is why an earlier
#: version reported a bare "(code 0)".
CODE_ALREADY_HOLDS_COURT = 205
CODE_NOT_MOST_RECENT_WAIVER = 210
CODE_ARCHIVED_FITNESS_EVENT = 212
CODE_NEED_GENERAL_MEMBERSHIP = 220
CODE_MEMBER_SUSPENDED = 300
CODE_MEMBER_INELIGIBLE = 301
CODE_CHECK_REQUIREMENTS_TIPS = 302
CODE_SID_IN_USE = 400
CODE_SID_PENDING_REQUEST = 401
CODE_SID_VERIFY_FAILED = 402
CODE_SID_NOT_MATCH = 404
CODE_NETWORK_DISABLED = 800

#: Session is dead -> stop and re-authenticate. Never retry through these.
AUTH_CODES = frozenset({CODE_TO_BE_AUTHENTICATED, CODE_ACCESS_VIOLATION, CODE_SSO_VIOLATED})

#: The one refusal we expect and want to retry through.
RETRYABLE_CODES = frozenset({CODE_REGISTRATION_NOT_OPEN})

#: Definitive "this will never succeed by retrying" outcomes. Seeing one
#: of these means: give up on this court and move to the next candidate,
#: or abort the target entirely.
FATAL_CODES = frozenset({
    CODE_ALREADY_HOLDS_COURT,
    CODE_MEMBER_SUSPENDED,
    CODE_MEMBER_INELIGIBLE,
    CODE_CHECK_REQUIREMENTS_TIPS,
    CODE_ARCHIVED_FITNESS_EVENT,
    CODE_NEED_GENERAL_MEMBERSHIP,
    CODE_REQUIRE_PAYMENT,
    CODE_NEED_REDIRECT_TO_PAYMENT,
    CODE_REQUIRE_SIGN_WAIVERS,
    CODE_REQUIRE_SIGNED_WAIVER,
    CODE_NOT_MOST_RECENT_WAIVER,
    CODE_NEED_PASS_QUIZ,
    CODE_SID_IN_USE,
    CODE_SID_PENDING_REQUEST,
    CODE_SID_VERIFY_FAILED,
    CODE_SID_NOT_MATCH,
    CODE_NETWORK_DISABLED,
    CODE_RECAPTCHA_VALIDATION_FAIL,
})

# --- Slot status ------------------------------------------------------
# Observed empirically across past / open / future days.

STATUS_NOT_YET_OPEN = 0
STATUS_OPEN = 1
STATUS_FULL = 2
STATUS_CLOSED = 4

STATUS_LABELS = {
    STATUS_NOT_YET_OPEN: "not yet open",
    STATUS_OPEN: "open",
    STATUS_FULL: "full",
    STATUS_CLOSED: "closed",
}

# --- Reservation rules (UTD policy, supplied by the account holder) ----

#: PER DAY. Verified live 2026-09-23: an account held four reservations
#: across two days without complaint, and a court held on one day did
#: not block that court on another.
MAX_RESERVATIONS = 3
#: The account may not hold two reservations on the same court ON THE
#: SAME DAY, even at different times -- and a finished slot still counts
#: for the rest of that day. Other days are independent. This is what
#: drives the fallback logic.
DISTINCT_COURTS_REQUIRED = True

#: Registration opens exactly this long before a slot starts. Verified
#: across many slots; we still read openRegistrationDateString from the
#: API rather than computing it, and use this only as a sanity check.
REGISTRATION_LEAD = 24 * 60 * 60
