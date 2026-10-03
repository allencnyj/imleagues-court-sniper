"""End-to-end tests of the firing loop against a stub client. No network."""
import sys
import time
from datetime import datetime, time as _time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import constants as C  # noqa: E402
import scheduler as sched  # noqa: E402
from models import FireResult, Slot, Target, TargetState  # noqa: E402
from payload import Consent  # noqa: E402
from scheduler import FireSettings, Sniper  # noqa: E402
from timing import ClockSync  # noqa: E402

#: Tomorrow, relative to the real clock: the scheduler skips slots whose
#: registration has closed, so a fixed date would rot.
TODAY = (datetime.now(C.SCHOOL_TZ) + timedelta(days=1)).date()
AT = _time(19, 0)


def make_slot(court, *, opens_in=0.0, registered=False, event_id=None, at=AT):
    """A slot whose registration opens `opens_in` seconds from now."""
    opens = datetime.now(C.SCHOOL_TZ) + timedelta(seconds=opens_in)
    start = datetime.combine(TODAY, at, tzinfo=C.SCHOOL_TZ)
    return Slot(
        event_id=event_id if event_id is not None else 7000 + court,
        court=court,
        name=f"RCW Badminton Reservations Court {court}",
        where=f"Rec Center West, Badminton Ct {court}",
        start=start,
        end=start + timedelta(minutes=30),
        opens=opens,
        closes=start - timedelta(hours=1),
        status_code=C.STATUS_OPEN,
        show_signup=True,
        current_spots=0,
        max_spots=1,
        is_registered=registered,
    )


class FakeClient:
    """Stands in for ImLeaguesClient. Records every booking attempt."""

    def __init__(self, slots, *, wins=(), fail_code=999, vm=None, waitlist_courts=()):
        self.slots = list(slots)
        self.wins = set(wins)          # courts that will accept a booking
        self.fail_code = fail_code
        self.vm = vm if vm is not None else {"waiver": {"show": False}}
        self.waitlist_courts = set(waitlist_courts)  # wins that land on waitlist
        self.attempts = []             # (court, event_id)
        self.session = object()
        self.prewarmed = 0

    def reservation_info(self, event_id):
        court = next((s.court for s in self.slots if s.event_id == event_id), None)
        return {"event_member_id": 10000 + (court or 0),
                "waitlisted": court in self.waitlist_courts,
                "registered": True}

    def search_slots(self, start, end):
        return list(self.slots)

    def init_register(self, event_id):
        return dict(self.vm)

    def prewarm(self):
        self.prewarmed += 1
        return 12.0

    def whoami(self):
        return {}

    def register_session(self, payload, *, court):
        self.attempts.append((court, payload["eventId"]))
        ok = court in self.wins
        if ok:
            # Mark it held, as the real site would.
            for i, s in enumerate(self.slots):
                if s.court == court:
                    self.slots[i] = Slot(**{**s.__dict__, "is_registered": True})
        return FireResult(ok=ok, code=0 if ok else self.fail_code,
                          message="ok" if ok else "not available",
                          court=court, event_id=payload["eventId"], elapsed_ms=5.0)


@pytest.fixture(autouse=True)
def instant_clock(monkeypatch):
    """Skip real network clock sync; trust the local clock."""
    monkeypatch.setattr(sched, "sync_clock", lambda *a, **k: ClockSync(
        perf_offset=time.time() - time.perf_counter(),
        uncertainty=0.01, rtt_ms=10.0, at_perf=time.perf_counter(), samples=5))


def fast_settings(**kw):
    base = dict(offset_s=0.0, retry_interval_s=0.01, retry_window_s=0.5,
                resync_lead_s=60.0, prewarm_lead_s=5.0)
    base.update(kw)
    return FireSettings(**base)


def run(sniper, targets, *, dry_run=False, timeout=20.0):
    sniper.arm(targets, dry_run=dry_run)
    deadline = time.time() + timeout
    while sniper.running and time.time() < deadline:
        time.sleep(0.02)
    sniper.disarm()
    assert time.time() < deadline, "sniper did not finish in time"


