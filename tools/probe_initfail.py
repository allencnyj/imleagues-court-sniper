"""Diagnose why RegisterEventBO.Initialize is returning an empty failure.

READ-ONLY. Calls Initialize only; never RegisterSession, so nothing can
be booked by running this.
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import constants as C  # noqa: E402
import secrets_store  # noqa: E402
from config import COOKIE_CACHE  # noqa: E402
from imleagues_api import ImLeaguesClient  # noqa: E402

client = ImLeaguesClient()
blob = secrets_store.read_secret(COOKIE_CACHE)
if not blob:
    print("No cached session. Open the app and sign in first.")
    raise SystemExit(1)
client.set_cookies(json.loads(blob.decode("utf-8")))

who = client.whoami()
print("signed in as:", (who or {}).get("email", "NOT SIGNED IN"))
if who is None:
    raise SystemExit(1)

today = date.today()
slots = client.search_slots(today, today + timedelta(days=3))
print(f"{len(slots)} slots\n")

by_status = {}
for s in slots:
    by_status.setdefault(s.status_label, []).append(s)
print("status breakdown:", {k: len(v) for k, v in by_status.items()})

mine = [s for s in slots if s.is_registered]
print("registered:", [(str(s.slot_date), s.time_label, f"Ct{s.court}", s.event_id) for s in mine])

# This is exactly how the scheduler picks its probe event.
probe = next((s for s in slots if s.status_code == C.STATUS_OPEN), None)
print("\nscheduler would probe:",
      (str(probe.slot_date), probe.time_label, f"Ct{probe.court}", probe.event_id)
      if probe else None)

print("\n--- raw Initialize envelopes ---")
candidates = []
if probe:
    candidates.append(("scheduler's pick (first OPEN)", probe))
for s in slots:
    if s.is_registered:
        candidates.append(("one you already hold", s))
        break
for s in slots:
    if s.status_code == C.STATUS_OPEN and not s.is_registered and not s.is_full:
        if all(s.event_id != c[1].event_id for c in candidates):
            candidates.append(("open, not yours, not full", s))
            break
for s in slots:
    if s.status_code == C.STATUS_NOT_YET_OPEN:
        candidates.append(("not yet open (future)", s))
        break

for label, s in candidates:
    env = client.call_bo(C.BO_REGISTER_INIT, {
        "entityType": "reservation",
        "entityId": C.SCHOOL_ID,
        "pageType": "Reservation",
        "eventId": s.event_id,
    }, referrer=C.register_page(s.event_id))
    print(f"\n[{label}] {s.slot_date} {s.time_label} Ct{s.court} "
          f"id={s.event_id} status={s.status_label} "
          f"spots={s.current_spots}/{s.max_spots} registered={s.is_registered}")
    print(f"   isDone={env.is_done} code={env.code} message={env.message!r}")
    if isinstance(env.data, dict):
        print("   data keys:", sorted(env.data.keys())[:12])
        vm = env.data.get("registerFitnessEventVM")
        print("   registerFitnessEventVM present:", vm is not None)
    else:
        print("   data:", repr(env.data)[:300])
