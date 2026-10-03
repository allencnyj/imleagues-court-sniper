"""Read-only check: sync to the IMLeagues server clock and report accuracy.

Run: .venv\\Scripts\\python.exe tools\\probe_clock.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from imleagues_api import ImLeaguesClient  # noqa: E402
from timing import sleep_until_perf, sync_clock  # noqa: E402

client = ImLeaguesClient()
client.prewarm()

print("time.time() resolution probe:")
seen = set()
t_end = time.perf_counter() + 0.05
while time.perf_counter() < t_end:
    seen.add(time.time())
deltas = sorted({round(b - a, 9) for a, b in zip(sorted(seen), sorted(seen)[1:])})
print("  distinct time.time() steps (s):", deltas[:5])

for attempt in range(3):
    t0 = time.perf_counter()
    sync = sync_clock(client.session)
    print(f"\nsync #{attempt + 1}: {sync.describe()}  [took {time.perf_counter() - t0:.1f}s]")
    print("  server now:", sync.server_now().strftime("%Y-%m-%d %H:%M:%S.%f %Z"))
    print("  local  now:", time.strftime("%Y-%m-%d %H:%M:%S"))
    print("  local clock is off by: %+.3f s" % (sync.perf_offset - (time.time() - time.perf_counter())))

print("\nsleep_until_perf precision (10 x 120ms targets):")
overshoots = []
for _ in range(10):
    target = time.perf_counter() + 0.12
    overshoots.append(sleep_until_perf(target) * 1000.0)
print("  overshoot ms: min %.3f  max %.3f  mean %.3f" %
      (min(overshoots), max(overshoots), sum(overshoots) / len(overshoots)))