def target(court, *, fallback=True, at=AT):
    return Target(date=TODAY, start_time=at, preferred_court=court, allow_fallback=fallback)


LATER = _time(19, 30)


def two_time_slots(**kw):
    """Courts 1-3 at both 7:00 and 7:30, so two targets can both resolve."""
    return ([make_slot(c, at=AT, event_id=7000 + c, **kw) for c in (1, 2, 3)]
            + [make_slot(c, at=LATER, event_id=7100 + c, **kw) for c in (1, 2, 3)])


# --- core flows -------------------------------------------------------

def test_books_the_preferred_court_when_it_is_free():
    client = FakeClient([make_slot(c) for c in (1, 2, 3)], wins={2})
    sniper = Sniper(client, fast_settings(), Consent())
    t = target(2)
    run(sniper, [t])

    assert t.state is TargetState.WON
    assert t.won_court == 2
    assert client.attempts[0][0] == 2


def test_falls_back_to_another_court_when_the_preferred_one_loses():
    client = FakeClient([make_slot(c) for c in (1, 2, 3)], wins={3})
    sniper = Sniper(client, fast_settings(), Consent())
    t = target(1)
    run(sniper, [t])

    assert t.won_court == 3
    tried = [court for court, _ in client.attempts]
    assert tried[0] == 1, "must try the preferred court first"
    assert 3 in tried


def test_no_fallback_means_only_the_preferred_court_is_attempted():
    client = FakeClient([make_slot(c) for c in (1, 2, 3)], wins={3})
    sniper = Sniper(client, fast_settings(), Consent())
    t = target(1, fallback=False)
    run(sniper, [t])

    assert t.state is TargetState.LOST
    assert {court for court, _ in client.attempts} == {1}


def test_a_court_already_held_is_never_attempted():
    """The distinct-court rule, enforced against the live calendar."""
    slots = [make_slot(1, registered=True), make_slot(2), make_slot(3)]
    client = FakeClient(slots, wins={1, 2, 3})
    sniper = Sniper(client, fast_settings(), Consent())
    t = target(1)
    run(sniper, [t])

    assert 1 not in {court for court, _ in client.attempts}
    assert t.won_court in (2, 3)


def test_second_target_avoids_the_court_the_first_one_won():
    """Two targets both preferring court 1: the second must move on."""
    client = FakeClient(two_time_slots(), wins={1, 2, 3})
    sniper = Sniper(client, fast_settings(), Consent())
    first, second = target(1), target(1, at=LATER)
    run(sniper, [first, second])

    assert first.state is TargetState.WON and first.won_court == 1
    assert second.state is TargetState.WON, "the second target must still resolve"
    assert second.won_court != 1, "must not book court 1 twice"


def test_all_three_courts_held_skips_the_target():
    slots = [make_slot(c, registered=True) for c in (1, 2, 3)]
    client = FakeClient(slots, wins={1, 2, 3})
    sniper = Sniper(client, fast_settings(), Consent())
    t = target(1)
    run(sniper, [t])

    assert t.state is TargetState.SKIPPED
    assert client.attempts == []


def test_dry_run_never_posts_a_booking():
    client = FakeClient([make_slot(c) for c in (1, 2, 3)], wins={1})
    sniper = Sniper(client, fast_settings(), Consent())
    t = target(1)
    run(sniper, [t], dry_run=True)

    assert client.attempts == [], "dry run must not submit anything"
    assert t.state is TargetState.SIMULATED
    assert "court 1" in t.message


def test_dry_run_does_not_claim_a_win():
    """Nothing was sent, so it cannot know the booking would succeed."""
    client = FakeClient([make_slot(c) for c in (1, 2, 3)], wins=set())
    sniper = Sniper(client, fast_settings(), Consent())
    t = target(2)
    run(sniper, [t], dry_run=True)

    assert t.state is not TargetState.WON
    assert t.won_court is None, "a simulated run must not record a court as held"


def test_dry_run_win_does_not_block_later_targets():
    """A simulated court must not be treated as one we now hold."""
    client = FakeClient(two_time_slots(), wins=set())
    sniper = Sniper(client, fast_settings(), Consent())
    first, second = target(1), target(1, at=LATER)
    run(sniper, [first, second], dry_run=True)

    assert "court 1" in first.message
    assert "court 1" in second.message, "court 1 was never actually taken"


