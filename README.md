# IMLeagues Badminton Sniper — UT Dallas

Books Rec Center West badminton courts on IMLeagues at the instant
registration opens, so you don't have to sit at the computer.

## How the booking system works

Learned from the live site, not guessed:

- Three courts (`RCW Badminton Reservations Court 1/2/3`), **30-minute slots**,
  **one person per court per slot** (`maxSpotCount: 1`). That's why it's a race.
- **Registration opens exactly 24 hours before the slot starts**, and closes
  1 hour before.
- Your account may hold **3 reservations**, and **each must be on a different
  court**. Holding Court 1 at 4:30 bars Court 1 at 5:00.
- Note the posted RCW policy shown on the booking page is stricter than that:
  *"1/2 hour block max reservation per patron"*, and an hour max on ONE court for
  doubles. The app books what you queue; the posted rule is worth knowing.
- Future slots are listed with their event IDs *before* registration opens, so
  the app resolves the exact target ahead of time and fires one precise request
  rather than hammering the site.
- **IMLeagues enforces the distinct-court rule server-side, at the form level.**
  `RegisterEventBO.Initialize` returns an empty failure (`isDone: false`, no code,
  no message) for any court the account already holds at *any* other time.
  Confirmed live: holding Ct1 at 11:30 PM made Initialize fail for Ct1 at
  1:00 PM, while Ct2 at the same time succeeded. The app therefore probes the
  registration form only on courts it could actually book.

## Setup

Requires Python 3.11+ on Windows. From this folder:

```bash
python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt && .venv/Scripts/python.exe -m playwright install chromium
```

Then run the one-time setup probe. It signs you in and reports exactly what the
booking form demands. **It books nothing.**

```bash
.venv/Scripts/python.exe tools/probe_setup.py
```

A browser window opens; sign in with your UTD NetID yourself. The app never sees
or stores your password — the session is kept in a Chromium profile under
`config/browser-profile/`, and re-authentication later is a silent SSO redirect
with no credentials involved.

Read the probe's output. Confirmed for UTD as of Sept 2026, by walking the
booking flow by hand:

| | |
|---|---|
| Captcha | **None** - unattended booking works |
| Waiver | **Required** - checkbox plus an Accept step |
| Digital signature / student ID / phone | Not asked for |
| Session reminders | Optional toggle |

If the probe ever reports `reCAPTCHA required : True`, unattended booking is
blocked and the app refuses to fire rather than work around it.

### Accept the waiver

Booking will not proceed until you have accepted the waiver **in the app**,
because agreeing to terms is yours to do, not the tool's. In the main window
click **Waiver...**: it pulls the real agreement text from IMLeagues, shows it,
and records your acceptance in `config/settings.json`. Until then the status
line reads *"Waiver required and NOT accepted"* and Arm refuses to start.

## Building the standalone app

```bash
.venv/Scripts/python.exe tools/build_exe.py
```

Produces `dist/Badminton Sniper/` (a real .exe plus `USER GUIDE.txt`) and
`dist/Badminton Sniper.zip` for sharing, with `config/` excluded so no session
data leaves the machine.

The .exe is also the only thing that fixes the **taskbar icon**. Windows 11
sources a taskbar button's icon from the app's identity, not from the window, so
neither `WM_SETICON` nor an AppUserModelID paired with a Start Menu shortcut
moved it -- the title bar changed but the taskbar kept showing Tk's Python logo.
An executable with an embedded icon resource just works.

Two things the build has to handle explicitly, both in `src/main.py`:

- **Paths.** Frozen, `__file__` points inside PyInstaller's temp extraction
  directory, so `config/` is anchored to the executable instead.
- **Browsers.** Playwright resolves browsers relative to the bundled package,
  which is empty. The real install is ~700 MB in `%LOCALAPPDATA%\ms-playwright`
  -- too big to bundle -- so `PLAYWRIGHT_BROWSERS_PATH` is pointed at it. A
  machine that has never run `playwright install chromium` gets a clear message
  saying exactly that.

## Running

```bash
run.bat
```

Or `.venv/Scripts/python.exe src/main.py`.

