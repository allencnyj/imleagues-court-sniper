"""Build a standalone Windows app folder with PyInstaller.

Produces dist/Badminton Sniper/, containing Badminton Sniper.exe plus a
user guide. A real executable carries an embedded icon resource, which is
what finally fixes the taskbar button -- WM_SETICON and AppUserModelID
both failed to move it, because Windows 11 sources that icon from the
app's identity rather than the window.

Run: .venv\\Scripts\\python.exe tools\\build_exe.py
"""
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tray import make_app_icon  # noqa: E402

APP_NAME = "Badminton Sniper"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
ICON = ROOT / "config" / "app.ico"

#: Where the finished app lives, beside the source project rather than
#: buried in dist/. This is the folder to actually run it from.
APP_HOME = ROOT.parent / "Badminton Sniper App"
#: The single file to hand to someone else.
SHARE_ZIP = ROOT.parent / "Badminton Sniper - Share.zip"


def build() -> bool:
    if not ICON.exists() and not make_app_icon(ICON):
        print("could not produce the icon")
        return False

    for path in (DIST, BUILD):
        shutil.rmtree(path, ignore_errors=True)

    cmd = [
        str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        # One folder, not one file: starts faster, and a onefile build
        # re-extracts to a temp dir on every launch, which would put
        # config/ somewhere that vanishes.
        "--onedir",
        "--windowed",                      # no console window
        "--icon", str(ICON),
        "--paths", str(ROOT / "src"),
        # Playwright ships a Node driver that PyInstaller will not find
        # by following imports alone.
        "--collect-all", "playwright",
        "--collect-all", "customtkinter",
        "--hidden-import", "PIL._tkinter_finder",
        "--hidden-import", "pystray._win32",
        str(ROOT / "src" / "main.py"),
    ]
    print("running PyInstaller...\n ", " ".join(cmd[:8]), "...\n")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print("\nPyInstaller failed")
        return False

    app_dir = DIST / APP_NAME
    exe = app_dir / f"{APP_NAME}.exe"
    if not exe.exists():
        print(f"\nexpected {exe}, not found")
        return False

    # Ship the guide alongside the executable.
    for name, dest in (("README.md", "README.md"), ("SETUP.md", "SETUP.md")):
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, app_dir / dest)
    (app_dir / "USER GUIDE.txt").write_text(GUIDE, encoding="utf-8")

    size = sum(f.stat().st_size for f in app_dir.rglob("*") if f.is_file())
    print(f"\nBuilt {app_dir}")
    print(f"  {exe.name}  ({size / 1024 / 1024:.0f} MB total)")
    install(app_dir)
    package(app_dir)
    return True


def install(app_dir: Path) -> None:
    """Copy the build into its permanent home, preserving config/.

    Rebuilding must not sign you out: config/ holds the session, the
    waiver consent and saved targets, so it is left untouched while
    everything else is replaced.
    """
    APP_HOME.mkdir(parents=True, exist_ok=True)
    for child in APP_HOME.iterdir():
        if child.name == "config":
            continue
        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()

    kept = (APP_HOME / "config").is_dir()
    for item in app_dir.iterdir():
        if item.name == "config":
            continue
        dest = APP_HOME / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
    print(f"  installed -> {APP_HOME}"
          + ("  (existing config/ kept, still signed in)" if kept else ""))
    repoint_shortcuts(APP_HOME / f"{APP_NAME}.exe")


#: Where a pythonw-era shortcut may still live. Windows resolves a
#: taskbar icon through any shortcut it can match to the running app, so
#: one of these pointing at pythonw.exe shows the Python logo instead of
#: the exe's embedded shuttlecock.
SHORTCUT_HOMES = (
    Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ROOT,
    Path.home() / "Desktop",
    Path.home() / "OneDrive" / "Desktop",
)


