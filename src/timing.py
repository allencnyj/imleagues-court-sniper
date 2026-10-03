"""Server clock synchronisation and sub-100ms waiting.

Registration opens on the server's clock, not ours, and the only clock
reading the server gives us is the HTTP Date header -- which has
one-second resolution. The trick is to not read Date as a timestamp but
to watch for the instant it *flips* to the next second: that pins the
server's second boundary to within one poll interval.

Everything is anchored to perf_counter rather than time.time(), because
perf_counter is monotonic, high-resolution, and immune to the system
clock being nudged by NTP mid-wait.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

import constants as C

log = logging.getLogger(__name__)

#: Tiny static asset, fetched with a cache-buster so we always reach the
#: origin and get a fresh Date rather than an edge-cached one.
_PING_PATH = "/spa/images/iml-icon-thumb-white.png"

#: How far off the server clock can be, in practice.
#:
#: Measured against the live site: within a single 30-second run, 30
#: independent rollover estimates agreed to within 238ms. But separate
#: syncs minutes apart disagreed by ~1.0s, in a clean step rather than a
#: drift -- the signature of an IIS web farm where one node's clock sits
#: about a second off its peers. Which node serves the booking request is
#: not ours to choose, so no amount of sampling removes this.
#:
#: The consequence drives the whole firing strategy: aim EARLY and let a
#: fast retry loop walk in. Firing early costs nothing (the server simply
#: answers "not open yet"); firing late costs the court.
FARM_CLOCK_SKEW = 1.0


@dataclass(frozen=True)
class ClockSync:
    """A mapping from local perf_counter to server epoch seconds."""

    #: server_epoch = perf_counter() + perf_offset
    perf_offset: float
    #: Half-width of the uncertainty window, in seconds.
    uncertainty: float
    #: Best round-trip time seen during the sync, in ms.
    rtt_ms: float
    #: perf_counter value when this sync completed.
    at_perf: float
    samples: int = 0

    def server_epoch(self) -> float:
        return time.perf_counter() + self.perf_offset

    def server_now(self) -> datetime:
        return datetime.fromtimestamp(self.server_epoch(), tz=timezone.utc).astimezone(C.SCHOOL_TZ)

    def perf_for(self, when: datetime) -> float:
        """The local perf_counter reading at which the server clock hits when."""
        return when.timestamp() - self.perf_offset

    def seconds_until(self, when: datetime) -> float:
        return self.perf_for(when) - time.perf_counter()

    @property
    def age_s(self) -> float:
        return time.perf_counter() - self.at_perf

    def describe(self) -> str:
        return (f"offset {self.perf_offset:+.3f}s ±{self.uncertainty * 1000:.0f}ms "
                f"(rtt {self.rtt_ms:.0f}ms, {self.samples} samples)")


#: A neutral sync used before the first real one, so the UI has something
#: to show. Falls back to trusting the local clock.
def local_only() -> ClockSync:
    return ClockSync(
        perf_offset=time.time() - time.perf_counter(),
        uncertainty=999.0,
        rtt_ms=0.0,
        at_perf=time.perf_counter(),
        samples=0,
    )


#: A single Date-rollover observation.
@dataclass(frozen=True)
class _Flip:
    offset: float
    gap: float
    rtt: float


def sync_clock(
    session: requests.Session,
    *,
    max_polls: int = 90,
    poll_interval: float = 0.05,
    want_flips: int = 5,
    timeout: float = 5.0,
) -> ClockSync:
    """Pin the server clock by watching the Date header roll over.

    Polls a tiny asset until the Date header advances. Each rollover
    gives one independent estimate of the offset; we take the median of
    several, which is what makes this robust -- a single estimate can be
    thrown off by a stalled response or a missed poll, and averaging
    would let that outlier drag the result.

    Raises RuntimeError if too few rollovers were observed.
    """
    flips: list[_Flip] = []
    prev_date: float | None = None
    prev_mid: float | None = None
    polls = 0

    while polls < max_polls and len(flips) < want_flips:
        polls += 1
        url = f"{C.BASE_URL}{_PING_PATH}?_={random.randrange(1 << 30)}"
        t0 = time.perf_counter()
        try:
            resp = session.get(url, timeout=timeout,
                               headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
        except requests.RequestException as exc:
            log.warning("clock sync poll failed: %s", exc)
            # A failed poll leaves a hole in the timeline; the next
            # observation cannot be paired with the one before it.
            prev_date = prev_mid = None
            time.sleep(poll_interval)
            continue
        t1 = time.perf_counter()

        raw = resp.headers.get("Date")
        if not raw:
            raise RuntimeError("Server sent no Date header; cannot sync clock")
        try:
            server_epoch = parsedate_to_datetime(raw).timestamp()
        except (TypeError, ValueError):
            raise RuntimeError(f"Unparseable Date header: {raw!r}")

        rtt = t1 - t0
        # The response was generated roughly halfway through the round trip.
        mid = t0 + rtt / 2.0
        jump = None if prev_date is None else server_epoch - prev_date

        # Only an exact one-second advance tells us where a boundary is.
        # A jump of 2+ means we missed a poll, and pairing across it would
        # place the boundary a whole second off -- the failure mode that
        # made this estimator disagree with itself by ~1s.
        if jump is not None and abs(jump - 1.0) < 1e-6 and prev_mid is not None:
            gap = mid - prev_mid
            if gap < 0.5:  # a wide window carries no useful information
                flips.append(_Flip(
                    offset=server_epoch - (mid - gap / 2.0),
                    gap=gap,
                    rtt=rtt,
                ))

        prev_date, prev_mid = server_epoch, mid
        time.sleep(poll_interval)

    if len(flips) < 3:
        raise RuntimeError(
            f"Only {len(flips)} usable Date rollovers in {polls} polls; cannot sync clock")

    offsets = sorted(f.offset for f in flips)
    median = offsets[len(offsets) // 2]
    spread = offsets[-1] - offsets[0]
    # Real uncertainty is whichever is worse: the observation windows or
    # the disagreement between independent estimates.
    uncertainty = max(max(f.gap for f in flips) / 2.0, spread / 2.0)
    if spread > 0.25:
        log.warning("clock estimates disagree by %.0fms across %d samples",
                    spread * 1000, len(flips))

    return ClockSync(
        perf_offset=median,
        uncertainty=uncertainty,
        rtt_ms=min(f.rtt for f in flips) * 1000.0,
        at_perf=time.perf_counter(),
        samples=len(flips),
    )


def sleep_until_perf(target_perf: float, *, spin_window: float = 0.05,
                     should_abort=None) -> float:
    """Block until perf_counter reaches target_perf.

    Sleeps coarsely until spin_window before the target, then busy-waits.
    Windows' sleep granularity is ~15ms, far too coarse to land a shot on
    its own, so the final stretch is spun.

    should_abort, if given, is polled during the coarse phase; returning
    True aborts the wait early. Returns the overshoot in seconds
    (actual - target), which is the number worth logging.
    """
    while True:
        remaining = target_perf - time.perf_counter()
        if remaining <= spin_window:
            break
        if should_abort is not None and should_abort():
            return float("nan")
        time.sleep(min(remaining - spin_window, 0.25))

    while time.perf_counter() < target_perf:
        pass

    return time.perf_counter() - target_perf


def format_countdown(seconds: float) -> str:
    """Human countdown: 2d 03:14:07, 03:14:07, or 14:07."""
    if seconds != seconds:  # NaN
        return "--:--"
    neg = seconds < 0
    seconds = abs(int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        body = f"{days}d {hours:02d}:{mins:02d}:{secs:02d}"
    elif hours:
        body = f"{hours:02d}:{mins:02d}:{secs:02d}"
    else:
        body = f"{mins:02d}:{secs:02d}"
    return ("-" if neg else "") + body


def sleep_until_wall(
    target: datetime,
    *,
    should_abort=None,
    chunk_s: float = 5.0,
) -> bool:
    """Wait for a wall-clock instant, surviving system sleep.

    perf_counter is the right anchor for the final approach -- monotonic
    and high resolution -- but on Windows it does NOT advance while the
    machine is suspended. A queued booking can be a day out, so a laptop
    that sleeps overnight would wake still believing it had hours to go
    and fire long after the court was gone.

    So the long wait runs on the system clock, which does track real
    time across suspend, and hands over to the perf_counter path only
    for the last stretch. Precision is irrelevant here; being awake at
    all is what matters.

    Returns True if aborted.
    """
    while True:
        remaining = (target - datetime.now(C.SCHOOL_TZ)).total_seconds()
        if remaining <= 0:
            return False
        if should_abort is not None and should_abort():
            return True
        time.sleep(min(remaining, chunk_s))


def detect_suspend(last_perf: float, last_wall: float,
                   *, tolerance: float = 5.0) -> float:
    """How far wall time ran ahead of perf time since a reference pair.

    A large positive value means the machine was suspended, and any
    perf-anchored clock sync taken before it is now meaningless.
    """
    perf_elapsed = time.perf_counter() - last_perf
    wall_elapsed = time.time() - last_wall
    drift = wall_elapsed - perf_elapsed
    return drift if abs(drift) > tolerance else 0.0
