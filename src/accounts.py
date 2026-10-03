"""Multiple UTD accounts, so several NetIDs can book in parallel.

Each account holder signs in themselves, once, in their own browser
profile -- no password is ever stored or typed for them, same as the
single-account model. This module holds the *pure* account data and the
assignment logic; the live per-account runtime (client, browser, keeper)
lives in account_session.py.

The reservation rules apply PER ACCOUNT: each account may hold three
reservations, all on different courts. There are only three physical
courts, so "nine courts" means nine distinct (court, time) reservations
spread across accounts, and no two accounts should chase the same
(court, time) -- that is one spot, one winner, and a second account
aimed at it only wastes its shot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import time as _time
from typing import Any

from models import Target
from payload import Consent

#: Sentinel account_id meaning "let the app choose".
AUTO = None

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """A filesystem-safe id from an email or label."""
    base = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return base or "account"


@dataclass
class Account:
    """One person's account. No password -- the session lives in a
    browser profile keyed by `id`."""

    id: str
    label: str = ""
    #: Waiver acceptance is a personal act, so consent is per account.
    consent: Consent = field(default_factory=Consent)
    #: registerFitnessEventVM is account-level; cache it per account.
    form_model: dict[str, Any] = field(default_factory=dict)
    requirements_seen: dict[str, Any] = field(default_factory=dict)
    #: This account's own targets (its up-to-three courts). Each account
    #: is edited independently, so its targets live with it.
    targets: list[Target] = field(default_factory=list)

    @property
    def display(self) -> str:
        return self.label or self.id

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "consent": self.consent.to_json(),
            "form_model": self.form_model,
            "requirements_seen": self.requirements_seen,
            "targets": [t.to_json() for t in self.targets],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Account":
        targets = [t for t in (Target.from_json(d)
                               for d in data.get("targets") or []) if t]
        return cls(
            id=str(data.get("id") or "account"),
            label=str(data.get("label") or ""),
            consent=Consent.from_json(data.get("consent")),
            form_model=data.get("form_model") or {},
            requirements_seen=data.get("requirements_seen") or {},
            targets=targets,
        )


# ---------------------------------------------------------------------
# Assignment: which account books which target
# ---------------------------------------------------------------------

MAX_PER_ACCOUNT = 3
COURTS = (1, 2, 3)


def _courts_for(preferred: int, held: set[int], allow_fallback: bool) -> list[int]:
    """Courts this account could still legally take, preferred first."""
    if len(held) >= MAX_PER_ACCOUNT:
        return []
    avail = [c for c in COURTS if c not in held]
    if not allow_fallback:
        return [preferred] if preferred in avail else []
    if preferred in avail:
        return [preferred] + [c for c in avail if c != preferred]
    return avail


@dataclass
class Assignment:
    """The account chosen for one target, and why (for the plan preview)."""

    target_index: int
    account_id: str | None
    reason: str = ""
    ok: bool = True


def assign_targets(
    targets: list[Any],
    account_ids: list[str],
    held_by_account: dict[str, set[int]],
) -> list[Assignment]:
    """Resolve every target to a concrete account.

    Manual targets (account_id set) are honoured and only checked for
    legality. Auto targets (account_id None) are spread across the
    signed-in accounts that can still take the court, avoiding two
    accounts landing on the same (court, time).

    `targets` need only expose .account_id, .preferred_court,
    .allow_fallback, .date, .start_time.
    """
    out: list[Assignment] = []
    # A running tally so we never over-book one account or double-claim a
    # (court, time) across accounts.
    committed: dict[str, set[int]] = {
        a: set(held_by_account.get(a, set())) for a in account_ids}
    claimed_slots: set[tuple[Any, _time, int]] = set()
    load: dict[str, int] = {a: 0 for a in account_ids}

    for i, t in enumerate(targets):
        pref, fallback = t.preferred_court, getattr(t, "allow_fallback", True)
        slot_key = (t.date, t.start_time)

        def can_take(acc: str) -> int | None:
            for court in _courts_for(pref, committed[acc], fallback):
                if (*slot_key, court) not in claimed_slots:
                    return court
            return None

        if t.account_id is not None:
            acc = t.account_id
            if acc not in committed:
                out.append(Assignment(i, acc, "account not signed in", ok=False))
                continue
            court = can_take(acc)
            if court is None:
                out.append(Assignment(i, acc, "no court left this account may book",
                                      ok=False))
                continue
            committed[acc].add(court)
            claimed_slots.add((*slot_key, court))
            load[acc] += 1
            out.append(Assignment(i, acc, f"court {court} first"))
            continue

        # Auto: fewest-loaded account that can take a court here.
        best: tuple[int, str, int] | None = None
        for acc in account_ids:
            court = can_take(acc)
            if court is not None and (best is None or load[acc] < best[0]):
                best = (load[acc], acc, court)
        if best is None:
            out.append(Assignment(
                i, None, "no signed-in account can take this slot", ok=False))
            continue
        _, acc, court = best
        committed[acc].add(court)
        claimed_slots.add((*slot_key, court))
        load[acc] += 1
        out.append(Assignment(i, acc, f"auto → {acc}, court {court} first"))

    return out