def test_connection_is_prewarmed_before_firing():
    client = FakeClient([make_slot(c) for c in (1, 2, 3)], wins={1})
    sniper = Sniper(client, fast_settings(), Consent())
    run(sniper, [target(1)])
    assert client.prewarmed >= 1


def test_retries_until_the_window_expires_then_gives_up():
    client = FakeClient([make_slot(c) for c in (1, 2, 3)], wins=set())
    sniper = Sniper(client, fast_settings(retry_window_s=0.25), Consent())
    t = target(1)
    run(sniper, [t])

    assert t.state is TargetState.LOST
    assert len(client.attempts) > 1, "should have retried at least once"


def test_fatal_response_stops_retrying_that_court():
    client = FakeClient([make_slot(c) for c in (1, 2, 3)],
                        wins=set(), fail_code=C.CODE_MEMBER_INELIGIBLE)
    sniper = Sniper(client, fast_settings(retry_window_s=2.0), Consent())
    run(sniper, [target(1)])

    # One shot per court, no retries on a fatal code.
    per_court = {}
    for court, _ in client.attempts:
        per_court[court] = per_court.get(court, 0) + 1
    assert all(n == 1 for n in per_court.values()), per_court


def test_recaptcha_requirement_blocks_instead_of_guessing():
    client = FakeClient([make_slot(c) for c in (1, 2, 3)], wins={1},
                        vm={"waiver": {"show": False}, "enableFitnessRecaptcha": True})
    sniper = Sniper(client, fast_settings(), Consent())
    t = target(1)
    run(sniper, [t])

    assert t.state is TargetState.ERROR
    assert "reCAPTCHA" in t.message
    assert client.attempts == []


def test_unaccepted_waiver_blocks_instead_of_auto_agreeing():
    client = FakeClient([make_slot(c) for c in (1, 2, 3)], wins={1},
                        vm={"waiver": {"show": True, "text": "terms"}})
    sniper = Sniper(client, fast_settings(), Consent(accept_terms=False))
    t = target(1)
    run(sniper, [t])

    assert t.state is TargetState.ERROR
    assert "waiver" in t.message.lower()
    assert client.attempts == []


def test_accepted_waiver_allows_booking():
    client = FakeClient([make_slot(c) for c in (1, 2, 3)], wins={1},
                        vm={"waiver": {"show": True, "text": "terms"}})
    sniper = Sniper(client, fast_settings(), Consent(accept_terms=True))
    t = target(1)
    run(sniper, [t])

    assert t.state is TargetState.WON


def test_waits_for_a_future_open_time():
    """A target that opens shortly must not fire before then."""
    client = FakeClient([make_slot(c, opens_in=1.0) for c in (1, 2, 3)], wins={1})
    sniper = Sniper(client, fast_settings(), Consent())
    started = time.perf_counter()
    run(sniper, [target(1)])
    assert time.perf_counter() - started >= 0.9, "fired too early"


def test_disarm_stops_a_pending_target():
    client = FakeClient([make_slot(c, opens_in=30.0) for c in (1, 2, 3)], wins={1})
    sniper = Sniper(client, fast_settings(), Consent())
    sniper.arm([target(1)])
    time.sleep(0.3)
    started = time.perf_counter()
    sniper.disarm()
    assert time.perf_counter() - started < 3.0, "disarm should be prompt"
    assert client.attempts == []


def test_fallback_is_reached_quickly_not_after_the_retry_window():
    """Regression: a retryable rejection on the preferred court used to
    consume the whole window, so fallback courts were never tried."""
    client = FakeClient([make_slot(c) for c in (1, 2, 3)], wins={3})
    # A long window: if fallback waited for it to expire, this would hang.
    sniper = Sniper(client, fast_settings(retry_window_s=30.0), Consent())
    t = target(1)
    started = time.perf_counter()
    run(sniper, [t], timeout=10.0)

    assert t.won_court == 3
    assert time.perf_counter() - started < 5.0, "fallback took far too long"
    assert [court for court, _ in client.attempts][:3] == [1, 2, 3]


