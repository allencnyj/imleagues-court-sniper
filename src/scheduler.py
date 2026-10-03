"""Deciding what to book, and firing at the right instant.

The pure decision functions at the top are the interesting part and are
covered by tests. The Sniper class underneath is the plumbing that runs
them against the live site on a background thread.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date as _date, datetime, time as _time, timedelta
from typing import Any, Callable, Iterable, Sequence

import requests

import constants as C
from imleagues_api import AuthError, ImLeaguesClient
from models import FireResult, Slot, Target, TargetState
from payload import Consent, build_payload, read_requirements, retarget
from timing import (ClockSync, detect_suspend, format_countdown, sleep_until_perf,
                    sleep_until_wall, sync_clock)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Pure decision logic
# ---------------------------------------------------------------------

def held_courts(slots: Iterable[Slot], day: _date | None = None, *,
                now: datetime | None = None) -> set[int]:
    """Courts the account holds ON `day`, from is_registered flags.

    The rule, established live on 2026-09-23 with three accounts and a
    dozen read-only probes, is PER DAY: an account may not hold the same
    court twice on one calendar day -- and a finished slot still counts
    for the rest of that day -- but holdings on other days do not
    interact at all. (An account holding Tuesday's Court 2 booked
    Wednesday's Court 2 fine; one holding Wednesday's Court 3, whose slot
    had already ended, was still refused Court 3 for Wednesday.) An
    earlier reading that any held court blocked that court on every day
    was wrong, and cost a booking.

    With no `day`, returns the union over every day (legacy callers).
    `now` is accepted and ignored: within a day, elapsed time does not
    free a court.
    """
    return {slot.court for slot in slots
            if slot.is_registered and (day is None or slot.slot_date == day)}


def held_by_day(slots: Iterable[Slot]) -> dict[_date, set[int]]:
    """Every held court, keyed by the day it is held on."""
    out: dict[_date, set[int]] = {}
    for slot in slots:
        if slot.is_registered and slot.slot_date is not None:
            out.setdefault(slot.slot_date, set()).add(slot.court)
    return out


def candidate_courts(
    preferred: int,
    held: Iterable[int],
    *,
    allow_fallback: bool = True,
    courts: Sequence[int] = C.COURTS,
) -> list[int]:
    """Which courts to try, in order, for one target.

    UTD allows three reservations per account and no two of them may be
    on the same court. So a court we already hold is not a fallback
    option -- booking it would be rejected, or worse, accepted and burn a
    slot we cannot use.
    """
    held_set = set(held)
    if len(held_set) >= C.MAX_RESERVATIONS:
        return []

    available = [c for c in courts if c not in held_set]
    if not allow_fallback:
        return [preferred] if preferred in available else []

    if preferred in available:
        return [preferred] + [c for c in available if c != preferred]
    return available


def find_slot(slots: Iterable[Slot], day: _date, start: _time, court: int) -> Slot | None:
    """Locate the exact event for a date/time/court."""
    for slot in slots:
        if (slot.court == court and slot.slot_date == day
                and slot.slot_time is not None
                and slot.slot_time.hour == start.hour
                and slot.slot_time.minute == start.minute):
            return slot
    return None


def slots_for(slots: Iterable[Slot], day: _date, start: _time) -> dict[int, Slot]:
    """All courts available at one date/time, keyed by court number."""
    out: dict[int, Slot] = {}
    for slot in slots:
        if (slot.slot_date == day and slot.slot_time is not None
                and slot.slot_time.hour == start.hour
                and slot.slot_time.minute == start.minute):
            out[slot.court] = slot
    return out


def should_keep_retrying(result: FireResult) -> bool:
    """Is another attempt at this same court worth making?

    Yes only for the transient case: the registration window has not
    opened yet, which is exactly what we expect when firing early.
    """
    if result.ok or result.is_fatal or result.is_auth_failure:
        return False
    return True


# ---------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------

@dataclass
class FireSettings:
    """Timing knobs, all user-visible."""

    #: Seconds relative to the published open time. Negative fires early.
    #: Default is deliberately early: the server farm's clocks disagree by
    #: up to a second (see timing.FARM_CLOCK_SKEW), and an early shot just
    #: gets a harmless "not open yet" while a late one loses the court.
    offset_s: float = -1.0
    #: Gap between retries once firing has begun.
    retry_interval_s: float = 0.2
    #: How long to keep retrying before giving up on a target.
    retry_window_s: float = 15.0
    #: Re-sync the clock this long before each fire.
    resync_lead_s: float = 60.0
    #: Warm the TCP/TLS connection this long before each fire.
    prewarm_lead_s: float = 5.0
    #: How often a WAITING target re-asks the server whether the court
    #: it needs has freed up. One light read-only call per court.
    wait_interval_s: float = 60.0

    def to_json(self) -> dict[str, Any]:
        return {
            "offset_s": self.offset_s,
            "retry_interval_s": self.retry_interval_s,
            "retry_window_s": self.retry_window_s,
            "resync_lead_s": self.resync_lead_s,
            "prewarm_lead_s": self.prewarm_lead_s,
            "wait_interval_s": self.wait_interval_s,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> "FireSettings":
        data = data or {}
        base = cls()
        return cls(
            offset_s=float(data.get("offset_s", base.offset_s)),
            retry_interval_s=float(data.get("retry_interval_s", base.retry_interval_s)),
            retry_window_s=float(data.get("retry_window_s", base.retry_window_s)),
            resync_lead_s=float(data.get("resync_lead_s", base.resync_lead_s)),
            prewarm_lead_s=float(data.get("prewarm_lead_s", base.prewarm_lead_s)),
            wait_interval_s=float(data.get("wait_interval_s", base.wait_interval_s)),
        )


@dataclass
class Attempt:
    """One shot at one court, for the log."""

    court: int
    event_id: int
    at: datetime
    result: FireResult | None
    note: str = ""


@dataclass
class TargetOutcome:
    target: Target
    won_court: int | None = None
    attempts: list[Attempt] = field(default_factory=list)
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.won_court is not None


# ---------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------

Notify = Callable[[str, str], None]  # (level, message)


class Sniper:
    """Runs the armed queue on a background thread."""

    def __init__(
        self,
        client: ImLeaguesClient,
        settings: FireSettings,
        consent: Consent,
        *,
        notify: Notify | None = None,
        reauth: Callable[[], bool] | None = None,
        form_model: dict[str, Any] | None = None,
        on_form_model: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.consent = consent
        self._notify = notify or (lambda level, msg: None)
        self._reauth = reauth
        #: Cached registration form, so a fire needs no Initialize.
        self.form_model = form_model or {}
        self.on_form_model = on_form_model or (lambda _vm: None)
        self.clock: ClockSync | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.dry_run = False
        self.targets: list[Target] = []
        self.outcomes: list[TargetOutcome] = []
        #: One thread per WAITING target, so a court being waited out for
        #: hours never delays this account's other, later targets.
        self._watchers: list[threading.Thread] = []

    # --- lifecycle ----------------------------------------------------

    @property
    def running(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        return any(w.is_alive() for w in self._watchers)

    def arm(self, targets: Sequence[Target], *, dry_run: bool = False) -> None:
        if self.running:
            raise RuntimeError("Already armed")
        self.targets = list(targets)
        self.outcomes = []
        self._watchers = []
        self.dry_run = dry_run
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sniper", daemon=True)
        self._thread.start()

    def disarm(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        for w in self._watchers:
            if w.is_alive():
                w.join(timeout=5.0)
        self._watchers = []

    def _say(self, level: str, msg: str) -> None:
        log.log(logging.WARNING if level == "warn" else logging.INFO, msg)
        self._notify(level, msg)

    # --- main loop ----------------------------------------------------

    def _run(self) -> None:
        try:
            self._resync("Initial clock sync")
            pending = [t for t in self.targets]
            for target in pending:
                target.state = TargetState.ARMED

            while pending and not self._stop.is_set():
                self._plan(pending)
                ready = [t for t in pending if t.fire_at is not None]
                if not ready:
                    self._say("warn", "No target could be matched to a real slot; stopping.")
                    break
                ready.sort(key=lambda t: t.fire_at)  # type: ignore[arg-type]
                target = ready[0]
                self._await_and_fire(target)
                pending.remove(target)

            if not self._stop.is_set():
                won = [o for o in self.outcomes if o.ok]
                waiting = [t for t in self.targets if t.state is TargetState.WAITING]
                msg = f"Finished firing. {len(won)} of {len(self.outcomes)} target(s) booked."
                if waiting:
                    msg += (f" {len(waiting)} still WAITING for a held court to free up - "
                            "it books the moment that happens.")
                self._say("info", msg)
        except Exception as exc:  # noqa: BLE001 - a crashed thread must be visible
            log.exception("sniper thread failed")
            self._say("error", f"Stopped on an unexpected error: {exc}")

    # --- planning -----------------------------------------------------

    #: A PC clock this far from the server's is treated as broken.
    CLOCK_SKEW_ALARM_S = 30.0

    def _resync(self, why: str) -> None:
        try:
            self.clock = sync_clock(self.client.session)
            self._say("info", f"{why}: {self.clock.describe()}")
        except (RuntimeError, requests.RequestException) as exc:
            self._say("warn", f"{why} failed ({exc}); using local clock.")
            if self.clock is None:
                from timing import local_only
                self.clock = local_only()
            return
        # The fire itself is timed against the server, but the long wait
        # and the "has this slot closed yet" checks would otherwise trust
        # Windows. This machine was once found 20 days behind (a lost
        # CMOS clock, corrected by NTP hours later): say so out loud
        # rather than silently planning around a fantasy date.
        skew = self._skew().total_seconds()
        if abs(skew) > self.CLOCK_SKEW_ALARM_S:
            self._say("warn",
                      f"Your PC clock is {format_countdown(abs(skew))} "
                      f"{'behind' if skew > 0 else 'ahead of'} IMLeagues. Timing is "
                      "corrected against the server, but fix Windows time sync.")

    def _skew(self) -> timedelta:
        """server time minus local wall time (zero without a sync)."""
        if self.clock is None:
            return timedelta(0)
        return self.clock.server_now() - datetime.now(C.SCHOOL_TZ)

    def _now(self) -> datetime:
        """Best estimate of the real current time, in school time."""
        return datetime.now(C.SCHOOL_TZ) + self._skew()

    def _local_for(self, when: datetime) -> datetime:
        """The local wall-clock moment corresponding to server time `when`."""
        return when - self._skew()

    def _ensure_signed_in(self, target: Target) -> bool:
        """Confirm the session is live just before firing; re-mint if not.

        Returns True if the session is good to fire on. The whoami() call
        also counts as activity, so on a healthy session this doubles as
        a final keep-alive touch.
        """
        try:
            if self.client.whoami() is not None:
                return True
        except (AuthError, requests.RequestException):
            pass

        self._say("warn", f"{target.label}: session expired before firing; "
                          "restoring it now.")
        if self._reauth and self._reauth() and self.client.whoami() is not None:
            self._say("success", f"{target.label}: session restored, still on track.")
            return True

        target.state = TargetState.ERROR
        target.message = ("Session could not be restored in time; sign in and "
                          "re-arm this target.")
        self._say("error", f"{target.label}: {target.message}")
        return False

    def _load_slots(self, days_ahead: int = 8) -> list[Slot]:
        today = datetime.now(C.SCHOOL_TZ).date()
        return self.client.search_slots(today, today + timedelta(days=days_ahead))

    def _plan(self, pending: Sequence[Target]) -> None:
        """Resolve each pending target to a real slot and fire time."""
        try:
            slots = self._load_slots()
        except (AuthError, RuntimeError, requests.RequestException) as exc:
            self._say("warn", f"Could not refresh the calendar: {exc}")
            return
        now = self._now()
        for target in pending:
            slot = find_slot(slots, target.date, target.start_time, target.preferred_court)
            if slot is None:
                # Fall back to any court at that time -- they share an
                # open time, so the schedule still resolves.
                same_time = slots_for(slots, target.date, target.start_time)
                slot = next(iter(same_time.values()), None)
            if slot is None or slot.opens is None:
                target.state = TargetState.SKIPPED
                target.message = "No such slot on the calendar"
                target.fire_at = None
                continue

            # A slot whose registration window has shut can never be
            # booked, so firing at it would only waste the retry window.
            if slot.closes is not None and now >= slot.closes:
                target.state = TargetState.SKIPPED
                target.message = f"Registration closed {slot.closes:%a %b %d %I:%M %p}"
                target.fire_at = None
                self._say("warn", f"{target.label}: {target.message}")
                continue
            if slot.start is not None and now >= slot.start:
                target.state = TargetState.SKIPPED
                target.message = "Slot has already started"
                target.fire_at = None
                self._say("warn", f"{target.label}: {target.message}")
                continue

            target.fire_at = slot.opens + timedelta(seconds=self.settings.offset_s)

    # --- firing -------------------------------------------------------

    def _await_and_fire(self, target: Target) -> None:
        assert self.clock is not None and target.fire_at is not None
        opens = target.fire_at - timedelta(seconds=self.settings.offset_s)

        # The long wait runs on the system clock, not perf_counter: a
        # queued booking can be a day out, and perf_counter stops while
        # Windows is suspended. A laptop that slept overnight would wake
        # believing it still had hours to go.
        resync_at = target.fire_at - timedelta(seconds=self.settings.resync_lead_s)
        if resync_at > self._now():
            self._say("info",
                      f"{target.label}: waiting for {opens:%a %b %d %I:%M %p} "
                      f"(fires {self.settings.offset_s:+.2f}s from open)")
            mark_perf, mark_wall = time.perf_counter(), time.time()
            # Translated to local wall time, so a wrong PC clock still
            # wakes us at the right server moment.
            if sleep_until_wall(self._local_for(resync_at), should_abort=self._stop.is_set):
                return
            slept = detect_suspend(mark_perf, mark_wall)
            if slept:
                self._say("warn",
                          f"{target.label}: machine was suspended for about "
                          f"{format_countdown(slept)}; re-syncing the clock.")
            self._resync(f"{target.label}: pre-fire sync")

        # Verify the session is still live, and re-mint it if not, while
        # there is still time. The session has an absolute cap the idle
        # touch cannot slide (observed ~3.75h live), so a booking queued
        # hours ahead can arrive at a dead session. Catch that here, with
        # ~60s of slack, rather than discovering it in the fire itself.
        if not self._ensure_signed_in(target):
            return

        # Refresh the registration template while there is still time.
        template, requirements = self._prepare_payload(target)
        if template is None:
            return

        prewarm_at = self.clock.perf_for(
            target.fire_at - timedelta(seconds=self.settings.prewarm_lead_s))
        if prewarm_at > time.perf_counter():
            if self._sleep_or_stop(prewarm_at):
                return
        rtt = self.client.prewarm()
        if rtt is not None:
            self._say("info", f"{target.label}: connection warm ({rtt:.0f}ms)")

        fire_perf = self.clock.perf_for(target.fire_at)
        late_by = time.perf_counter() - fire_perf
        overshoot = sleep_until_perf(fire_perf, should_abort=self._stop.is_set)
        if self._stop.is_set():
            return

        if late_by > 5.0:
            # Registration opened a while ago and the slot is still live,
            # so go straight at it. Reporting this as "wait overshoot"
            # would print a meaningless number of hours.
            self._say("info",
                      f"{target.label}: registration already open "
                      f"({format_countdown(late_by)} ago) - firing immediately")
        else:
            self._say("info",
                      f"{target.label}: firing now (t{self.settings.offset_s:+.2f}s, "
                      f"landed {overshoot * 1000:+.1f}ms from target)")
        target.state = TargetState.FIRING
        outcome = self._fire_sequence(target, template)
        self.outcomes.append(outcome)

    def _sleep_or_stop(self, target_perf: float) -> bool:
        """Wait until target_perf. Returns True if we were told to stop."""
        sleep_until_perf(target_perf, should_abort=self._stop.is_set)
        return self._stop.is_set()

    def _prepare_payload(self, target: Target) -> tuple[dict[str, Any] | None, Any]:
        """Fetch the registration form model and build the request body.

        Uses whichever event at this time is already open if the target
        itself is not, since the fields are account-level.
        """
        try:
            slots = self._load_slots()
        except (AuthError, RuntimeError, requests.RequestException) as exc:
            target.state = TargetState.ERROR
            target.message = str(exc)
            self._say("error", f"{target.label}: {exc}")
            return None, None

        at_time = slots_for(slots, target.date, target.start_time)
        if not at_time:
            target.state = TargetState.SKIPPED
            target.message = "Slot vanished from the calendar"
            self._say("warn", f"{target.label}: {target.message}")
            return None, None

        # At fire time the target's own slots have NOT opened yet, so
        # Initialize refuses them with code 147. That used to abort the
        # whole target a minute before it would have fired.
        #
        # The registration form is account-level, so a model captured
        # from ANY event builds a valid booking for any other. Prefer a
        # fresh one from a currently-open slot; fall back to the cached
        # one; only give up if we have neither.
        held = held_courts(slots, target.date) | self._won_courts(target.date)

        # Nothing to do if every court is spoken for -- decide that before
        # bothering with a form. Unless the target is willing to wait for
        # one of our own courts to free up, in which case we still need
        # the form (from the cache: nothing is readable live) and let the
        # fire itself discover which courts the server refuses.
        if not target.wait_for_free and not candidate_courts(
                target.preferred_court, held, allow_fallback=target.allow_fallback):
            target.state = TargetState.SKIPPED
            target.message = (f"No bookable court left (already holding "
                              f"{sorted(held) or 'none'})")
            self._say("warn", f"{target.label}: {target.message}")
            return None, None

        vm: dict[str, Any] | None = None
        by_day = held_by_day(slots)
        probe_ids = [s.event_id for s in slots
                     if s.status_code == C.STATUS_OPEN
                     and s.court not in by_day.get(s.slot_date, set()) and not s.is_full]
        for probe_id in probe_ids[:3]:
            try:
                vm = self.client.init_register(probe_id)
                self.on_form_model(vm)
                break
            except AuthError:
                if not (self._reauth and self._reauth()):
                    target.state = TargetState.ERROR
                    target.message = "Not signed in"
                    self._say("error", f"{target.label}: not signed in; cannot book.")
                    return None, None
            except (RuntimeError, requests.RequestException) as exc:
                log.info("Initialize refused event %s (%s)", probe_id, exc)

        if not vm and self.form_model:
            vm = dict(self.form_model)
            self._say("info", f"{target.label}: using the saved booking form "
                              "(nothing is open to read a fresh one from)")

        if not vm:
            target.state = TargetState.ERROR
            target.message = ("No booking form available. Open the app while a "
                              "court is bookable and click Waiver once, so the "
                              "form can be saved for later.")
            self._say("error", f"{target.label}: {target.message}")
            return None, None

        reqs = read_requirements(vm)
        if reqs.blockers:
            target.state = TargetState.ERROR
            target.message = "; ".join(reqs.blockers)
            self._say("error", f"{target.label}: cannot book unattended - {target.message}")
            return None, None

        missing = []
        if reqs.waiver_shown and not self.consent.accept_terms:
            missing.append("waiver not accepted in setup")
        if reqs.needs_signature and not self.consent.digital_signature:
            missing.append("digital signature not set in setup")
        if missing:
            target.state = TargetState.ERROR
            target.message = "; ".join(missing)
            self._say("error", f"{target.label}: {target.message}")
            return None, None

        first = next(iter(at_time.values()))
        return build_payload(vm, first.event_id, self.consent), reqs

    def _won_courts(self, day: _date) -> set[int]:
        """Courts won earlier in this run on `day` (the rule is per day)."""
        return {o.won_court for o in self.outcomes
                if o.won_court and o.target.date == day}

    @staticmethod
    def _allowed_courts(target: Target) -> list[int]:
        """Every court this target may ever take, preferred first."""
        if not target.allow_fallback:
            return [target.preferred_court]
        if target.fallback_courts is not None:
            # Planner-made target: fallback is confined to courts the plan
            # left free for this account, so our own accounts never race
            # each other for one spot.
            return [target.preferred_court] + list(target.fallback_courts)
        return [target.preferred_court] + [c for c in C.COURTS if c != target.preferred_court]

    def _record_success(self, target: Target, outcome: TargetOutcome, slot: Slot,
                        court: int, result: FireResult) -> None:
        """Classify an accepted booking: confirmed, or only waitlisted."""
        target.event_id = slot.event_id
        # The request succeeded, but a contested slot may have put us on
        # the waitlist rather than a confirmed spot. Classify so a
        # waitlist is never reported as a win.
        info = self.client.reservation_info(slot.event_id)
        target.event_member_id = info.get("event_member_id")
        if info.get("waitlisted"):
            target.state = TargetState.WAITLISTED
            target.won_court = None
            target.message = f"Waitlisted for court {court}"
            outcome.message = target.message
            self._say("warn",
                      f"{target.label}: WAITLISTED for court {court} "
                      "(not a confirmed spot). Left as-is - unreserve it "
                      "from Reservations if you don't want it.")
            return
        target.state = TargetState.WON
        target.won_court = court
        target.message = f"Booked court {court}"
        outcome.won_court = court
        self._say("success",
                  f"{target.label}: BOOKED court {court} "
                  f"({result.elapsed_ms:.0f}ms, {len(outcome.attempts)} attempt(s))")

    def _fire_sequence(self, target: Target, template: dict[str, Any]) -> TargetOutcome:
        """Try each candidate court in order until one lands.

        A court refused with 205 ("you already hold this court") is not
        necessarily lost: if the target allows it, a watcher keeps asking
        the server about that court until the old reservation lets go --
        because it lapsed, or because the user unreserved it -- and books
        the instant it does.
        """
        outcome = TargetOutcome(target=target)
        deadline = time.perf_counter() + self.settings.retry_window_s

        try:
            slots = self._load_slots()
        except Exception:  # noqa: BLE001 - fall back to firing blind
            slots = []
        already = held_courts(slots, target.date) if slots else set()
        already |= self._won_courts(target.date)

        at_time = slots_for(slots, target.date, target.start_time) if slots else {}
        allowed = self._allowed_courts(target)
        free = [c for c in candidate_courts(target.preferred_court, already,
                                            allow_fallback=target.allow_fallback)
                if c in allowed]
        # Courts we appear to hold ourselves. A waiting target still takes
        # one shot at them: our "held" reading comes from is_registered
        # flags, and if the server has in fact let a court go, the shot
        # simply lands.
        held_too = [c for c in allowed if c not in free] if target.wait_for_free else []
        order = free + held_too
        if not order:
            target.state = TargetState.SKIPPED
            reason = (f"all three courts are already reserved on {target.date:%a %b %d}"
                      if len(already) >= C.MAX_RESERVATIONS
                      else f"court {target.preferred_court} is already held that day")
            target.message = reason
            outcome.message = reason
            self._say("warn", f"{target.label}: skipped - {reason}")
            return outcome

        self._say("info", f"{target.label}: court order {order} "
                          f"(already held: {sorted(already) or 'none'})")
        #: Courts the server refused because this account holds them.
        blocked: list[int] = []

        # Courts are tried round-robin, not one-until-exhausted.
        #
        # A non-fatal rejection is ambiguous: it means either "not open
        # yet" (which affects every court equally, so retrying is right)
        # or "someone beat you to it" (where the answer is to move on).
        # Cycling handles both without having to tell them apart -- the
        # fallback court gets a shot within one cycle, roughly 150ms,
        # instead of only after the preferred court's retries ran out.
        rotation: list[int] = []
        for court in order:
            if court in at_time:
                rotation.append(court)
            else:
                outcome.attempts.append(Attempt(court, 0, datetime.now(C.SCHOOL_TZ), None,
                                                "no event id for this court"))
                self._say("warn", f"{target.label}: no event id for court {court}; skipping it")

        while rotation and time.perf_counter() < deadline and not self._stop.is_set():
            for court in list(rotation):
                if self._stop.is_set():
                    break
                slot = at_time[court]

                if self.dry_run:
                    self._say("info",
                              f"{target.label}: DRY RUN - would POST RegisterSession "
                              f"for court {court} (event {slot.event_id}); "
                              f"remaining order "
                              f"{rotation[rotation.index(court) + 1:] or 'none'}")
                    # Not WON: nothing was sent, so whether it would have
                    # been accepted is genuinely unknown.
                    target.state = TargetState.SIMULATED
                    target.won_court = None
                    target.message = f"would try court {court} first"
                    outcome.message = f"Dry run - would try court {court}"
                    return outcome

                body = retarget(template, slot.event_id)
                try:
                    result = self.client.register_session(body, court=court)
                except AuthError:
                    if self._reauth and self._reauth():
                        continue
                    target.state = TargetState.ERROR
                    target.message = "Session expired mid-fire"
                    self._say("error", f"{target.label}: {target.message}")
                    return outcome
                except requests.RequestException as exc:
                    outcome.attempts.append(Attempt(court, slot.event_id,
                                                    datetime.now(C.SCHOOL_TZ), None, str(exc)))
                    continue

                outcome.attempts.append(Attempt(court, slot.event_id,
                                                datetime.now(C.SCHOOL_TZ), result))
                if result.ok:
                    self._record_success(target, outcome, slot, court, result)
                    return outcome

                if not should_keep_retrying(result):
                    if (result.code == C.CODE_ALREADY_HOLDS_COURT
                            and target.wait_for_free):
                        self._say("info",
                                  f"{target.label}: court {court} is held by this "
                                  "account; will keep watching it")
                        blocked.append(court)
                    else:
                        self._say("warn",
                                  f"{target.label}: court {court} is out - "
                                  f"{result.message or 'code %d' % result.code}")
                    rotation.remove(court)

            if rotation and time.perf_counter() < deadline:
                time.sleep(self.settings.retry_interval_s)

        if blocked and not self._stop.is_set():
            self._start_watch(target, outcome, blocked, at_time)
            return outcome

        target.state = TargetState.LOST
        last = outcome.attempts[-1] if outcome.attempts else None
        target.message = (last.result.message if last and last.result
                          else "No court could be booked")
        outcome.message = target.message
        self._say("warn", f"{target.label}: lost - {target.message}")
        return outcome

    # --- waiting for one of our own courts to free up ----------------

    def _start_watch(self, target: Target, outcome: TargetOutcome,
                     blocked: list[int], at_time: dict[int, Slot]) -> None:
        closes = [at_time[c].closes for c in blocked if c in at_time and at_time[c].closes]
        until = min(closes) if closes else None
        target.state = TargetState.WAITING
        target.message = (f"court(s) {blocked} held by this account; retrying every "
                          f"{self.settings.wait_interval_s:.0f}s"
                          + (f" until {until:%a %I:%M %p}" if until else ""))
        outcome.message = target.message
        self._say("warn", f"{target.label}: WAITING - {target.message}. It books the "
                          "moment the old reservation lets go (unreserve it, or let "
                          "it lapse).")
        watcher = threading.Thread(target=self._watch, args=(target, outcome, blocked),
                                   name="court-watch", daemon=True)
        with self._lock:
            self._watchers.append(watcher)
        watcher.start()

    def _other_fire_imminent(self, target: Target, within_s: float = 30.0) -> bool:
        """Never let a watch probe collide with another target's shot."""
        if self.clock is None:
            return False
        for t in self.targets:
            if t is target or not t.fire_at:
                continue
            if t.state in (TargetState.ARMED, TargetState.FIRING) \
                    and self.clock.seconds_until(t.fire_at) < within_s:
                return True
        return False

    def _watch(self, target: Target, outcome: TargetOutcome, blocked: list[int]) -> None:
        """Poll the server until a blocked court is bookable, then book it.

        The truth about whether a court has freed lives only on the
        server: is_registered flags linger on past reservations, so we
        ask with Initialize (read-only) and fire as soon as it stops
        answering 205. Stops when registration closes, the slot fills, or
        the sniper is disarmed.
        """
        try:
            while not self._stop.wait(self.settings.wait_interval_s):
                if self._other_fire_imminent(target):
                    continue
                try:
                    slots = self._load_slots()
                except AuthError:
                    if not (self._reauth and self._reauth()):
                        continue      # the keeper may restore it; try again later
                    continue
                except (RuntimeError, requests.RequestException) as exc:
                    log.info("%s: watch could not read the calendar (%s)", target.label, exc)
                    continue
                at_time = slots_for(slots, target.date, target.start_time)
                now = self._now()

                for court in list(blocked):
                    slot = at_time.get(court)
                    gone = None
                    if slot is None:
                        gone = "slot vanished from the calendar"
                    elif slot.closes is not None and now >= slot.closes:
                        gone = "registration closed"
                    elif slot.is_full or slot.status_code != C.STATUS_OPEN:
                        gone = "someone else took it"
                    if gone:
                        blocked.remove(court)
                        self._say("warn", f"{target.label}: court {court} - {gone}")
                        continue

                    try:
                        vm = self.client.init_register(slot.event_id)
                    except AuthError:
                        if self._reauth:
                            self._reauth()
                        break
                    except requests.RequestException:
                        break
                    except RuntimeError as exc:
                        if f"(code {C.CODE_ALREADY_HOLDS_COURT})" in str(exc):
                            continue          # still held; keep waiting
                        blocked.remove(court)
                        self._say("warn", f"{target.label}: court {court} refused - {exc}")
                        continue

                    # The server will take it now: fire with the fresh form.
                    self.on_form_model(vm)
                    body = build_payload(vm, slot.event_id, self.consent)
                    try:
                        result = self.client.register_session(body, court=court)
                    except (AuthError, requests.RequestException) as exc:
                        log.info("%s: watch fire failed (%s)", target.label, exc)
                        break
                    outcome.attempts.append(Attempt(court, slot.event_id,
                                                    datetime.now(C.SCHOOL_TZ), result))
                    if result.ok:
                        self._record_success(target, outcome, slot, court, result)
                        return
                    if result.code == C.CODE_ALREADY_HOLDS_COURT:
                        continue
                    if result.is_fatal:
                        blocked.remove(court)
                        self._say("warn", f"{target.label}: court {court} is out - "
                                          f"{result.message or 'code %d' % result.code}")

                if not blocked:
                    target.state = TargetState.LOST
                    target.message = "the held court never freed up in time"
                    outcome.message = target.message
                    self._say("warn", f"{target.label}: lost - {target.message}")
                    return
        except Exception as exc:  # noqa: BLE001 - a dead watcher must be visible
            log.exception("court watcher failed")
            target.state = TargetState.ERROR
            target.message = f"watcher stopped: {exc}"
            self._say("error", f"{target.label}: {target.message}")
