"""Turning "I want N courts at these times" into per-account targets.

The user says what they want -- a time slot and how many courts -- and
this module works out which account books which court so the request is
satisfiable under IMLeagues' rules:

  * an account holds at most three reservations, and
  * no two of them may be on the same court (a court an account already
    holds, at any time, is spent for that account).

So each account really owns three *tokens*, one per court, minus the
courts it already holds. A request for k courts at one time needs k
tokens for k DIFFERENT courts, from any accounts. Which account supplies
which court is a matching problem, solved here as a max-flow so that a
request is only ever reported short when no rearrangement could satisfy
it. The instances are tiny (a handful of accounts, three courts), so a
plain augmenting-path search is more than enough.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date, datetime, time as _time
from typing import Any, Iterable, Mapping

import constants as C
from models import Target

#: What an account already holds: either {day: courts} (the real,
#: per-day shape) or a flat set of courts, taken to apply on every day.
Held = Mapping[_date, Iterable[int]] | Iterable[int]


def held_on(held: Held, day: _date) -> set[int]:
    """Courts `held` says are taken on `day`, whichever shape it has."""
    if isinstance(held, Mapping):
        return set(held.get(day, ()))
    return set(held)


@dataclass
class Request:
    """One wish: this many courts at this date and start time."""

    date: _date
    start_time: _time
    courts: int = 1

    @property
    def label(self) -> str:
        t = self.start_time.strftime("%I:%M %p").lstrip("0")
        return f"{self.date:%a %b %d} {t}"

    def to_json(self) -> dict[str, Any]:
        return {"date": self.date.isoformat(),
                "start_time": self.start_time.strftime("%H:%M"),
                "courts": self.courts}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Request | None":
        try:
            courts = max(1, min(len(C.COURTS), int(data.get("courts", 1))))
            return cls(date=_date.fromisoformat(str(data["date"])),
                       start_time=datetime.strptime(str(data["start_time"]), "%H:%M").time(),
                       courts=courts)
        except (KeyError, ValueError, TypeError):
            return None


@dataclass
class Placement:
    """One court of one request, given to one account."""

    request_index: int
    account_id: str
    court: int
    #: Other courts this account may fall back to for this slot without
    #: colliding with the rest of the plan (see plan_requests).
    fallback: list[int] = field(default_factory=list)
    #: No free token existed: this account already holds the court. The
    #: target is armed anyway and waits for the court to free up (the
    #: old reservation lapses, or the user unreserves it).
    waiting: bool = False


@dataclass
class Plan:
    requests: list[Request]
    placements: list[Placement] = field(default_factory=list)
    #: request index -> why it is short, for requests not fully placed.
    shortfalls: dict[int, str] = field(default_factory=dict)

    @property
    def wanted(self) -> int:
        return sum(r.courts for r in self.requests)

    @property
    def planned(self) -> int:
        """Courts that can be booked outright at the open instant."""
        return sum(1 for p in self.placements if not p.waiting)

    @property
    def waiting(self) -> int:
        """Courts that will be booked only if a held court frees up."""
        return sum(1 for p in self.placements if p.waiting)

    @property
    def complete(self) -> bool:
        return self.planned == self.wanted

    def for_request(self, index: int) -> list[Placement]:
        return [p for p in self.placements if p.request_index == index]

    def accounts_used(self) -> list[str]:
        seen: list[str] = []
        for p in self.placements:
            if p.account_id not in seen:
                seen.append(p.account_id)
        return seen

    def targets_for(self, account_id: str) -> list[Target]:
        """This account's share of the plan, as scheduler Targets."""
        out = []
        for p in self.placements:
            if p.account_id != account_id:
                continue
            r = self.requests[p.request_index]
            out.append(Target(date=r.date, start_time=r.start_time,
                              preferred_court=p.court, allow_fallback=bool(p.fallback),
                              account_id=account_id, fallback_courts=list(p.fallback),
                              request_index=p.request_index, wait_for_free=True))
        return out


