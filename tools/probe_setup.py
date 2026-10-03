"""One-time setup probe.

Signs you in (you type everything yourself, in a real browser window),
then reads the registration form the site would show for a bookable
badminton slot and reports exactly what booking demands: a waiver? a
signature? a captcha?

Nothing is booked. This only reads.

Run: .venv\\Scripts\\python.exe tools\\probe_setup.py
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import constants as C  # noqa: E402
from config import PROFILE_DIR  # noqa: E402
from imleagues_api import ImLeaguesClient  # noqa: E402
from payload import Consent, build_payload, read_requirements  # noqa: E402
from session import BrowserSession  # noqa: E402


def banner(text):
    print("\n" + text)
    print("-" * len(text))


client = ImLeaguesClient()
browser = BrowserSession(PROFILE_DIR)


def authed(cookies):
    client.set_cookies(cookies)
    return client.whoami() is not None


banner("1. Session")
cookies = browser.cached_cookies()
if cookies and authed(cookies):
    print("   Reusing the saved browser profile - already signed in.")
else:
    print("   Not signed in. Opening a browser window...")
    print("   Sign in with your UTD NetID. This script never sees your password.")
    result = browser.sign_in_interactive(is_authenticated=authed,
                                         progress=lambda m: print("   " + m))
    if not result.ok:
        print("   FAILED:", result.detail)
        sys.exit(1)
    client.set_cookies(result.cookies)

info = client.whoami() or {}
print("   Signed in. Reservation home loaded ok.")

banner("2. Calendar")
today = date.today()
slots = client.search_slots(today, today + timedelta(days=8))
print(f"   {len(slots)} badminton slots visible over the next 8 days.")

mine = [s for s in slots if s.is_registered]
print(f"   Currently reserved by you: "
      f"{[(str(s.slot_date), s.time_label, 'Ct%d' % s.court) for s in mine] or 'none'}")

open_slots = [s for s in slots if s.status_code == C.STATUS_OPEN]
if not open_slots:
    print("   No slot is currently open for registration, so the form cannot be read.")
    print("   Re-run this during a period when at least one slot is bookable.")
    sys.exit(2)

probe = open_slots[0]
print(f"   Reading the form for: {probe.slot_date} {probe.time_label} "
      f"Ct{probe.court} (event {probe.event_id})")

banner("3. Registration requirements")
vm = client.init_register(probe.event_id)
reqs = read_requirements(vm)

print(f"   reCAPTCHA required : {reqs.recaptcha}")
print(f"   waiver shown       : {reqs.waiver_shown}")
print(f"   digital signature  : {reqs.needs_signature}")
print(f"   waiver questions   : {len(reqs.waiver_questions)}")
print(f"   student ID needed  : {reqs.needs_sid} ({reqs.sid_label or 'n/a'})")
print(f"   phone needed       : {reqs.needs_phone}")
print(f"   payment needed     : {reqs.needs_payment}")
print(f"   skipInput          : {reqs.skip_input}")

if reqs.blockers:
    print("\n   *** Unattended booking is BLOCKED: " + "; ".join(reqs.blockers))
else:
    print("\n   Unattended booking is possible.")

if reqs.consent_needed:
    print("   You will need to supply once, in the app: " + ", ".join(reqs.consent_needed))

if reqs.waiver_text:
    text = reqs.waiver_text
    print(f"\n   Waiver text ({len(text)} chars), first 600:")
    print("   " + text[:600].replace("\n", "\n   "))

if reqs.waiver_questions:
    print("\n   Waiver questions:")
    for i, q in enumerate(reqs.waiver_questions):
        print(f"     [{i}] {json.dumps(q)[:300]}")

banner("4. Payload that would be sent")
payload = build_payload(vm, probe.event_id, Consent(accept_terms=True,
                                                    digital_signature="<your name>"))
print(json.dumps(payload, indent=2, default=str))

out = Path(__file__).resolve().parent.parent / "config" / "probe_vm.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(vm, indent=2, default=str), encoding="utf-8")
print(f"\n   Full form model written to {out}")
print("\nDone. Nothing was booked.")
