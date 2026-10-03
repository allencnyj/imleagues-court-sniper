"""Offline tests for clock maths and waiting. No network."""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import constants as C  # noqa: E402
from timing import ClockSync, format_countdown, sleep_until_perf  # noqa: E402


def make_sync(offset: float = 1000.0) -> ClockSync:
    return ClockSync(perf_offset=offset, uncertainty=0.05, rtt_ms=45.0,
                     at_perf=time.perf_counter(), samples=5)


def test_perf_for_inverts_server_epoch():
    sync = make_sync()
    when = datetime(2026, 9, 4, 19, 0, tzinfo=C.SCHOOL_TZ)
    assert sync.perf_for(when) == pytest.approx(when.timestamp() - 1000.0)


def test_seconds_until_counts_down():
    sync = make_sync(offset=time.time() - time.perf_counter())
    when = datetime.now(C.SCHOOL_TZ) + timedelta(seconds=30)
    assert 29.0 < sync.seconds_until(when) < 31.0


def test_server_now_lands_in_school_timezone():
    sync = make_sync(offset=time.time() - time.perf_counter())
    now = sync.server_now()
    assert now.tzinfo is C.SCHOOL_TZ
    assert abs(now.timestamp() - time.time()) < 1.0


def test_negative_offset_fires_early():
    """A negative user offset must move the fire instant earlier."""
    sync = make_sync()
    opens = datetime(2026, 9, 4, 19, 0, tzinfo=C.SCHOOL_TZ)
    on_time = sync.perf_for(opens)
    early = sync.perf_for(opens + timedelta(seconds=-1.0))
    assert early == pytest.approx(on_time - 1.0)


@pytest.mark.parametrize("target_ms", [0, 5, 40, 120])
def test_sleep_until_perf_does_not_undershoot(target_ms):
    target = time.perf_counter() + target_ms / 1000.0
    overshoot = sleep_until_perf(target)
    assert overshoot >= 0.0, "must never return before the target"
    assert overshoot < 0.02, f"overshot by {overshoot * 1000:.1f}ms"


def test_sleep_until_perf_returns_immediately_for_past_target():
    overshoot = sleep_until_perf(time.perf_counter() - 5.0)
    assert overshoot >= 5.0


def test_sleep_until_perf_honours_abort():
    target = time.perf_counter() + 10.0
    started = time.perf_counter()
    result = sleep_until_perf(target, should_abort=lambda: True)
    assert result != result  # NaN
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize("seconds,expected", [
    (0, "00:00"),
    (59, "00:59"),
    (61, "01:01"),
    (3600, "01:00:00"),
    (3661, "01:01:01"),
    (90061, "1d 01:01:01"),
    (-75, "-01:15"),
])
def test_format_countdown(seconds, expected):
    assert format_countdown(seconds) == expected


def test_format_countdown_handles_nan():
    assert format_countdown(float("nan")) == "--:--"


# --- suspend-safe waiting ---------------------------------------------

def test_sleep_until_wall_returns_at_a_past_target():
    from timing import sleep_until_wall
    past = datetime.now(C.SCHOOL_TZ) - timedelta(hours=5)
    started = time.perf_counter()
    assert sleep_until_wall(past) is False
    assert time.perf_counter() - started < 0.5


def test_sleep_until_wall_waits_for_a_future_target():
    from timing import sleep_until_wall
    target = datetime.now(C.SCHOOL_TZ) + timedelta(seconds=0.4)
    started = time.perf_counter()
    assert sleep_until_wall(target, chunk_s=0.05) is False
    assert time.perf_counter() - started >= 0.35


def test_sleep_until_wall_can_be_aborted():
    from timing import sleep_until_wall
    target = datetime.now(C.SCHOOL_TZ) + timedelta(hours=3)
    started = time.perf_counter()
    assert sleep_until_wall(target, should_abort=lambda: True) is True
    assert time.perf_counter() - started < 1.0


def test_detect_suspend_reports_nothing_when_clocks_agree():
    from timing import detect_suspend
    assert detect_suspend(time.perf_counter(), time.time()) == 0.0


def test_detect_suspend_reports_a_gap_when_wall_ran_ahead():
    """Simulates perf_counter stalling while the machine slept."""
    from timing import detect_suspend
    # Pretend perf started 8 hours later than wall did, i.e. perf lost time.
    drift = detect_suspend(time.perf_counter(), time.time() - 8 * 3600)
    assert drift > 7 * 3600, drift


def test_detect_suspend_ignores_small_jitter():
    from timing import detect_suspend
    assert detect_suspend(time.perf_counter(), time.time() - 1.0) == 0.0