# ---------------------------------------------------------------------
# A very small max-flow, enough for a dozen nodes a side
# ---------------------------------------------------------------------

class _Flow:
    def __init__(self) -> None:
        self.adj: dict[Any, list[Any]] = {}
        self.cap: dict[tuple[Any, Any], int] = {}

    def edge(self, u: Any, v: Any, c: int) -> None:
        self.adj.setdefault(u, []).append(v)
        self.adj.setdefault(v, []).append(u)
        self.cap[(u, v)] = self.cap.get((u, v), 0) + c
        self.cap.setdefault((v, u), 0)

    def augment(self, s: Any, t: Any) -> bool:
        """Push one unit along a DFS path in the residual graph."""
        parent: dict[Any, Any] = {s: s}
        stack = [s]
        while stack and t not in parent:
            u = stack.pop()
            for v in self.adj.get(u, []):
                if v not in parent and self.cap[(u, v)] > 0:
                    parent[v] = u
                    stack.append(v)
        if t not in parent:
            return False
        v = t
        while v != s:
            u = parent[v]
            self.cap[(u, v)] -= 1
            self.cap[(v, u)] += 1
            v = u
        return True

    def flow_on(self, u: Any, v: Any) -> int:
        return self.cap.get((v, u), 0)


def plan_requests(
    requests: list[Request],
    held_by_account: dict[str, Held],
    *,
    courts: tuple[int, ...] = C.COURTS,
) -> Plan:
    """Assign courts and accounts to every request, as fully as possible.

    `held_by_account` maps each usable (signed-in) account to what it
    already holds, per day (see Held). Account order is the tie-break,
    so put the preferred account first.

    The rule is per day, so a token is (account, day, court): holding
    Wednesday's Court 1 spends only Wednesday's token; Thursday's is
    untouched. Requests on different days therefore never compete.

    Network:  source -(k)-> request -(1)-> (request, court)
              -(1)-> (account, day, court) -(1)-> sink
    Unit capacity through (request, court) enforces distinct courts
    within a request; through the token it enforces one use per account
    per court per day. Requests are augmented in chronological order so
    an earlier slot never loses out to a later one.
    """
    accounts = list(held_by_account)
    days = sorted({r.date for r in requests})
    held = {(a, d): held_on(held_by_account[a], d) for a in accounts for d in days}
    plan = Plan(requests=list(requests))
    if not accounts or not requests:
        for i in range(len(requests)):
            plan.shortfalls[i] = "no signed-in account to book with"
        return plan

    tokens = [(a, d, c) for a in accounts for d in days for c in courts
              if c not in held[(a, d)]]
    net = _Flow()
    S, T = "S", "T"
    order = sorted(range(len(requests)),
                   key=lambda i: (requests[i].date, requests[i].start_time, i))
    for i in order:
        want = max(0, min(len(courts), requests[i].courts))
        day = requests[i].date
        net.edge(S, ("r", i), want)
        # Courts with more spare tokens that day first: keep scarce ones
        # for whoever will really need them.
        spare = {c: sum(1 for (_a, d, cc) in tokens if d == day and cc == c) for c in courts}
        for c in sorted(courts, key=lambda c: (-spare[c], c)):
            net.edge(("r", i), ("rc", i, c), 1)
            for a in accounts:
                if (a, day, c) in tokens:
                    net.edge(("rc", i, c), ("ac", a, day, c), 1)
    for tok in tokens:
        net.edge(("ac",) + tok, T, 1)

    # Saturate requests one at a time, chronologically. Each augment may
    # reroute earlier assignments, which is exactly what lets a later
    # request be served by shuffling who holds what.
    got: dict[int, int] = {}
    for i in order:
        want = max(0, min(len(courts), requests[i].courts))
        n = 0
        while n < want and _augment_from(net, S, ("r", i), T):
            n += 1
        got[i] = n

    # Read the assignment back.
    assign: dict[tuple[int, int], str] = {}
    for i in order:
        day = requests[i].date
        for c in courts:
            for a in accounts:
                if net.flow_on(("rc", i, c), ("ac", a, day, c)):
                    assign[(i, c)] = a
    day_of = {i: requests[i].date for i in range(len(requests))}
    _balance(assign, accounts, tokens, day_of)

    for (i, c), a in sorted(assign.items(), key=lambda kv: (order.index(kv[0][0]), kv[0][1])):
        plan.placements.append(Placement(request_index=i, account_id=a, court=c))

    # Fallbacks: a court this account could still take that day (token
    # unspent) that no other placement of the same request is aiming at.
    # Anything else would either be rejected by the server (court already
    # held that day) or steal a court from our own plan.
    spent = {(a, day_of[i], c) for (i, c), a in assign.items()}
    for p in plan.placements:
        aimed = {q.court for q in plan.placements if q.request_index == p.request_index}
        day = day_of[p.request_index]
        p.fallback = [c for c in courts
                      if c != p.court and c not in aimed
                      and (p.account_id, day, c) in tokens
                      and (p.account_id, day, c) not in spent]

    # Whatever could not be placed outright is placed as WAITING: an
    # account that already holds the court that day is asked to keep
    # watching it and book the moment it frees (the user unreserves it).
    # Spread across accounts like the rest, and never aimed at a court
    # another placement of the request has.
    for i in order:
        want = max(0, min(len(courts), requests[i].courts))
        short = want - got[i]
        if short <= 0:
            continue
        plan.shortfalls[i] = _explain(want, got[i], accounts, day_of[i], held, spent, courts)
        for _ in range(short):
            aimed = {q.court for q in plan.placements if q.request_index == i}
            options = [c for c in courts if c not in aimed]
            if not options:
                break
            on_this = [q.account_id for q in plan.placements if q.request_index == i]
            loads = {a: sum(1 for q in plan.placements if q.account_id == a) for a in accounts}
            acc = min(accounts, key=lambda a: (a in on_this, loads[a], accounts.index(a)))
            plan.placements.append(Placement(
                request_index=i, account_id=acc, court=options[0], waiting=True))
    # A waiting placement may fall back to any court of the slot that no
    # placement of the same request (firm or waiting) is aiming at --
    # decided only now, once every unit has picked its court.
    for p in plan.placements:
        if p.waiting:
            aimed = {q.court for q in plan.placements if q.request_index == p.request_index}
            p.fallback = [c for c in courts if c not in aimed]
    return plan