def test_rotation_drops_a_court_that_returns_a_fatal_code_but_keeps_others():
    slots = [make_slot(c) for c in (1, 2, 3)]

    class PickyClient(FakeClient):
        def register_session(self, payload, *, court):
            self.attempts.append((court, payload["eventId"]))
            if court == 1:
                return FireResult(ok=False, code=C.CODE_MEMBER_INELIGIBLE,
                                  message="ineligible", court=court,
                                  event_id=payload["eventId"], elapsed_ms=1.0)
            if court == 3 and len(self.attempts) > 4:
                return FireResult(ok=True, code=0, message="ok", court=court,
                                  event_id=payload["eventId"], elapsed_ms=1.0)
            return FireResult(ok=False, code=999, message="not yet", court=court,
                              event_id=payload["eventId"], elapsed_ms=1.0)

    client = PickyClient(slots)
    sniper = Sniper(client, fast_settings(retry_window_s=5.0), Consent())
    t = target(1)
    run(sniper, [t], timeout=10.0)

    assert t.won_court == 3
    court1_attempts = [c for c, _ in client.attempts if c == 1]
    assert len(court1_attempts) == 1, "a fatal court must leave the rotation"


def closed_slot(court, *, closed_ago_s=3600.0, event_id=None):
    """A slot whose registration window has already shut."""
    now = datetime.now(C.SCHOOL_TZ)
    start = now + timedelta(minutes=30)
    return Slot(
        event_id=event_id if event_id is not None else 8000 + court,
        court=court,
        name=f"RCW Badminton Reservations Court {court}",
        where="Rec Center West",
        start=start,
        end=start + timedelta(minutes=30),
        opens=now - timedelta(hours=24),
        closes=now - timedelta(seconds=closed_ago_s),
        status_code=C.STATUS_CLOSED,
        show_signup=False,
        current_spots=0,
        max_spots=1,
        is_registered=False,
    )


def started_slot(court):
    """A slot that is already underway."""
    now = datetime.now(C.SCHOOL_TZ)
    start = now - timedelta(minutes=10)
    return Slot(
        event_id=9000 + court, court=court,
        name=f"RCW Badminton Reservations Court {court}", where="Rec Center West",
        start=start, end=start + timedelta(minutes=30),
        opens=start - timedelta(hours=24), closes=start + timedelta(hours=2),
        status_code=C.STATUS_OPEN, show_signup=True,
        current_spots=0, max_spots=1, is_registered=False,
    )


def test_target_whose_registration_already_closed_is_skipped():
    """Firing at a shut window would burn the whole retry budget."""
    slots = [closed_slot(c) for c in (1, 2, 3)]
    client = FakeClient(slots, wins={1, 2, 3})
    sniper = Sniper(client, fast_settings(), Consent())
    t = Target(date=slots[0].slot_date, start_time=slots[0].slot_time,
               preferred_court=1, allow_fallback=True)
    run(sniper, [t])

    assert t.state is TargetState.SKIPPED
    assert "closed" in t.message.lower()
    assert client.attempts == [], "must not fire at a closed window"


def test_target_whose_slot_already_started_is_skipped():
    slots = [started_slot(c) for c in (1, 2, 3)]
    client = FakeClient(slots, wins={1, 2, 3})
    sniper = Sniper(client, fast_settings(), Consent())
    t = Target(date=slots[0].slot_date, start_time=slots[0].slot_time,
               preferred_court=1, allow_fallback=True)
    run(sniper, [t])

    assert t.state is TargetState.SKIPPED
    assert "started" in t.message.lower()
    assert client.attempts == []


