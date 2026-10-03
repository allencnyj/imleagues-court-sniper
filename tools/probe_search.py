"""Read-only check: hit the live search endpoint and print what comes back.

Run: .venv\\Scripts\\python.exe tools\\probe_search.py
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from imleagues_api import ImLeaguesClient  # noqa: E402

client = ImLeaguesClient()
rtt = client.prewarm()
print("prewarm rtt: %.1f ms" % (rtt if rtt is not None else -1))

today = date.today()
slots = client.search_slots(today, today + timedelta(days=3))
print("badminton slots found:", len(slots))

by_day = {}
for s in slots:
    by_day.setdefault(s.slot_date, []).append(s)

for day, day_slots in sorted(by_day.items()):
    times = sorted({s.time_label for s in day_slots})
    courts = sorted({s.court for s in day_slots})
    statuses = sorted({s.status_label for s in day_slots})
    print(f"\n{day}  ({len(day_slots)} slots, courts {courts}, status {statuses})")
    print("  times:", ", ".join(times))
    sample = day_slots[0]
    print(f"  sample: id={sample.event_id} Ct{sample.court} {sample.time_label} "
          f"opens={sample.opens} closes={sample.closes} "
          f"spots={sample.current_spots}/{sample.max_spots} registered={sample.is_registered}")

# Confirm the 24h lead assumption holds everywhere we can see it.
bad = [s for s in slots if s.opens and s.start and
       abs((s.start - s.opens).total_seconds() - 24 * 3600) > 60]
print("\nslots whose open time is NOT exactly 24h before start:", len(bad))
for s in bad[:5]:
    print("  ", s.slot_date, s.time_label, "Ct%d" % s.court, "opens", s.opens)