def _augment_from(net: _Flow, s: Any, first: Any, t: Any) -> bool:
    """One unit of flow from s that must leave through `first`."""
    if net.cap[(s, first)] <= 0:
        return False
    # Temporarily make `first` the only exit from the source.
    saved = {v: net.cap[(s, v)] for v in net.adj[s] if v != first}
    for v in saved:
        net.cap[(s, v)] = 0
    try:
        return net.augment(s, t)
    finally:
        for v, c in saved.items():
            net.cap[(s, v)] = c


def _balance(assign: dict[tuple[int, int], str], accounts: list[str],
             tokens: list[tuple[str, _date, int]], day_of: dict[int, _date]) -> None:
    """Spread the load without changing what gets booked.

    Max-flow only cares about the count. Two things make a plan nicer
    to run: no account asked to hold two courts at the same time (the
    app fires one account's targets back-to-back, so the second lands
    late -- three accounts in parallel is the whole point), and an even
    load across accounts. Neither changes which (time, court) pairs are
    booked, so this is a local search over two legal moves:

      * MOVE a placement to another account whose token for that court
        that day is unspent;
      * SWAP two placements of the SAME court on the SAME day between
        two requests -- always legal, since each account keeps the
        (day, court) token it had.

    The swap matters: when every token is in use (the common case for a
    full plan) there is nothing to move, but plenty to swap.
    """
    def score() -> int:
        # Doubling is penalised hard, imbalance softly.
        per_req_acc: dict[tuple[int, str], int] = {}
        for (i, _c), a in assign.items():
            per_req_acc[(i, a)] = per_req_acc.get((i, a), 0) + 1
        doubled = sum(n * (n - 1) for n in per_req_acc.values())
        loads = [sum(1 for v in assign.values() if v == a) for a in accounts]
        return 100 * doubled + sum(n * n for n in loads)

    best = score()
    improved = True
    while improved:
        improved = False
        keys = sorted(assign)
        # Moves.
        spent = {(a, day_of[i], c) for (i, c), a in assign.items()}
        for key in keys:
            i, c = key
            a, d = assign[key], day_of[i]
            for b in accounts:
                if b == a or (b, d, c) not in tokens or (b, d, c) in spent:
                    continue
                assign[key] = b
                s = score()
                if s < best:
                    best, improved = s, True
                    spent.discard((a, d, c))
                    spent.add((b, d, c))
                    break
                assign[key] = a
        # Swaps.
        for x in range(len(keys)):
            for y in range(x + 1, len(keys)):
                (i1, c1), (i2, c2) = keys[x], keys[y]
                if (c1 != c2 or i1 == i2 or day_of[i1] != day_of[i2]
                        or assign[keys[x]] == assign[keys[y]]):
                    continue
                assign[keys[x]], assign[keys[y]] = assign[keys[y]], assign[keys[x]]
                s = score()
                if s < best:
                    best, improved = s, True
                else:
                    assign[keys[x]], assign[keys[y]] = assign[keys[y]], assign[keys[x]]


