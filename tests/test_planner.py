"""The auto-planner: requests + accounts -> who books which court."""
import itertools
import random
import sys
from datetime import date, time as _time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from planner import Plan, Request, describe, plan_requests  # noqa: E402

D = date(2026, 9, 26)
T1, T2, T3 = _time(14, 0), _time(14, 30), _time(15, 0)


def req(courts, at=T1, day=D):
    return Request(date=day, start_time=at, courts=courts)


def assert_legal(plan: Plan, held):
    """Every rule IMLeagues enforces, checked on the plan's firm part.

    WAITING placements deliberately aim at courts the account holds, so
    they are checked only for not colliding with the firm ones."""
    firm = [p for p in plan.placements if not p.waiting]
    used = {}
    for p in firm:
        # one court per account, ever
        key = (p.account_id, p.court)
        assert key not in used, f"{p.account_id} asked for court {p.court} twice"
        assert p.court not in held[p.account_id], "court already held by that account"
        used[key] = p
    for a in held:
        mine = [p for p in firm if p.account_id == a]
        assert len(mine) + len(held[a]) <= 3
    for i, r in enumerate(plan.requests):
        placed = plan.for_request(i)
        courts = [p.court for p in placed]
        assert len(courts) == len(set(courts)), "a request got the same court twice"
        assert len(placed) <= r.courts
        # fallbacks never target a court another placement of the same
        # request is aiming at, nor (for firm ones) one the account cannot take
        for p in placed:
            for c in p.fallback:
                assert c not in courts
                if not p.waiting:
                    assert (p.account_id, c) not in used
                    assert c not in held[p.account_id]


# --- the basics --------------------------------------------------------

def test_one_account_three_courts_at_one_time():
    plan = plan_requests([req(3)], {"a": set()})
    assert plan.complete and plan.planned == 3
    assert sorted(p.court for p in plan.placements) == [1, 2, 3]
    assert all(p.account_id == "a" for p in plan.placements)
    assert all(p.fallback == [] for p in plan.placements)
    assert_legal(plan, {"a": set()})


def test_three_accounts_fill_nine_court_slots():
    reqs = [req(3, T1), req(3, T2), req(3, T3)]
    held = {"a": set(), "b": set(), "c": set()}
    plan = plan_requests(reqs, held)
    assert plan.complete and plan.planned == 9
    assert not plan.shortfalls
    assert_legal(plan, held)
    for a in held:
        assert sorted(p.court for p in plan.placements if p.account_id == a) == [1, 2, 3]


def test_held_courts_are_never_reassigned():
    held = {"a": {1}}
    plan = plan_requests([req(1)], held)
    assert plan.planned == 1
    assert plan.placements[0].court in (2, 3)
    assert 1 not in plan.placements[0].fallback
    assert_legal(plan, held)


def test_two_courts_go_to_two_different_people_when_possible():
    held = {"a": set(), "b": set()}
    plan = plan_requests([req(2)], held)
    assert plan.planned == 2
    assert {p.account_id for p in plan.placements} == {"a", "b"}
    assert_legal(plan, held)


def test_fallback_excludes_courts_our_other_account_is_aiming_at():
    held = {"a": set(), "b": set()}
    plan = plan_requests([req(2)], held)
    aimed = {p.court for p in plan.placements}
    spare = ({1, 2, 3} - aimed).pop()
    for p in plan.placements:
        assert p.fallback == [spare]


def test_load_is_spread_across_accounts():
    held = {"a": set(), "b": set(), "c": set()}
    plan = plan_requests([req(1, T1), req(1, T2), req(1, T3)], held)
    assert plan.complete
    assert sorted(sum(1 for p in plan.placements if p.account_id == a)
                  for a in held) == [1, 1, 1]