def test_still_open_past_window_fires_immediately():
    """Opened hours ago but not yet closed: go straight at it."""
    now = datetime.now(C.SCHOOL_TZ)
    start = now + timedelta(hours=3)
    slots = [Slot(event_id=7700 + c, court=c,
                  name=f"RCW Badminton Reservations Court {c}", where="RCW",
                  start=start, end=start + timedelta(minutes=30),
                  opens=now - timedelta(hours=13), closes=start - timedelta(hours=1),
                  status_code=C.STATUS_OPEN, show_signup=True,
                  current_spots=0, max_spots=1, is_registered=False)
             for c in (1, 2, 3)]
    client = FakeClient(slots, wins={1})
    sniper = Sniper(client, fast_settings(), Consent())
    t = Target(date=slots[0].slot_date, start_time=slots[0].slot_time,
               preferred_court=1, allow_fallback=True)
    started = time.perf_counter()
    run(sniper, [t], timeout=10.0)

    assert t.won_court == 1
    assert time.perf_counter() - started < 5.0, "should not wait for a past open time"


def held_elsewhere_slots():
    """Courts 1-3 at 7:00, but the account already holds court 1 at 7:30.

    Mirrors the live behaviour: IMLeagues refuses to open the
    registration form for a court you hold at any other time.
    """
    return ([make_slot(c, at=AT, event_id=7000 + c) for c in (1, 2, 3)]
            + [make_slot(1, at=LATER, event_id=7101, registered=True)])


class PickyInitClient(FakeClient):
    """Initialize fails for any court the account already holds."""

    def __init__(self, slots, *, held, **kw):
        super().__init__(slots, **kw)
        self.held = set(held)
        self.init_calls = []

    def init_register(self, event_id):
        self.init_calls.append(event_id)
        court = next((s.court for s in self.slots if s.event_id == event_id), None)
        if court in self.held:
            raise RuntimeError(" (code 0)")
        return dict(self.vm)


def test_probe_skips_courts_the_account_already_holds():
    """Regression: probing the first open slot picked a held court and
    killed a target that was otherwise perfectly bookable."""
    slots = held_elsewhere_slots()
    client = PickyInitClient(slots, held={1}, wins={2})
    sniper = Sniper(client, fast_settings(), Consent())
    t = target(2)
    run(sniper, [t], timeout=10.0)

    assert t.state is TargetState.WON, t.message
    assert t.won_court == 2
    tried_courts = [next(s.court for s in slots if s.event_id == e)
                    for e in client.init_calls]
    assert 1 not in tried_courts, "must not probe a court it already holds"


def test_probe_falls_through_to_another_court_on_refusal():
    """Even if the first probe is refused, another court should work."""
    slots = [make_slot(c, at=AT, event_id=7000 + c) for c in (1, 2, 3)]
    # Court 1 refuses even though nothing says it is held.
    client = PickyInitClient(slots, held={1}, wins={2, 3})
    sniper = Sniper(client, fast_settings(), Consent())
    t = target(1)
    run(sniper, [t], timeout=10.0)

    assert len(client.init_calls) > 1, "should have tried another court"
    assert t.state is TargetState.WON


def test_all_probes_refused_reports_a_useful_message():
    slots = [make_slot(c, at=AT, event_id=7000 + c) for c in (1, 2, 3)]
    client = PickyInitClient(slots, held={1, 2, 3}, wins=set())
    sniper = Sniper(client, fast_settings(), Consent())
    t = target(1)
    run(sniper, [t], timeout=10.0)

    assert t.state is TargetState.ERROR
    assert "booking form" in t.message.lower()
    assert client.attempts == [], "must not fire without a form model"


def opens_in(seconds, court, *, event_id=None):
    """A slot whose registration opens `seconds` from now."""
    now = datetime.now(C.SCHOOL_TZ)
    start = datetime.combine(TODAY, AT, tzinfo=C.SCHOOL_TZ)
    return Slot(event_id=event_id or (6000 + court), court=court,
                name=f"RCW Badminton Reservations Court {court}", where="RCW",
                start=start, end=start + timedelta(minutes=30),
                opens=now + timedelta(seconds=seconds),
                closes=start - timedelta(hours=1),
                status_code=C.STATUS_OPEN, show_signup=True,
                current_spots=0, max_spots=1, is_registered=False)


class TimingClient(FakeClient):
    """Records exactly when the booking request was sent."""

    def __init__(self, slots, **kw):
        super().__init__(slots, **kw)
        self.fired_at = None

    def register_session(self, payload, *, court):
        if self.fired_at is None:
            self.fired_at = datetime.now(C.SCHOOL_TZ)
        return super().register_session(payload, court=court)


