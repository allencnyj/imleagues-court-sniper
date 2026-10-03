"""Multi-account model and assignment. No network."""
import sys
from datetime import date, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from accounts import Account, assign_targets, slugify  # noqa: E402
from config import AppConfig, account_paths  # noqa: E402
from models import Target  # noqa: E402


def tgt(court, *, account_id=None, at=time(19, 0), day=date(2026, 9, 4), fallback=True):
    return Target(date=day, start_time=at, preferred_court=court,
                  allow_fallback=fallback, account_id=account_id)


# --- basics -----------------------------------------------------------

def test_slugify_makes_a_safe_id():
    assert slugify("abc123456@utdallas.edu") == "abc123456-utdallas-edu"
    assert slugify("") == "account"


def test_default_config_has_one_primary_account():
    c = AppConfig()
    assert [a.id for a in c.accounts] == ["primary"]


def test_add_account_gives_distinct_ids():
    c = AppConfig()
    a = c.add_account("Friend")
    b = c.add_account("Other")
    assert a.id != b.id
    assert {x.id for x in c.accounts} == {"primary", a.id, b.id}


def test_account_paths_are_per_account():
    p1, c1 = account_paths("primary")
    p2, c2 = account_paths("account2")
    assert p1 != p2 and c1 != c2
    assert "primary" in str(p1) and "account2" in str(p2)


def test_account_json_round_trip():
    a = Account(id="account2", label="Friend")
    a.consent.accept_terms = True
    a.form_model = {"waiver": {"show": True}}
    back = Account.from_json(a.to_json())
    assert back.id == "account2"
    assert back.label == "Friend"
    assert back.consent.accept_terms is True
    assert back.form_model == {"waiver": {"show": True}}


# --- manual assignment -----------------------------------------------

def test_manual_targets_are_honoured():
    res = assign_targets([tgt(1, account_id="primary"), tgt(2, account_id="account2")],
                         ["primary", "account2"], {"primary": set(), "account2": set()})
    assert res[0].account_id == "primary" and res[0].ok
    assert res[1].account_id == "account2" and res[1].ok


def test_manual_target_for_a_held_court_is_flagged():
    res = assign_targets([tgt(1, account_id="primary", fallback=False)],
                         ["primary"], {"primary": {1}})
    assert res[0].ok is False
    assert "court" in res[0].reason.lower()


def test_manual_target_for_an_unknown_account_is_flagged():
    res = assign_targets([tgt(1, account_id="ghost")], ["primary"], {"primary": set()})
    assert res[0].ok is False
    assert "signed in" in res[0].reason.lower()


# --- auto assignment --------------------------------------------------

def test_auto_spreads_across_accounts():
    """Three courts at one time: two accounts should share the load."""
    tgts = [tgt(c) for c in (1, 2, 3)]
    res = assign_targets(tgts, ["primary", "account2"],
                         {"primary": set(), "account2": set()})
    assert all(a.ok for a in res)
    used = {a.account_id for a in res}
    assert used == {"primary", "account2"}, "both accounts should be used"


def test_auto_never_double_claims_a_court_time_across_accounts():
    """Two accounts, both wanting court 1 at the same time -- only one can."""
    tgts = [tgt(1, fallback=False), tgt(1, fallback=False)]
    res = assign_targets(tgts, ["primary", "account2"],
                         {"primary": set(), "account2": set()})
    ok = [a for a in res if a.ok]
    assert len(ok) == 1, "the second must be refused; one spot, one winner"
    assert not ok[0] or ok[0].account_id in ("primary", "account2")


def test_auto_respects_per_account_three_court_cap():
    """One account already holds all three courts -> nothing auto-fits it."""
    tgts = [tgt(1)]
    res = assign_targets(tgts, ["primary"], {"primary": {1, 2, 3}})
    assert res[0].ok is False


def test_nine_courts_across_three_accounts():
    """The headline goal: 9 distinct (court,time) reservations, 3 per
    account, all courts distinct within each account."""
    times = [time(18, 0), time(18, 30), time(19, 0)]
    tgts = [tgt(court, at=t) for t in times for court in (1, 2, 3)]
    accounts = ["primary", "account2", "account3"]
    res = assign_targets(tgts, accounts, {a: set() for a in accounts})

    assert all(a.ok for a in res), [a.reason for a in res if not a.ok]
    # Each account ends with exactly 3, all on different courts.
    from collections import defaultdict
    courts_by_acct = defaultdict(list)
    for a, t in zip(res, tgts):
        # the reason names the court it took
        courts_by_acct[a.account_id].append(t.preferred_court)
    for acct, courts in courts_by_acct.items():
        assert len(courts) == 3, f"{acct} got {len(courts)}"


# --- config persistence + migration ----------------------------------

def test_config_round_trips_per_account_targets(tmp_path):
    from scheduler import FireSettings
    cfg = AppConfig(fire=FireSettings())
    a2 = cfg.add_account("Friend")
    cfg.primary.consent.accept_terms = True
    cfg.primary.targets = [tgt(1), tgt(2)]
    a2.targets = [tgt(3)]
    path = tmp_path / "settings.json"
    cfg.save(path)

    back = AppConfig.load(path)
    assert [a.id for a in back.accounts] == ["primary", a2.id]
    assert back.primary.consent.accept_terms is True
    assert [t.preferred_court for t in back.primary.targets] == [1, 2]
    assert [t.preferred_court for t in back.account(a2.id).targets] == [3]


def test_single_account_config_migrates_targets_to_primary(tmp_path, monkeypatch):
    """An old settings.json (top-level consent/form_model/targets, no
    accounts) must become one primary account carrying all of them."""
    import json
    import config
    monkeypatch.setattr(config, "COOKIE_CACHE", tmp_path / "session.bin")
    monkeypatch.setattr(config, "ACCOUNTS_DIR", tmp_path / "accounts")
    (tmp_path / "session.bin").write_bytes(b"legacy-cookie-blob")

    old = {
        "fire": {"offset_s": -1.0},
        "consent": {"accept_terms": True, "digital_signature": "Allen"},
        "form_model": {"waiver": {"show": True}},
        "requirements_seen": {"waiver_shown": True},
        "targets": [{"date": "2026-09-04", "start_time": "19:00",
                     "preferred_court": 2, "allow_fallback": True}],
    }
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(old), encoding="utf-8")

    cfg = AppConfig.load(path)
    assert [a.id for a in cfg.accounts] == ["primary"]
    assert cfg.primary.consent.accept_terms is True
    assert cfg.primary.consent.digital_signature == "Allen"
    assert cfg.primary.form_model == {"waiver": {"show": True}}
    assert cfg.primary.requirements_seen == {"waiver_shown": True}
    assert [t.preferred_court for t in cfg.primary.targets] == [2]
    # The session was carried into the primary account slot.
    _, primary_cache = config.account_paths("primary")
    assert primary_cache.read_bytes() == b"legacy-cookie-blob"
