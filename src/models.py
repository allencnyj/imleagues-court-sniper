"""Plain data types shared across the app."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date, datetime, time as _time
from enum import Enum
from typing import Any

import constants as C


def _parse_stamp(text: str | None) -> datetime | None:
    """Parse IMLeagues' "09/01/2026 @ 01:00 PM" into an aware datetime.

    The API returns these with no zone attached; they are always in the
    school's local time.
    """
    if not text:
        return None
    try:
        naive = datetime.strptime(text.strip(), "%m/%d/%Y @ %I:%M %p")
    except ValueError:
        return None
    return naive.replace(tzinfo=C.SCHOOL_TZ)


def _parse_day(text: str | None) -> _date | None:
    """Parse either day format the API uses.

    startDateStr is "Tuesday, September 01, 2026"; endDateString is
    "09/02/2026".
    """
    if not text:
        return None
    for fmt in ("%A, %B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _combine(day: _date | None, clock: str, meridiem: str = "") -> datetime | None:
    """Attach a "01:00"+"PM" or "01:30 PM" wall time to a date."""
    if day is None or not clock:
        return None
    stamp = f"{clock.strip()} {meridiem.strip()}".strip()
    try:
        wall = datetime.strptime(stamp, "%I:%M %p").time()
    except ValueError:
        return None
    return datetime.combine(day, wall, tzinfo=C.SCHOOL_TZ)


@dataclass(frozen=True)
class Slot:
    """One bookable court-hour, as returned by SearchScheduledFitnessSessions."""

    event_id: int
    court: int
    name: str
    where: str
    start: datetime | None
    end: datetime | None
    opens: datetime | None
    closes: datetime | None
    status_code: int
    show_signup: bool
    current_spots: int
    max_spots: int
    is_registered: bool
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, ev: dict[str, Any]) -> "Slot | None":
        """Build a Slot, or None if this event isn't a badminton court."""
        court = C.court_from_name(ev.get("eventName", ""))
        if court is None:
            return None
        # startDateStr is the authoritative start day. endDateString rolls
        # over to the next day for slots that cross midnight (11:30 PM),
        # so it must never be used to date the start.
        start_day = _parse_day(ev.get("startDateStr")) or _parse_day(ev.get("endDateString"))
        end_day = _parse_day(ev.get("endDateString")) or start_day
        return cls(
            event_id=int(ev["eventId"]),
            court=court,
            name=ev.get("eventName", ""),
            where=ev.get("where", ""),
            start=_combine(start_day, ev.get("startTime", ""), ev.get("startTimeAbbr", "")),
            end=_combine(end_day, ev.get("endTimeString", "")),
            opens=_parse_stamp(ev.get("openRegistrationDateString")),
            closes=_parse_stamp(ev.get("endRegistrationDateString")),
            status_code=int(ev.get("statusCode", -1)),
            show_signup=bool(ev.get("showSignupBtn")),
            current_spots=int(ev.get("currentSpotCount", 0)),
            max_spots=int(ev.get("maxSpotCount", 0)),
            is_registered=bool(ev.get("isRegistered")),
            raw=ev,
        )

    @property
    def slot_date(self) -> _date | None:
        return self.start.date() if self.start else None

    @property
    def slot_time(self) -> _time | None:
        return self.start.timetz().replace(tzinfo=None) if self.start else None

    @property
    def time_label(self) -> str:
        return self.start.strftime("%I:%M %p").lstrip("0") if self.start else "?"

    @property
    def status_label(self) -> str:
        return C.STATUS_LABELS.get(self.status_code, f"code {self.status_code}")

    @property
    def is_full(self) -> bool:
        return self.max_spots > 0 and self.current_spots >= self.max_spots


class TargetState(str, Enum):
    IDLE = "Idle"
    ARMED = "Armed"
    FIRING = "Firing"
    WON = "Won"
    LOST = "Lost"
    ERROR = "Error"
    SKIPPED = "Skipped"
    #: A dry run reached this court and stopped before submitting. It is
    #: deliberately NOT "Won": nothing was sent, so whether the booking
    #: would have succeeded is unknown.
    SIMULATED = "Dry run"
    #: The request succeeded but landed on the waitlist, not a confirmed
    #: spot. We notify and stop; the user decides whether to keep it.
    WAITLISTED = "Waitlisted"
    #: Every court this target may take is one the account already holds
    #: (IMLeagues refuses with 205). A watcher keeps asking the server
    #: and books the moment one frees up.
    WAITING = "Waiting"


@dataclass
class Target:
    """One slot the user wants: a date, a start time, a preferred court."""

    date: _date
    start_time: _time
    preferred_court: int
    allow_fallback: bool = True
    #: Which account books this. None means "auto" -- let the app pick a
    #: signed-in account that can still legally take the court.
    account_id: str | None = None
    #: When set (by the planner), the ONLY courts fallback may try, in
    #: order. None means the legacy rule: any court this account does
    #: not already hold. The planner narrows it so two of our own
    #: accounts never chase the same court at the same time.
    fallback_courts: list[int] | None = None
    #: Which planner Request this target serves, if any.
    request_index: int | None = None
    #: When a court is refused only because THIS account already holds
    #: it, keep watching that court until registration closes and book
    #: it the instant the old reservation lets go (it lapsed, or the
    #: user unreserved it). Off by default so low-level callers get the
    #: plain skip/lose behaviour; the app and planner turn it on.
    wait_for_free: bool = False

    # Runtime state, mutated by the scheduler.
    state: TargetState = TargetState.IDLE
    won_court: int | None = None
    message: str = ""
    fire_at: datetime | None = None
    #: Set once booked (or waitlisted), for the Unreserve action.
    event_id: int | None = None
    event_member_id: int | None = None

    @property
    def label(self) -> str:
        t = self.start_time.strftime("%I:%M %p").lstrip("0")
        return f"{self.date:%a %b %d} {t} \u00b7 Ct {self.preferred_court}"

    def reset(self) -> None:
        self.state = TargetState.IDLE
        self.won_court = None
        self.message = ""
        self.fire_at = None
        self.event_id = None
        self.event_member_id = None

    def to_json(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "start_time": self.start_time.strftime("%H:%M"),
            "preferred_court": self.preferred_court,
            "allow_fallback": self.allow_fallback,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Target | None":
        try:
            return cls(
                date=_date.fromisoformat(str(data["date"])),
                start_time=datetime.strptime(str(data["start_time"]), "%H:%M").time(),
                preferred_court=int(data["preferred_court"]),
                allow_fallback=bool(data.get("allow_fallback", True)),
            )
        except (KeyError, ValueError, TypeError):
            return None


@dataclass(frozen=True)
class FireResult:
    """Outcome of one RegisterSession attempt."""

    ok: bool
    code: int
    message: str
    court: int
    event_id: int
    elapsed_ms: float
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def is_auth_failure(self) -> bool:
        return self.code in C.AUTH_CODES

    @property
    def is_fatal(self) -> bool:
        """True when retrying this exact request cannot possibly help."""
        return self.code in C.FATAL_CODES
