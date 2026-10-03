"""Offline tests for the court-selection rules. No network.

The rule being enforced: three reservations per account, and no two of
them on the same court.
"""
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import constants as C  # noqa: E402
from models import FireResult, Slot  # noqa: E402
from scheduler import (  # noqa: E402
    candidate_courts,
    find_slot,
    held_courts,
    should_keep_retrying,
    slots_for,
)


def make_slot(court=1, day=date(2026, 9, 4), hhmm=(19, 0), *,
              event_id=None, registered=False, status=C.STATUS_NOT_YET_OPEN):
    start = datetime.combine(day, time(*hhmm), tzinfo=C.SCHOOL_TZ)
    return Slot(
        event_id=event_id if event_id is not None else 1000 + court,
        court=court,
        name=f"RCW Badminton Reservations Court {court}",
        where=f"Rec Center West, Badminton Ct {court}",
        start=start,
        end=start + timedelta(minutes=30),
        opens=start - timedelta(hours=24),
        closes=start - timedelta(hours=1),
        status_code=status,
        show_signup=status == C.STATUS_OPEN,
        current_spots=0,
        max_spots=1,
        is_registered=registered,
    )


# --- candidate_courts -------------------------------------------------

def test_preferred_court_comes_first():
    assert candidate_courts(2, held=[]) == [2, 1, 3]


def test_falls_back_to_other_courts_in_order():
    assert candidate_courts(1, held=[]) == [1, 2, 3]
    assert candidate_courts(3, held=[]) == [3, 1, 2]


def test_a_court_already_held_is_never_offered():
    """The distinct-court rule: holding Ct1 at 4:30 bars Ct1 at 5:00."""
    assert candidate_courts(2, held=[1]) == [2, 3]
    assert 1 not in candidate_courts(1, held=[1])


def test_preferred_court_already_held_falls_back_to_the_rest():
    assert candidate_courts(1, held=[1]) == [2, 3]


def test_two_courts_held_leaves_only_the_third():
    assert candidate_courts(1, held=[1, 2]) == [3]
    assert candidate_courts(3, held=[1, 2]) == [3]


def test_three_courts_held_means_no_candidates():
    assert candidate_courts(1, held=[1, 2, 3]) == []


def test_reservation_cap_blocks_even_with_a_free_court():
    """Cap is on reservation count, not distinct courts."""
    assert candidate_courts(1, held=[1, 2, 3]) == []


def test_fallback_disabled_tries_only_the_preferred_court():
    assert candidate_courts(2, held=[], allow_fallback=False) == [2]


def test_fallback_disabled_and_preferred_held_gives_nothing():
    assert candidate_courts(2, held=[2], allow_fallback=False) == []


def test_held_may_be_any_iterable():
    assert candidate_courts(1, held={2}) == [1, 3]
    assert candidate_courts(1, held=(2,)) == [1, 3]


# --- held_courts ------------------------------------------------------

def test_held_courts_reads_registered_flags():
    slots = [make_slot(1, registered=True), make_slot(2), make_slot(3, registered=True)]
    assert held_courts(slots) == {1, 3}


def test_held_courts_counts_finished_reservations_too():
    """Verified live: a booking whose slot ENDED hours ago still made
    IMLeagues refuse that court (code 205). Excluding past reservations
    made the app claim courts were free when the server disagreed."""
    past = make_slot(1, day=date(2026, 9, 1), registered=True)
    future = make_slot(2, day=date(2026, 9, 9), registered=True)
    now = datetime(2026, 9, 4, 12, 0, tzinfo=C.SCHOOL_TZ)
    assert held_courts([past, future], now=now) == {1, 2}


def test_held_courts_ignores_the_now_argument():
    slots = [make_slot(1, day=date(2026, 9, 1), registered=True)]
    assert held_courts(slots) == {1}
    assert held_courts(slots, now=datetime(2030, 1, 1, tzinfo=C.SCHOOL_TZ)) == {1}


def test_code_205_is_fatal_so_the_rotation_drops_that_court():
    assert C.CODE_ALREADY_HOLDS_COURT in C.FATAL_CODES
    assert should_keep_retrying(result(code=C.CODE_ALREADY_HOLDS_COURT)) is False


# --- lookup helpers ---------------------------------------------------

def test_find_slot_matches_date_time_and_court():
    slots = [make_slot(c, hhmm=(19, 0)) for c in (1, 2, 3)]
    slots += [make_slot(c, hhmm=(19, 30), event_id=2000 + c) for c in (1, 2, 3)]
    found = find_slot(slots, date(2026, 9, 4), time(19, 30), 2)
    assert found is not None and found.event_id == 2002


def test_find_slot_returns_none_when_absent():
    slots = [make_slot(1)]
    assert find_slot(slots, date(2026, 9, 4), time(6, 0), 1) is None
    assert find_slot(slots, date(2026, 9, 5), time(19, 0), 1) is None


def test_slots_for_groups_courts_at_one_time():
    slots = [make_slot(c) for c in (1, 2, 3)] + [make_slot(1, hhmm=(20, 0), event_id=99)]
    at = slots_for(slots, date(2026, 9, 4), time(19, 0))
    assert sorted(at) == [1, 2, 3]
    assert at[2].event_id == 1002


# --- retry policy -----------------------------------------------------

def result(code=0, ok=False):
    return FireResult(ok=ok, code=code, message="", court=1, event_id=1, elapsed_ms=1.0)


def test_success_stops_retrying():
    assert should_keep_retrying(result(ok=True)) is False


def test_unknown_failure_keeps_retrying():
    """The expected early-fire case: not open yet, so keep knocking."""
    assert should_keep_retrying(result(code=999)) is True


@pytest.mark.parametrize("code", sorted(C.FATAL_CODES))
def test_fatal_codes_stop_retrying(code):
    assert should_keep_retrying(result(code=code)) is False


@pytest.mark.parametrize("code", sorted(C.AUTH_CODES))
def test_auth_failures_stop_retrying(code):
    assert should_keep_retrying(result(code=code)) is False


# --- per-day holdings ---------------------------------------------------

def test_held_courts_is_per_day():
    """Verified live 2026-09-23: a court held on one day does not block
    that court on another day; within a day a finished slot still counts."""
    from datetime import date as _d
    from scheduler import held_by_day, held_courts
    wed, thu = _d(2026, 9, 23), _d(2026, 9, 24)
    slots = [make_slot(1, day=wed, registered=True), make_slot(3, day=wed, registered=True),
             make_slot(2, day=thu, registered=True), make_slot(1, day=thu)]
    assert held_courts(slots, wed) == {1, 3}
    assert held_courts(slots, thu) == {2}
    assert held_courts(slots) == {1, 2, 3}, "no day: legacy union"
    assert held_by_day(slots) == {wed: {1, 3}, thu: {2}}