def test_full_plan_still_spreads_each_slot_across_accounts():
    # Every token is spent (8 wanted, 8 available), so only same-court
    # swaps can improve the spread -- and they must: no account should
    # be asked to hold two courts at the same time when three people
    # are available.
    held = {"axc": set(), "dal": {1}, "kev": set()}
    reqs = [req(3, T1), req(2, T2), req(3, T3)]
    plan = plan_requests(reqs, held)
    assert plan.complete and plan.planned == 8
    assert_legal(plan, held)
    for i in range(3):
        accs = [p.account_id for p in plan.for_request(i)]
        assert len(accs) == len(set(accs)), f"request {i} doubled up: {accs}"


# --- rerouting: the reason for max-flow -------------------------------

def test_a_later_request_is_served_by_shuffling_earlier_ones():
    # 'b' can only ever supply court 1. If the 2:00 request naively took
    # court 1 from 'a', the 2:30 request for all three courts would need
    # court 1 from 'b' -- fine -- but courts 2 and 3 from 'a' as well,
    # which 'a' can give. Then 3:00 for one court has nothing left unless
    # the earlier picks were arranged around it. The count must be the
    # true maximum regardless of how the search happened to start.
    held = {"a": set(), "b": {2, 3}}
    reqs = [req(1, T1), req(3, T2), req(1, T3)]
    plan = plan_requests(reqs, held)
    assert_legal(plan, held)
    # tokens: a:{1,2,3}, b:{1} -> 4 tokens, wanted 5 -> best possible is 4
    assert plan.planned == 4
    assert len(plan.for_request(1)) == 3, "the 3-court request must be whole"


def brute_force_max(reqs, held):
    """Exhaustive optimum, for cross-checking on tiny instances."""
    accounts = list(held)
    tokens = [(a, c) for a in accounts for c in (1, 2, 3) if c not in held[a]]
    best = 0

    def rec(i, spent, total):
        nonlocal best
        if i == len(reqs):
            best = max(best, total)
            return
        want = reqs[i].courts
        options = []
        for k in range(want, -1, -1):
            for courts in itertools.combinations((1, 2, 3), k):
                for accs in itertools.product(accounts, repeat=k):
                    use = set(zip(accs, courts))
                    if len(use) == k and all(t in tokens and t not in spent for t in use):
                        options.append(use)
            if options:
                break   # taking fewer than the max here can never help
        if not options:
            rec(i + 1, spent, total)
        for use in options:
            rec(i + 1, spent | use, total + len(use))

    rec(0, frozenset(), 0)
    return best


def test_matches_brute_force_on_random_instances():
    rng = random.Random(7)
    for _ in range(150):
        n_acc = rng.randint(1, 3)
        held = {f"acc{i}": set(rng.sample((1, 2, 3), rng.randint(0, 3)))
                for i in range(n_acc)}
        reqs = [req(rng.randint(1, 3), at) for at in rng.sample((T1, T2, T3), rng.randint(1, 3))]
        plan = plan_requests(reqs, held)
        assert_legal(plan, held)
        assert plan.planned == brute_force_max(reqs, held), (reqs, held, plan)


# --- reporting ---------------------------------------------------------

def test_shortfall_explains_a_full_account():
    held = {"a": {1, 2, 3}}
    plan = plan_requests([req(1)], held)
    assert plan.planned == 0
    assert "already at 3" in plan.shortfalls[0]


# --- waiting placements: book when a held court frees up --------------

def test_a_full_account_gets_waiting_placements_not_nothing():
    """Holding all three courts for tomorrow and wanting the day after:
    the plan still arms, WAITING on the held courts."""
    held = {"a": {1, 2, 3}}
    plan = plan_requests([req(2)], held)
    assert plan.planned == 0 and plan.waiting == 2
    placed = plan.for_request(0)
    assert all(p.waiting for p in placed)
    assert sorted(p.court for p in placed) == [1, 2]
    # each waits on its own court and may fall back to the other unaimed one
    for p in placed:
        assert p.court not in p.fallback
        assert set(p.fallback) <= {3}
    for t in plan.targets_for("a"):
        assert t.wait_for_free
    assert "WAITING" in describe(plan)[0]