def _explain(want: int, got: int, accounts: list[str], day: _date,
             held: dict[tuple[str, _date], set[int]],
             spent: set[tuple[str, _date, int]], courts: tuple[int, ...]) -> str:
    free = {c: [a for a in accounts
                if c not in held[(a, day)] and (a, day, c) not in spent]
            for c in courts}
    if not any(free.values()):
        blocked = all(len(held[(a, day)]) >= C.MAX_RESERVATIONS for a in accounts)
        return (f"wanted {want}, can book {got}: every account is "
                + (f"already at 3 reservations on {day:%a}" if blocked
                   else "out of free courts that day (used elsewhere in this plan)"))
    return (f"wanted {want}, can book {got}: only "
            f"{sum(1 for c in courts if free[c])} court(s) still have an account "
            "free to take them")


def confine_manual_fallbacks(by_account: dict[str, list[Target]],
                             *, courts: tuple[int, ...] = C.COURTS) -> None:
    """Keep manually-entered targets from racing each other.

    Manual mode has no planner, so nothing stopped one account's
    fallback from landing on the court another account was aimed at in
    the same slot -- the first one in took it and the second was
    waitlisted (seen live). At each slot, an account may now fall back
    only to courts no OTHER account prefers there. Mutates the targets.
    """
    for acc, targets in by_account.items():
        for t in targets:
            if not t.allow_fallback:
                t.fallback_courts = []
                continue
            others = {o.preferred_court
                      for a2, ts in by_account.items() if a2 != acc
                      for o in ts
                      if (o.date, o.start_time) == (t.date, t.start_time)}
            t.fallback_courts = [c for c in courts
                                 if c != t.preferred_court and c not in others]


def describe(plan: Plan, names: dict[str, str] | None = None) -> list[str]:
    """Human-readable lines, one per request, for the plan preview."""
    names = names or {}
    lines = []
    for i, r in enumerate(plan.requests):
        placed = plan.for_request(i)
        if not placed:
            lines.append(f"{r.label} × {r.courts}  ->  NONE "
                         f"({plan.shortfalls.get(i, 'nothing planned')})")
            continue
        parts = []
        for p in placed:
            who = names.get(p.account_id, p.account_id)
            fb = f" (or {','.join(map(str, p.fallback))})" if p.fallback else ""
            wait = " WAITING for it to free" if p.waiting else ""
            parts.append(f"{who}: Ct {p.court}{fb}{wait}")
        tail = f"   ← {plan.shortfalls[i]}" if i in plan.shortfalls else ""
        lines.append(f"{r.label} × {r.courts}  ->  " + "; ".join(parts) + tail)
    return lines