1. **Sign in** if the dot is red (it's green when the saved session still works).
2. **Refresh slots** to pull the calendar.
3. **Waiver...** once, to read and accept the agreement. Arm refuses until this
   is done.
4. Tick up to three target rows: date, start time, preferred court. Leave
   *fallback* on so it moves to another court if yours is taken.
5. Check the **Plan** line — it shows the court order it will try, and when each
   target opens.
6. **Dry run** first. It does everything except the final POST.
7. **Arm**. Leave the app running; it fires on its own, hours later if need be.

When the queue drains the app **disarms itself**, reports what it booked, and
unticks the rows that succeeded so the queue is ready for the next round.
Targets that failed stay ticked so you can retry them. A booking also triggers a
calendar refresh, so the courts-held tile stays honest.

## Sessions and staying signed in

**The saved browser profile does not keep you signed in.** This was assumed at
first and it is wrong, verified by inspecting the profile: every cookie that
carries authentication is a *session* cookie with no expiry --
`ASP.NET_SessionId`, `ApiTokenForSPA`, `ApiRefreshTokenForSPA`, and the
Shibboleth SSO cookie `_shibsession_...`. Chromium never writes session cookies
to disk, so closing the sign-in browser discards all of them. The profile keeps
analytics cookies and nothing else.

Two consequences:

- `/account/ssoredirect` cannot silently restore anything. With no Shibboleth
  cookie it just lands on the marketing homepage. Recovery instead re-seeds the
  browser from the encrypted cookie cache and loads the reservation page, which
  works only while the server side of that session is still alive.
- Once the server session has expired, **only signing in again gets it back.**
  The app says so plainly and stops trying.

### Keeping the session alive

The session appears to have an **idle timeout of about 20 minutes**. Pinging the
BO endpoints was not enough -- a session pinged every 10 minutes still died 20
minutes after the last real page load, which suggests
`AjaxRequestHandler.ashx` does not slide the timeout. The keeper therefore loads
an actual page (`client.touch()`) every 5 minutes.

If recovery fails, the keeper **latches**: it notifies once and stops. An earlier
version retried every 10 minutes and opened a browser window each time -- 35 of
them overnight -- for a session that could never be restored without a human.

The header always shows **Signed in as \<your email\>**. If it just says
"Signed in" with no address, or the dot is red, the session is not real --
the reservation calendar is public, so a page loading proves nothing.

### No credentials, anywhere

There is no password field in this app, and nowhere on disk that holds one.
Session cookies are cached in `config/session.bin`, encrypted with Windows DPAPI
so only your Windows account on this machine can read them.

## Why the fire offset is negative

Default is `-1.00s` — it fires a second *early*, deliberately.

IMLeagues runs behind an IIS web farm whose node clocks disagree. Measured
against the live site: within one 30-second run, 30 independent clock estimates
agreed to within 238 ms, but separate syncs minutes apart differed by a clean
~1.0 s step. You can't choose which node serves your booking, so no amount of
sampling removes that.

Firing early is free — the server just answers "not open yet" and the retry loop
(every 200 ms, for 15 s) walks you in the moment it opens. Firing late costs the
court. Given the fastest human is ~6–7 seconds, landing within a few hundred
milliseconds is a large margin.

## Closing the window

The X button **hides the app to the system tray**; it does not quit. A queued
booking can be hours away and closing the window is an easy misclick, so exiting
is deliberate: right-click the tray icon and choose **Quit**.

The tray icon is a shuttlecock, and it turns **red while armed**. Right-clicking
it offers *Open*, *Disarm* (only while armed), and *Quit*. Double-clicking opens
the window. Outcomes are announced as tray notifications, so a booking that lands
while the window is hidden still tells you.

Quitting stops the worker threads first, so nothing is left running.

If the notification area is unavailable, the X button quits normally rather than
hiding the window somewhere you cannot reach it.

## Long waits and system sleep

A queued booking can be more than a day out. The long wait therefore runs on the
**system clock**, not `perf_counter`: on Windows `perf_counter` does not advance
while the machine is suspended, so a laptop that slept overnight would wake still
believing it had hours to go, and fire long after the court was gone. Only the
final approach — the last minute, after a fresh clock sync — uses `perf_counter`,
where its precision is what matters.

If a suspend is detected (wall time ran ahead of `perf_counter`), the app logs it
and re-syncs before firing. Even so, the machine has to be *awake* at the open
moment to fire at all, so for an overnight wait it is worth setting the power plan
to never sleep.

## What it will not do

- **Bypass a captcha.** If the network turns reCAPTCHA on for bookings, the app
  reports it and stops.
- **Retry a failed login.** IMLeagues throttles by IP and locks accounts after
  repeated failures. One attempt, then it asks you to sign in by hand.
- **Accept a waiver you haven't seen.** Terms, signatures, and waiver answers
  come from what you entered during setup; nothing is auto-agreed.
- **Store your password.** There is nowhere in this codebase that holds one.
  Session cookies are cached in `config/session.bin`, encrypted with Windows
  DPAPI so only your Windows account on this machine can read them.

## Layout

| Path | What it does |
|---|---|
| `src/constants.py` | Network IDs, endpoint names, server error codes, court parsing |
| `src/models.py` | `Slot`, `Target`, `FireResult`; parsing the API's date formats |
| `src/imleagues_api.py` | The three calls that matter, over `AjaxRequestHandler.ashx` |
| `src/timing.py` | Server clock sync via Date-header rollover; sub-ms waiting |
| `src/session.py` | Chromium profile, interactive sign-in, silent SSO refresh |
| `src/secrets_store.py` | Windows DPAPI encrypt/decrypt for the cookie cache |
| `src/payload.py` | Builds the `joinEventVO` exactly as the site's own page does |
| `src/scheduler.py` | Court selection rules and the firing loop |
| `src/theme.py` | Monochrome dark palette, type scale, spacing tokens |
| `src/tray.py` | Shuttlecock icon (window, taskbar, tray), menu, notifications |
| `src/app.py` | The window |
| `tools/probe_*.py` | Read-only diagnostics; none of them book anything |

## Tests

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

73 tests, no network. They cover the distinct-court rule, the retry policy, the
clock maths, and the full firing loop against a stub client — including that a
dry run never submits, that a court you already hold is never attempted, and
that fallback is reached within one cycle rather than after the retry window.

## Diagnostics

```bash
.venv/Scripts/python.exe tools/probe_search.py
```

Read-only, no login needed: dumps the calendar, confirms every slot's open time
is exactly 24 h before its start.

```bash
.venv/Scripts/python.exe tools/probe_clock.py
```

Reports clock offset, its uncertainty, and the precision of the wait loop.