def test_positive_offset_books_that_many_seconds_after_open():
    """Offset +N means: land the booking N seconds after registration
    opens, not N seconds early."""
    slots = [opens_in(1.0, c) for c in (1, 2, 3)]
    opens = slots[0].opens
    client = TimingClient(slots, wins={1})
    sniper = Sniper(client, fast_settings(offset_s=2.0), Consent())
    run(sniper, [target(1)], timeout=20.0)

    assert client.fired_at is not None, "never fired"
    delay = (client.fired_at - opens).total_seconds()
    assert 1.8 <= delay <= 2.6, f"fired {delay:.2f}s after open, wanted ~2.0s"


def test_negative_offset_fires_before_open():
    slots = [opens_in(3.0, c) for c in (1, 2, 3)]
    opens = slots[0].opens
    client = TimingClient(slots, wins={1})
    sniper = Sniper(client, fast_settings(offset_s=-1.0), Consent())
    run(sniper, [target(1)], timeout=20.0)

    delay = (client.fired_at - opens).total_seconds()
    assert delay < 0, f"fired {delay:.2f}s after open; a negative offset must be early"


def test_zero_offset_fires_at_the_open_moment():
    slots = [opens_in(1.5, c) for c in (1, 2, 3)]
    opens = slots[0].opens
    client = TimingClient(slots, wins={1})
    sniper = Sniper(client, fast_settings(offset_s=0.0), Consent())
    run(sniper, [target(1)], timeout=20.0)

    delay = abs((client.fired_at - opens).total_seconds())
    assert delay < 0.4, f"fired {delay:.2f}s from the open moment"


class NotOpenYetClient(FakeClient):
    """Reproduces Thu 3 Sep 10:29 AM: nothing is open, so Initialize
    refuses every event with code 147 ("registration not open yet")."""

    def __init__(self, slots, **kw):
        super().__init__(slots, **kw)
        self.init_calls = 0

    def init_register(self, event_id):
        self.init_calls += 1
        raise RuntimeError(" (code 147)")


def test_cached_form_lets_it_fire_when_nothing_is_open():
    """The real miss: at T-60s the target's own slots have not opened, so
    Initialize refuses them, and there was no open slot to read a form
    from. It aborted a minute before it would have fired."""
    slots = [make_slot(c) for c in (1, 2, 3)]
    client = NotOpenYetClient(slots, wins={1})
    sniper = Sniper(client, fast_settings(), Consent(),
                    form_model={"waiver": {"show": False}})
    t = target(1)
    run(sniper, [t], timeout=15.0)

    assert t.state is TargetState.WON, t.message
    assert t.won_court == 1


def test_without_a_cached_form_it_says_what_to_do():
    slots = [make_slot(c) for c in (1, 2, 3)]
    client = NotOpenYetClient(slots, wins={1})
    sniper = Sniper(client, fast_settings(), Consent())
    t = target(1)
    run(sniper, [t], timeout=15.0)

    assert t.state is TargetState.ERROR
    assert "waiver" in t.message.lower(), t.message
    assert client.attempts == []


def test_a_fresh_form_is_handed_back_for_caching():
    """A successful read must be persisted, or the next fire has nothing."""
    saved = []
    slots = [make_slot(c) for c in (1, 2, 3)]   # make_slot is open by default
    client = FakeClient(slots, wins={1})
    sniper = Sniper(client, fast_settings(), Consent(),
                    on_form_model=saved.append)
    run(sniper, [target(1)], timeout=15.0)

    assert saved, "the form model was never handed back"
    assert "waiver" in saved[0]


def test_code_147_is_not_fatal():
    """Firing early depends on retrying through 'not open yet'."""
    from scheduler import should_keep_retrying

    assert C.CODE_REGISTRATION_NOT_OPEN not in C.FATAL_CODES
    not_open = FireResult(ok=False, code=C.CODE_REGISTRATION_NOT_OPEN,
                          message="", court=1, event_id=1, elapsed_ms=1.0)
    assert should_keep_retrying(not_open) is True


