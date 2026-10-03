"""Per-account target editing in the window. Stubs all network/browser.

Verifies the model the user asked for: the header selector switches which
account's three targets are on screen; each account keeps its own.
"""
import sys
import time
from datetime import date, datetime, time as _time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import constants as C  # noqa: E402
from models import Slot  # noqa: E402

DAY = (date.today() + timedelta(days=1))


def make_slots():
    slots = []
    for court in (1, 2, 3):
        start = datetime.combine(DAY, _time(19, 0), tzinfo=C.SCHOOL_TZ)
        slots.append(Slot(
            event_id=900 + court, court=court,
            name=f"RCW Badminton Reservations Court {court}", where="RCW",
            start=start, end=start + timedelta(minutes=30),
            opens=start - timedelta(hours=24), closes=start - timedelta(hours=1),
            status_code=C.STATUS_OPEN, show_signup=True,
            current_spots=0, max_spots=1, is_registered=False))
    return slots


@pytest.fixture
def app(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(config, "COOKIE_CACHE", tmp_path / "session.bin")

    import account_session
    import imleagues_api
    # Never touch a real browser or the network during a test.
    monkeypatch.setattr(account_session.AccountSession, "try_cached_session",
                        lambda self: False)
    monkeypatch.setattr(account_session.SessionKeeper, "start", lambda self: None)
    monkeypatch.setattr(imleagues_api.ImLeaguesClient, "search_slots",
                        lambda self, a, b: make_slots())
    monkeypatch.setattr(imleagues_api.ImLeaguesClient, "whoami", lambda self: None)

    import app as app_mod
    application = app_mod.SniperApp()
    for _ in range(6):
        application.update()
        time.sleep(0.01)
    yield application
    application._quitting = True
    try:
        application.destroy()
    except Exception:
        pass


def _pump(app, n=6):
    for _ in range(n):
        app.update()
        time.sleep(0.01)


def _sign_in(session, email):
    session.signed_in = True
    session.email = email
    session.account.label = email


def _fill(row, court):
    row.enabled.set(True)
    row.date_var.set(DAY.isoformat())
    row.time_var.set("7:00 PM")
    row.court_var.set(f"Court {court}")


def test_per_account_workflow(app):
    """One end-to-end pass (one Tk root): fixed rows, per-account target
    persistence across switching, and a plan that lists every account."""
    assert len(app.rows) == 3

    # The app opens in auto-plan mode; the manual rows are the old model.
    assert app.auto_mode
    app.mode_switch.set("Manual")
    app._on_mode_switched("Manual")
    assert not app.auto_mode
    assert app.manual_holder.winfo_manager() == "grid"
    assert app.auto_holder.winfo_manager() == ""

    app.slots = make_slots()
    _sign_in(app.sessions[0], "a@utd.edu")
    app._refresh_account_ui()

    # Account A: courts 1 and 2.
    _fill(app.rows[0], 1)
    _fill(app.rows[1], 2)
    app.rows[2].clear()

    # Add account B; switching to it captures A and shows B empty.
    app.on_add_account()
    _pump(app)
    assert app.active.id == "account2"
    assert [t.preferred_court for t in app.config_data.primary.targets] == [1, 2]
    assert all(not r.enabled.get() for r in app.rows), "B starts with no targets"

    # Account B: court 1 (same time as A's court 1 -- allowed, diff account).
    _sign_in(app.sessions[1], "b@utd.edu")
    app._refresh_account_ui()
    _fill(app.rows[0], 1)
    app.rows[1].clear()

    # Switch back to A: its two targets reappear; B's is preserved.
    app._on_account_selected(app._account_label(app.sessions[0]))
    _pump(app)
    assert app.active.id == "primary"
    assert sorted(r.court for r in app.rows if r.enabled.get()) == [1, 2]
    assert [t.preferred_court for t in app.config_data.accounts[1].targets] == [1]

    # The plan lists both accounts, no duplicated court in a try-order.
    app.refresh_plan()
    plan = app.plan_label.cget("text")
    assert "a@utd.edu:" in plan and "b@utd.edu:" in plan
    assert "[1, 1" not in plan and "[2, 2" not in plan and "[3, 3" not in plan

    # ---- auto plan: say what you want, the planner assigns accounts ----
    app.mode_switch.set("Auto plan")
    app._on_mode_switched("Auto plan")
    assert app.auto_mode and app.config_data.mode == "auto"
    assert app.auto_holder.winfo_manager() == "grid"
    assert app.manual_holder.winfo_manager() == ""
    assert len(app.request_rows) == 1, "starts with one empty row"

    app._populate_choices()
    row = app.request_rows[0]
    row.date_var.set(DAY.isoformat())
    row.set_times(app._times_for(DAY))
    row.time_var.set("7:00 PM")
    row.count_var.set("3 courts")
    app._held_by_account = {"primary": set(), "account2": {1}}
    app.on_request_edited()

    # 3 courts, two accounts, B already holds court 1 -> B cannot take 1.
    assert app._plan is not None and app._plan.complete
    who = {(p.account_id, p.court) for p in app._plan.placements}
    assert ("account2", 1) not in who
    assert {c for _a, c in who} == {1, 2, 3}
    text = app.plan_label.cget("text")
    assert "All 3 court(s)" in text
    assert "a@utd.edu" in text and "b@utd.edu" in text
    assert "Ct" in row.status.cget("text")

    # A second time slot: the planner shows a shortfall when tokens run out.
    app.on_add_request()
    row2 = app.request_rows[1]
    assert row2.date_var.get() == DAY.isoformat(), "inherits the previous date"
    row2.time_var.set("7:00 PM")     # same slot again: 6 courts wanted, 5 tokens
    row2.count_var.set("3 courts")
    app.on_request_edited()
    assert not app._plan.complete
    assert "5 of 6" in app.plan_label.cget("text")
    assert app._plan.waiting == 1, "the 6th court is armed as WAITING, not dropped"
    assert "(short)" in row2.status.cget("text") or "(short)" in row.status.cget("text")

    # Removing it restores a complete plan, and requests persist to config.
    app.on_remove_request(row2)
    assert len(app.request_rows) == 1 and app._plan.complete
    app._save()
    assert [r.courts for r in app.config_data.requests] == [3]

    # Arming in auto mode goes through the planner: both accounts get
    # snipers, with fallback lists that never cross each other's court.
    for s in app.sessions:
        s.account.requirements_seen = {"waiver_shown": False}
    app.on_dry_run()
    _pump(app, 30)
    assert app._armed
    assert len(app.snipers) == 2
    armed = [t for s in app.snipers for t in s.targets]
    assert sorted(t.preferred_court for t in armed) == [1, 2, 3]
    for t in armed:
        assert t.fallback_courts == [], "all three courts are aimed at; no fallback"
    assert app._request_targets and 0 in app._request_targets
    # Arm is blocked by the mode switch while running.
    assert app.mode_switch.cget("state") == "disabled"
    app.on_arm()   # disarm
    _pump(app, 10)
    assert not app._armed

    # ---- waiver still opens when every court is held (cached form) ----
    import types
    sess = app.sessions[0]
    sess.signed_in = True
    sess.email = "a@utd.edu"

    # Every live Initialize is refused (as when the account holds all 3).
    def _refuse(self, event_id):
        raise RuntimeError(" (code 205)")
    sess.client.init_register = types.MethodType(_refuse, sess.client)
    # But a form was saved earlier: the dialog must build from it.
    sess.account.form_model = {"waiver": {"show": True, "text": "Terms XYZ"},
                               "showDigitalSignature": False, "waiverQuestions": [],
                               "sid": {"show": False}, "phone": {"show": False}}
    # Mark all three courts held so no candidate is even attempted.
    app.slots = [Slot(**{**s.__dict__, "is_registered": True}) for s in make_slots()]
    app.on_waiver()
    _pump(app, 20)
    dlgs = [w for w in app.winfo_children() if w.winfo_class() == "Toplevel"]
    assert dlgs, "waiver dialog should open from the cached form"
    for d in dlgs:
        d.destroy()
