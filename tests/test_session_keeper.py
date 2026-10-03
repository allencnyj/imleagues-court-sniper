"""Tests for keeping the login warm while armed. No network."""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from session import SessionKeeper  # noqa: E402


class Recorder:
    def __init__(self):
        self.messages = []
        self.lock = threading.Lock()

    def __call__(self, level, message):
        with self.lock:
            self.messages.append((level, message))

    def levels(self):
        with self.lock:
            return [lvl for lvl, _ in self.messages]


def wait_until(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_healthy_session_is_left_alone():
    notes = Recorder()
    refreshed = []
    keeper = SessionKeeper(check=lambda: True,
                           refresh=lambda: refreshed.append(1) or True,
                           notify=notes, interval_s=0.05)
    keeper.start()
    time.sleep(0.3)
    keeper.stop()

    assert refreshed == [], "must not refresh a working session"
    assert notes.levels() == [], "a healthy session should stay quiet"
    assert keeper.healthy


def test_dead_session_is_repaired_automatically():
    notes = Recorder()
    calls = {"checks": 0}

    def check():
        calls["checks"] += 1
        return calls["checks"] > 1        # dead once, then healthy

    keeper = SessionKeeper(check=check, refresh=lambda: True,
                           notify=notes, interval_s=0.05)
    keeper.start()
    assert wait_until(lambda: "success" in notes.levels())
    keeper.stop()
    assert keeper.healthy


def test_unrepairable_session_raises_a_loud_error():
    notes = Recorder()
    keeper = SessionKeeper(check=lambda: False, refresh=lambda: False,
                           notify=notes, interval_s=0.05)
    keeper.start()
    assert wait_until(lambda: "error" in notes.levels())
    keeper.stop()

    assert keeper.healthy is False
    text = " ".join(m for _, m in notes.messages)
    assert "Sign in" in text, "must tell the user what to do"


def test_check_is_skipped_while_a_shot_is_imminent():
    """A stray request could evict the pre-warmed connection."""
    notes = Recorder()
    checks = []
    keeper = SessionKeeper(check=lambda: checks.append(1) or True,
                           refresh=lambda: True, notify=notes,
                           interval_s=0.05, defer=lambda: True)
    keeper.start()
    time.sleep(0.3)
    keeper.stop()
    assert checks == [], "must not touch the network near a fire"


def test_a_raising_check_does_not_kill_the_keeper():
    notes = Recorder()
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("network blip")
        return True

    keeper = SessionKeeper(check=check, refresh=lambda: True,
                           notify=notes, interval_s=0.05)
    keeper.start()
    assert wait_until(lambda: calls["n"] >= 4), "keeper died on a transient error"
    keeper.stop()


def test_stop_is_prompt_and_idempotent():
    keeper = SessionKeeper(check=lambda: True, refresh=lambda: True,
                           notify=Recorder(), interval_s=30.0)
    keeper.start()
    assert keeper.running
    started = time.perf_counter()
    keeper.stop()
    assert time.perf_counter() - started < 2.0, "stop must not wait out the interval"
    assert not keeper.running
    keeper.stop()  # second call must be harmless


def test_start_twice_runs_only_one_thread():
    keeper = SessionKeeper(check=lambda: True, refresh=lambda: True,
                           notify=Recorder(), interval_s=30.0)
    keeper.start()
    first = keeper._thread
    keeper.start()
    assert keeper._thread is first
    keeper.stop()


def test_healthy_session_is_renewed_periodically():
    """Slide the identity session forward instead of waiting for expiry."""
    notes = Recorder()
    renewals = []
    keeper = SessionKeeper(check=lambda: True, refresh=lambda: True,
                           notify=notes, interval_s=0.05,
                           renew=lambda: renewals.append(1) or True,
                           renew_every_s=0.12)
    keeper.start()
    assert wait_until(lambda: len(renewals) >= 2, timeout=3.0)
    keeper.stop()
    assert notes.levels() == [], "renewal is routine; it should not chatter"


def test_renewal_does_not_run_on_every_check():
    notes = Recorder()
    renewals = []
    keeper = SessionKeeper(check=lambda: True, refresh=lambda: True,
                           notify=notes, interval_s=0.02,
                           renew=lambda: renewals.append(1) or True,
                           renew_every_s=30.0)
    keeper.start()
    time.sleep(0.3)
    keeper.stop()
    assert len(renewals) == 0, "renewal interval must be respected"


def test_failed_renewal_is_silent_and_keeps_the_keeper_alive():
    notes = Recorder()
    checks = []

    def boom():
        raise RuntimeError("idp unreachable")

    keeper = SessionKeeper(check=lambda: checks.append(1) or True,
                           refresh=lambda: True, notify=notes,
                           interval_s=0.05, renew=boom, renew_every_s=0.0)
    keeper.start()
    assert wait_until(lambda: len(checks) >= 3)
    keeper.stop()
    assert notes.levels() == [], "a failed renewal must not alarm the user"


def test_keeper_gives_up_after_a_failed_recovery():
    """Regression: it retried every 10 minutes, 35 times overnight,
    opening a browser window each time, for a session that could never
    be restored without a human."""
    notes = Recorder()
    refreshes = []
    keeper = SessionKeeper(check=lambda: False,
                           refresh=lambda: refreshes.append(1) or False,
                           notify=notes, interval_s=0.05)
    keeper.start()
    assert wait_until(lambda: keeper.needs_user, timeout=3.0)
    before = len(refreshes)
    time.sleep(0.4)          # several more intervals pass
    keeper.stop()

    assert len(refreshes) == before, "must not keep retrying once a human is needed"
    assert notes.levels().count("error") == 1, "must not repeat the alarm"


def test_resume_lets_the_keeper_work_again():
    notes = Recorder()
    state = {"alive": False}
    keeper = SessionKeeper(check=lambda: state["alive"],
                           refresh=lambda: False,
                           notify=notes, interval_s=0.05)
    keeper.start()
    assert wait_until(lambda: keeper.needs_user, timeout=3.0)

    state["alive"] = True
    keeper.resume()
    assert keeper.needs_user is False
    assert keeper.healthy is True
    time.sleep(0.2)
    keeper.stop()
    assert notes.levels().count("error") == 1


def test_a_recovered_session_does_not_latch():
    """A refresh that works must leave the keeper running normally."""
    notes = Recorder()
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        return calls["n"] != 1        # fails once, then healthy

    keeper = SessionKeeper(check=check, refresh=lambda: True,
                           notify=notes, interval_s=0.05)
    keeper.start()
    assert wait_until(lambda: "success" in notes.levels(), timeout=3.0)
    time.sleep(0.2)
    keeper.stop()
    assert keeper.needs_user is False
    assert "error" not in notes.levels()


# --- browser availability ---------------------------------------------

def test_browser_error_is_actionable_when_none_is_installed():
    """A friend's first run on a clean machine hits this. It used to
    escape as Playwright's raw multi-line banner, because the launch call
    sat outside the try block."""
    from session import _browser_error

    raw = Exception("BrowserType.launch_persistent_context: Executable doesn't "
                    "exist at C:/x/chromium-1234/chrome.exe")
    msg = _browser_error(raw)
    assert "playwright install chromium" in msg
    assert "Executable doesn't exist" not in msg, "raw error must not leak through"


def test_other_browser_errors_are_passed_through():
    from session import _browser_error

    msg = _browser_error(Exception("Target page crashed"))
    assert "Target page crashed" in msg
    assert "playwright install" not in msg


def test_launch_failure_is_caught_not_raised():
    """The launch itself must be inside the try, or no handler runs."""
    import inspect
    import session

    for name in ("sign_in_interactive", "refresh_cookies", "open_page"):
        src = inspect.getsource(getattr(session.BrowserSession, name))
        launch = src.index("self._launch(")
        try_at = src.index("try:")
        assert try_at < launch, f"{name}: _launch must sit inside the try block"