class StaleThenReauthClient(FakeClient):
    """whoami() fails until a reauth 'fixes' it, mirroring a session that
    died during the long wait and is re-minted just before firing."""

    def __init__(self, slots, **kw):
        super().__init__(slots, **kw)
        self.alive = False

    def whoami(self):
        return {"email": "x@utdallas.edu"} if self.alive else None


def test_dead_session_is_reminted_just_before_firing():
    slots = [make_slot(c) for c in (1, 2, 3)]
    client = StaleThenReauthClient(slots, wins={1})

    def reauth():
        client.alive = True      # the browser re-mint succeeds
        return True

    sniper = Sniper(client, fast_settings(), Consent(),
                    form_model={"waiver": {"show": False}}, reauth=reauth)
    t = target(1)
    run(sniper, [t], timeout=15.0)

    assert t.state is TargetState.WON, t.message
    assert t.won_court == 1


def test_unrecoverable_session_fails_the_target_without_firing():
    slots = [make_slot(c) for c in (1, 2, 3)]
    client = StaleThenReauthClient(slots, wins={1})   # stays dead
    sniper = Sniper(client, fast_settings(), Consent(),
                    form_model={"waiver": {"show": False}},
                    reauth=lambda: False)
    t = target(1)
    run(sniper, [t], timeout=15.0)

    assert t.state is TargetState.ERROR
    assert "session" in t.message.lower()
    assert client.attempts == [], "must not fire on a dead session"


def test_waitlisted_result_is_not_counted_as_a_win():
    """A contested slot that lands on the waitlist must report WAITLISTED,
    not WON, and must not claim a court."""
    client = FakeClient([make_slot(c) for c in (1, 2, 3)],
                        wins={1}, waitlist_courts={1})
    sniper = Sniper(client, fast_settings(), Consent())
    t = target(1, fallback=False)
    run(sniper, [t])

    assert t.state is TargetState.WAITLISTED
    assert t.won_court is None
    assert t.event_member_id == 10001
    assert "waitlist" in t.message.lower()


def test_confirmed_booking_records_the_member_id():
    client = FakeClient([make_slot(c) for c in (1, 2, 3)], wins={2})
    sniper = Sniper(client, fast_settings(), Consent())
    t = target(2)
    run(sniper, [t])

    assert t.state is TargetState.WON
    assert t.event_member_id == 10002
    assert t.event_id is not None


# --- planner-made targets ---------------------------------------------

def test_planner_fallback_list_confines_which_courts_are_tried():
    """A target with fallback_courts=[3] must never touch court 2, even
    though court 2 is free -- another of our own accounts is aiming at it."""
    client = FakeClient([make_slot(c) for c in (1, 2, 3)], wins={3})
    sniper = Sniper(client, fast_settings(), Consent())
    t = Target(date=TODAY, start_time=AT, preferred_court=1,
               allow_fallback=True, fallback_courts=[3])
    run(sniper, [t])

    assert t.won_court == 3
    tried = [court for court, _ in client.attempts]
    assert tried[0] == 1
    assert 2 not in tried, "court 2 belongs to another account in the plan"


def test_planner_target_with_no_fallback_list_only_tries_preferred():
    client = FakeClient([make_slot(c) for c in (1, 2, 3)], wins={3})
    sniper = Sniper(client, fast_settings(), Consent())
    t = Target(date=TODAY, start_time=AT, preferred_court=1,
               allow_fallback=False, fallback_courts=[])
    run(sniper, [t])

    assert t.state is TargetState.LOST
    assert {court for court, _ in client.attempts} == {1}


# --- waiting for a held court to free up ------------------------------

