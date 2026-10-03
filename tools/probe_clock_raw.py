"""Diagnostic: dump raw Date-header observations to see why sync jitters."""
import random
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import constants as C  # noqa: E402
from imleagues_api import ImLeaguesClient  # noqa: E402

client = ImLeaguesClient()
client.prewarm()
s = client.session

PATH = "/spa/images/iml-icon-thumb-white.png"
rows = []
for i in range(45):
    url = f"{C.BASE_URL}{PATH}?_={random.randrange(1 << 30)}"
    t0 = time.perf_counter()
    r = s.get(url, timeout=5, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
    t1 = time.perf_counter()
    d = parsedate_to_datetime(r.headers["Date"]).timestamp()
    rows.append((t0, t1, d, r.headers.get("Age"), r.headers.get("CF-Ray"),
                 r.headers.get("X-Served-By"), r.status_code, len(r.content)))
    time.sleep(0.05)

base = rows[0][0]
print("idx  t0      t1      rtt_ms  date_delta  age  status")
prev_d = None
flips = []
for i, (t0, t1, d, age, ray, served, st, ln) in enumerate(rows):
    delta = "" if prev_d is None else f"{d - prev_d:+.0f}"
    print(f"{i:3d}  {t0-base:6.3f}  {t1-base:6.3f}  {(t1-t0)*1000:6.1f}  {delta:>10}  {str(age):>4}  {st}")
    if prev_d is not None and d > prev_d:
        mid = t0 + (t1 - t0) / 2
        flips.append((i, d - prev_d, mid, d))
    prev_d = d

print("\nflips:", len(flips))
prev_mid = None
mids = {i: t0 + (t1 - t0) / 2 for i, (t0, t1, *_rest) in enumerate(rows)}
for idx, jump, mid, d in flips:
    gap = mid - mids[idx - 1]
    boundary = mid - gap / 2
    offset = d - boundary
    print(f"  poll {idx:3d}  jump {jump:+.0f}s  gap {gap*1000:5.1f}ms  offset {offset:.4f}")

hdrs = rows[0]
print("\nsample headers: Age=%s CF-Ray=%s X-Served-By=%s len=%s" % (hdrs[3], hdrs[4], hdrs[5], hdrs[7]))
