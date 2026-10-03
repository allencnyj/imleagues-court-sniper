"""Diagnose why the SSO refresh reports 'not authenticated'.

Works on a COPY of the browser profile so it cannot fight the running
app for the profile lock, and cannot corrupt the real one.

Navigates only. Submits nothing, types nothing.

Run: .venv\\Scripts\\python.exe tools\\probe_sso.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import constants as C  # noqa: E402
from config import PROFILE_DIR  # noqa: E402
from imleagues_api import ImLeaguesClient  # noqa: E402

work = Path(tempfile.mkdtemp(prefix="sso-probe-"))
profile = work / "profile"
print(f"copying profile -> {profile}")
shutil.copytree(PROFILE_DIR, profile, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("Singleton*", "*.lock"))

from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile), headless=True,
        viewport={"width": 1280, "height": 860},
        args=["--disable-blink-features=AutomationControlled"],
    )
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        hops = []
        page.on("framenavigated",
                lambda f: hops.append(f.url) if f == page.main_frame else None)

        print("\n1. Do the profile's existing cookies still authenticate?")
        client = ImLeaguesClient()
        client.set_cookies(ctx.cookies())
        who = client.whoami()
        print("   whoami before any navigation:", who or "NOT SIGNED IN")
        names = sorted({c["name"] for c in ctx.cookies()
                        if "imleagues" in (c.get("domain") or "")})
        print("   imleagues cookies in profile:", names or "none")

        print("\n2. Navigating the SSO redirect...")
        print("   ", C.SSO_REDIRECT)
        page.goto(C.SSO_REDIRECT, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass

        print("\n3. Where did it land?")
        print("   final url  :", page.url)
        print("   page title :", page.title())
        print("   redirect chain:")
        for h in hops:
            print("     ->", h[:160])

        text = page.inner_text("body")[:900].strip()
        print("\n4. What the page says:")
        for line in [ln for ln in text.splitlines() if ln.strip()][:20]:
            print("   |", line[:150])

        print("\n5. Do the cookies authenticate AFTER the redirect?")
        client.set_cookies(ctx.cookies())
        who = client.whoami()
        print("   whoami:", who or "NOT SIGNED IN")

        print("\n6. Try loading the reservation page directly instead:")
        page.goto(C.RESERVATION_HOME, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        print("   url  :", page.url)
        client.set_cookies(ctx.cookies())
        who = client.whoami()
        print("   whoami:", who or "NOT SIGNED IN")
        body = page.inner_text("body")[:400]
        signal = ("LOGIN" if "LOGIN" in body.upper() or "SIGN IN" in body.upper()
                  else "no obvious login prompt")
        print("   page shows:", signal)
    finally:
        ctx.close()
        shutil.rmtree(work, ignore_errors=True)
        print("\n(temporary profile copy removed)")