def repoint_shortcuts(exe: Path) -> None:
    """Retarget existing 'Badminton Sniper.lnk' files at the built exe.

    Only shortcuts that already exist are touched; none are created.
    Recreating through WScript also drops any AppUserModelID property the
    old shortcut carried, so nothing is left for Windows to mis-resolve.
    """
    script = """
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut('{lnk}')
$s.TargetPath       = '{exe}'
$s.Arguments        = ''
$s.WorkingDirectory = '{cwd}'
$s.IconLocation     = '{exe},0'
$s.Description      = 'Book UTD badminton courts on IMLeagues'
$s.Save()
"""
    for home in SHORTCUT_HOMES:
        lnk = home / f"{APP_NAME}.lnk"
        if not lnk.exists():
            continue
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             script.format(lnk=lnk, exe=exe, cwd=exe.parent)],
            capture_output=True, text=True)
        status = "repointed" if result.returncode == 0 else "could not repoint"
        print(f"  {status} shortcut -> {lnk}")
    # Explorer caches icons by path; nudge it so the change shows at once.
    subprocess.run(["ie4uinit.exe", "-show"], capture_output=True)


def package(app_dir: Path) -> None:
    """Zip the app for sharing, without any personal data.

    config/ holds the signed-in browser profile, the encrypted session
    cookies and saved settings. Shipping it would hand over account
    access, so it is excluded and recreated on the recipient's first run.
    """
    archive = SHARE_ZIP
    archive.unlink(missing_ok=True)
    skipped = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(app_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(app_dir)
            if rel.parts and rel.parts[0] == "config":
                skipped += 1
                continue
            zf.write(path, Path(APP_NAME) / rel)
    print(f"  share zip -> {archive} "
          f"({archive.stat().st_size / 1024 / 1024:.0f} MB, "
          f"{skipped} personal file(s) excluded)")


GUIDE = """BADMINTON SNIPER - USER GUIDE
=============================

Books UT Dallas Rec Center West badminton courts on IMLeagues the moment
registration opens (exactly 24 hours before each slot).


FIRST RUN
---------
Double-click "Badminton Sniper.exe".

Everything it saves - your sign-in session, settings, logs - is created in
a "config" folder next to the executable. Keep them together.

One extra step is needed the first time on a NEW computer: the app signs
you in through a real browser, and that browser has to be installed once.
If sign-in reports a browser error, open Command Prompt and run:

    pip install playwright
    playwright install chromium


USING IT
--------
1. SIGN IN. The account selector at the top shows the current account.
   Click "Sign in" and log in with your UTD NetID in the window that
   opens. It closes itself once it detects a real session, and the
   selector then shows your email with a filled dot. The app never sees
   or stores your password.

   MULTIPLE ACCOUNTS: click "+ Account" to add one, then have that
   person sit down and "Sign in" with THEIR NetID. Each account keeps
   its own separate session. Because there are only three physical
   courts and each account may hold three reservations on three
   DIFFERENT courts, every extra account adds up to three more
   (court, time) reservations.

2. WAIVER. For EACH signed-in account: select it, click "Waiver" once,
   read it, tick the box, Save. IMLeagues requires it for every booking;
   the app will not agree on anyone's behalf, so it refuses to arm
   until every account it might use has done this.

3. SAY WHAT YOU WANT. The TARGETS card has two modes:

   AUTO PLAN (the default). Add a row per time slot: the date, the
   start time, and HOW MANY courts you want then (1-3). That is all.
   The planner works out which account books which court so that
   the request is met under IMLeagues' rules - no account asked for
   a court it already holds, no two of your accounts racing for the
   same court, and the load spread across people. The line under the
   rows shows the full plan, e.g.
       Sat Sep 26 2:00 PM x 3  ->  axc: Ct 1; dal: Ct 2; kev: Ct 3
   and says plainly when something cannot be done ("Only 5 of 6
   courts can be booked...") and why. It re-plans every time you
   change a row, sign an account in, or a reservation changes.

   MANUAL. The old model: the header selector picks an account, and
   its own three rows say exactly which court it goes for. Use this
   when you want a specific person on a specific court.

4. DRY RUN first. It does everything except the final booking request.

5. ARM. Leave it running. It fires on its own, even hours later. In
   Auto plan it re-checks what every account holds right before
   arming, so the plan is built on the real calendar.


RESERVATIONS AND WAITLIST
-------------------------
* WAITLIST: if a court is already taken when your request lands, IMLeagues
  may put you on the waitlist instead of a confirmed spot. The app detects
  this, reports it as WAITLISTED (not "booked"), notifies you, and does
  nothing else - it never assumes the waitlist is what you wanted.

* UNRESERVE: click "Reservations" (top right) to see the selected
  account's current reservations, confirmed and waitlist alike, each with
  an "Unreserve" button. One click cancels it (or leaves the waitlist).
  Use this to quickly drop a court right after booking, or to clear a
  waitlist spot you don't want. It acts on the account currently selected
  in the header.

* HOLDING ALL THREE COURTS ON A DAY: once an account holds all three
  courts on one day it cannot book a fourth slot THAT day until one is
  unreserved - IMLeagues' rule, not the app's. Other days are
  unaffected. The Waiver button keeps working in this state (it uses
  the form saved from an earlier read).

* WAITING: you can still arm a court the account already holds that
  day. At the open the app takes its shot anyway; if IMLeagues refuses
  because this account holds that court, the target goes to WAITING
  instead of losing, and the app re-asks the server every minute until
  registration closes. The moment you Unreserve the old one, it books.

* REMOTE CONTROL: drop an empty file named  arm , dry-run  or  disarm
  into the config\\commands folder and the running app acts on it
  within a second. config\\status.json mirrors what the window shows.


THINGS WORTH KNOWING
--------------------
* Closing the window does NOT quit - it hides to the system tray, so an
  armed booking keeps running. To quit for real, right-click the tray
  shuttlecock and choose Quit.

* The tray icon turns RED while a booking is armed.

* Your computer must be AWAKE at the moment a booking fires. Set the
  power plan to never sleep before leaving it armed overnight.

* THE RULE IS PER DAY (verified live): on any one day an account may
  hold up to 3 reservations, each on a DIFFERENT court, and a court it
  holds that day stays blocked for the rest of that day even after the
  slot is over. Other days are independent - holding all three courts
  tomorrow does not stop you booking the day after. The app plans and
  fires by this rule.

* OFFSET is measured from the moment registration opens:
      -1.00  start trying one second early  (the default)
       0.00  fire exactly on the open
      +2.00  deliberately book two seconds after it opens
  Early is insurance, not speed. IMLeagues runs behind several servers
  whose clocks disagree by up to a second, so an early shot is simply
  rejected and retried, while a late one can lose the court.

  Firing earlier does NOT book faster. Once the window is open, how
  quickly you get in is set by "retry every", not by the offset.

* Your PC CLOCK: the app times the shot against IMLeagues' own clock,
  and warns you if Windows is more than 30 seconds off. If you see that
  warning, fix Windows time sync (Settings > Time & language > Sync now).

* SIGN IN ONCE. After the first sign-in, you should almost never have
  to do it again. Each launch reconnects every account silently in the
  background (no window, no password) by reusing the UTD trusted-device
  login saved in that account's browser profile. While the app is open
  it also keeps each session warm. You only have to sign in by hand
  again if UTD's own login finally expires - the app never stores a
  password, so that last step has to be you.

* Sessions have two timeouts: idle (~20 min) and an absolute cap
  (~3.75 h). The app touches a page every 4 min to slide the idle timer,
  re-mints every 2 h to stay under the cap, and - the real safeguard -
  verifies each account's session about a minute before it fires and
  rebuilds it if needed. If a session truly can't be restored it tells
  you which account and stops, well before the booking moment.


IF SOMETHING GOES WRONG
-----------------------
Everything the app does is written to config\\sniper.log. That file is
the first place to look, and the most useful thing to share when asking
for help.
"""


if __name__ == "__main__":
    raise SystemExit(0 if build() else 1)