def test_waiting_is_spread_and_never_collides_with_firm_placements():
    held = {"a": {1, 2, 3}, "b": {1, 2}}
    plan = plan_requests([req(3)], held)
    firm = [p for p in plan.placements if not p.waiting]
    waits = [p for p in plan.placements if p.waiting]
    assert len(firm) == 1 and firm[0].account_id == "b" and firm[0].court == 3
    assert len(waits) == 2
    assert {p.court for p in waits} == {1, 2}
    assert_legal(plan, held)


def test_manual_targets_never_fall_back_onto_another_accounts_court():
    """Seen live: A (Ct1, but holding Ct1) fell back to Ct2, which B was
    aimed at; A took it and B was waitlisted. A may fall back only to
    courts nobody else prefers in that slot."""
    from models import Target
    from planner import confine_manual_fallbacks
    a = [Target(date=D, start_time=T1, preferred_court=1),
         Target(date=D, start_time=T2, preferred_court=3)]
    b = [Target(date=D, start_time=T1, preferred_court=2),
         Target(date=D, start_time=T2, preferred_court=1, allow_fallback=False)]
    confine_manual_fallbacks({"a": a, "b": b})
    assert a[0].fallback_courts == [3], "Ct2 belongs to B at 2:00"
    assert b[0].fallback_courts == [3]
    assert a[1].fallback_courts == [2], "Ct1 belongs to B at 2:30"
    assert b[1].fallback_courts == [], "fallback off stays off"


def test_holdings_are_per_day_so_other_days_do_not_block():
    """The bug that cost a court: holding all three courts on Wednesday
    must not stop the same account booking Thursday -- the rule is per
    day, verified live."""
    from datetime import timedelta
    wed, thu = D, D + timedelta(days=1)
    held = {"a": {wed: {1, 2, 3}}}
    plan = plan_requests([req(3, T1, thu)], held)
    assert plan.complete and plan.planned == 3 and plan.waiting == 0
    # ...while the same request on Wednesday itself can only wait.
    plan_wed = plan_requests([req(3, T1, wed)], held)
    assert plan_wed.planned == 0 and plan_wed.waiting == 3


def test_same_day_requests_share_tokens_but_different_days_do_not():
    from datetime import timedelta
    wed, thu = D, D + timedelta(days=1)
    held = {"a": {}}
    # 3 on Wed + 3 on Thu from ONE account: fine, six firm placements.
    plan = plan_requests([req(3, T1, wed), req(3, T1, thu)], held)
    assert plan.planned == 6 and plan.waiting == 0
    # 3 + 3 on the same day from one account: only three tokens exist.
    plan = plan_requests([req(3, T1, wed), req(3, T2, wed)], held)
    assert plan.planned == 3 and plan.waiting == 3


def test_all_planner_targets_opt_into_waiting():
    plan = plan_requests([req(1)], {"a": set()})
    assert plan.targets_for("a")[0].wait_for_free


def test_no_accounts_means_every_request_is_short():
    plan = plan_requests([req(1), req(2, T2)], {})
    assert plan.planned == 0
    assert set(plan.shortfalls) == {0, 1}


def test_targets_carry_fallback_and_request_index():
    held = {"a": set(), "b": set()}
    plan = plan_requests([req(2)], held)
    for acc in held:
        for t in plan.targets_for(acc):
            assert t.request_index == 0
            assert t.account_id == acc
            assert t.fallback_courts is not None
            assert t.preferred_court not in t.fallback_courts
            assert t.allow_fallback == bool(t.fallback_courts)


def test_describe_names_accounts_and_shortfalls():
    plan = plan_requests([req(3)], {"a": {1}})
    lines = describe(plan, {"a": "allen@utd"})
    assert len(lines) == 1
    assert "allen@utd" in lines[0]
    assert "wanted 3, can book 2" in lines[0]


def test_request_json_round_trip_clamps_courts():
    r = Request.from_json({"date": "2026-09-26", "start_time": "14:00", "courts": 9})
    assert r.courts == 3
    assert Request.from_json(r.to_json()) == r
    assert Request.from_json({"date": "nope"}) is None