class HeldCourtClient(FakeClient):
    """Courts in `held` answer 205 (this account holds them) until
    `frees_after` register attempts on that court have been made."""

    def __init__(self, slots, *, held, frees_after=2, **kw):
        super().__init__(slots, **kw)
        self.held = set(held)
        self.frees_after = frees_after
        self.probes = {}

    def _held_now(self, court):
        return court in self.held and self.probes.get(court, 0) < self.frees_after

    def init_register(self, event_id):
        court = next(s.court for s in self.slots if s.event_id == event_id)
        self.probes[court] = self.probes.get(court, 0) + 1
        if self._held_now(court):
            raise RuntimeError(" (code 205)")
        return dict(self.vm)

    def register_session(self, payload, *, court):
        self.attempts.append((court, payload["eventId"]))
        if self._held_now(court):
            return FireResult(ok=False, code=C.CODE_ALREADY_HOLDS_COURT,
                              message="already holds", court=court,
                              event_id=payload["eventId"], elapsed_ms=5.0)
        return super().register_session(payload, court=court)


def test_held_court_is_watched_and_booked_once_it_frees():
    """The user's case: every court is held by this account, so the fire
    gets 205 everywhere. Instead of losing, keep asking; the moment the
    server lets go (unreserve, or lapse), book it."""
    slots = [make_slot(c, registered=True) for c in (1, 2, 3)]
    client = HeldCourtClient(slots, held={1, 2, 3}, frees_after=3, wins={1, 2, 3},
                             vm={"waiver": {"show": False}})
    sniper = Sniper(client, fast_settings(wait_interval_s=0.05), Consent(),
                    form_model={"waiver": {"show": False}})
    t = Target(date=TODAY, start_time=AT, preferred_court=1, wait_for_free=True)
    run(sniper, [t], timeout=15.0)

    assert t.state is TargetState.WON, t.message
    assert t.won_court == 1
    assert client.probes[1] >= 3, "must have kept asking the server"


def test_waiting_target_gives_up_when_registration_closes():
    slots = [make_slot(c, registered=True) for c in (1, 2, 3)]
    # closes in the past: nothing to wait for
    slots = [Slot(**{**s.__dict__, "closes": datetime.now(C.SCHOOL_TZ) + timedelta(seconds=0.3)})
             for s in slots]
    client = HeldCourtClient(slots, held={1, 2, 3}, frees_after=10**6, wins=set(),
                             vm={"waiver": {"show": False}})
    sniper = Sniper(client, fast_settings(wait_interval_s=0.05), Consent(),
                    form_model={"waiver": {"show": False}})
    t = Target(date=TODAY, start_time=AT, preferred_court=2, allow_fallback=False,
               wait_for_free=True)
    run(sniper, [t], timeout=15.0)

    assert t.state is TargetState.LOST
    assert "never freed" in t.message


def test_without_wait_for_free_a_held_court_is_still_skipped():
    slots = [make_slot(c, registered=True) for c in (1, 2, 3)]
    client = HeldCourtClient(slots, held={1, 2, 3}, wins=set())
    sniper = Sniper(client, fast_settings(), Consent(), form_model={"waiver": {"show": False}})
    t = Target(date=TODAY, start_time=AT, preferred_court=1)
    run(sniper, [t])
    assert t.state is TargetState.SKIPPED
    assert client.attempts == []


def test_a_waiting_target_does_not_delay_the_next_one():
    """Waiting runs on its own thread: the account's later target must
    still fire on time while the first sits in WAITING."""
    slots = ([make_slot(c, at=AT, event_id=7000 + c, registered=(c == 1)) for c in (1, 2, 3)]
             + [make_slot(c, at=LATER, event_id=7100 + c) for c in (1, 2, 3)])
    client = HeldCourtClient(slots, held={1}, frees_after=10**6, wins={2, 3},
                             vm={"waiver": {"show": False}})
    sniper = Sniper(client, fast_settings(wait_interval_s=0.2), Consent(),
                    form_model={"waiver": {"show": False}})
    first = Target(date=TODAY, start_time=AT, preferred_court=1, allow_fallback=False,
                   wait_for_free=True)
    second = Target(date=TODAY, start_time=LATER, preferred_court=2, wait_for_free=True)
    sniper.arm([first, second])
    deadline = time.time() + 10
    while time.time() < deadline and second.state is not TargetState.WON:
        time.sleep(0.02)
    assert second.state is TargetState.WON, second.message
    assert first.state is TargetState.WAITING
    assert sniper.running, "the watcher keeps the sniper alive"
    sniper.disarm()
    assert not sniper.running
