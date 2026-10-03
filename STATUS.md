# Status — handoff notes

## Multi-account (added) + session-warming (solved)

- **Multiple NetIDs, parallel booking.** Each person signs in on this machine in
  their own browser profile (config/accounts/<id>/); no password stored. Target
  rows have an Account column (Auto or a specific account). One Sniper per
  account fires in parallel at the open instant. Three accounts -> up to nine
  distinct (court,time) reservations, 3 per account, courts distinct within each.
  Modules: `accounts.py` (data + assignment), `account_session.py` (per-account
  runtime), config migrated old single-account settings forward losslessly.
- **Session warming solved.** The only cookie a booking needs is
  ASP.NET_SessionId. It has a ~20 min idle timeout (slid by a 4-min page touch)
  AND a ~3.75 h absolute cap (can't be slid). Fix: proactive re-mint every 2 h,
  plus verify-and-re-mint ~60 s before every fire (per account). A truly dead
  session fails that account loudly with lead time instead of silently missing.
- **Response codes now known:** 147 = registration not open yet (retry), 205 =
  account already holds this court (fatal, drop court), 0-envelope wraps an inner
  data.code. Form model is cached so a fire needs no Initialize (which refuses
  147 before open). All resolved this session.


Written Wed 2 Sep 2026, ~03:00. Read this first in a new conversation;
`README.md` has the full reasoning, this is just where things stand.

## What the project is

Books UTD Rec Center West badminton courts on IMLeagues at the instant
registration opens (exactly 24 h before each slot). Python + CustomTkinter
desktop app. Run `run.bat`; 111 tests pass via `.venv\Scripts\python.exe -m pytest tests/ -q`.

## Proven against the live site

- **A real booking landed**: Ct2, 164 ms, first attempt. Full `RegisterSession`
  path works — payload, waiver acceptance, everything.
- **No captcha** on booking for this network. A waiver **is** required and is
  accepted once in the app (Waiver… button), then sent automatically.
- **The distinct-court rule is enforced server-side.** `RegisterEventBO.Initialize`
  returns an empty failure for any court the account already holds at *any* other
  time. Confirmed: holding Ct1 @ 11:30 PM made Initialize fail for Ct1 @ 1:00 PM
  while Ct2 succeeded at the same time.
- Clock sync ≈ ±50–60 ms; wait overshoot ~1 µs.
- Auto-disarm on finish, minimise-to-tray, windowed launch (no console).

## Not yet tested — the whole point of Thursday

1. **The timed fire.** The real booking took the "already open — fire
   immediately" path. The app has never fired at T−1 s against a window that was
   closed and then opened. This is the core mechanism.
2. **The "not open yet" response code.** Comes free with #1: at the default
   −1.00 s offset the first attempts land before the window opens. Currently the
   retry loop retries anything non-fatal; with the real code it can be precise.
3. ~~Silent SSO re-auth~~ -- **tested overnight 2 Sep, and it does not work.**
   The saved profile holds no auth cookies at all (they are session cookies,
   which Chromium never persists), so there is nothing to resume. Recovery now
   re-seeds from the encrypted cookie cache instead, which only helps while the
   server session is still alive. Expect to sign in by hand after a lapse.
4. **Court fallback under real contention** — only ever tested against stubs.

## Session lifetime -- the live question

Observed 2 Sep: the session died **20 minutes** after the last real page load,
despite BO pings every 10 minutes. Keep-alive now loads an actual page every
5 minutes (`ImLeaguesClient.touch()`). **Unverified** whether that is enough to
hold a session for hours. If it is not, a target must be armed shortly before
its open time rather than the night before.

## Resolved: a finished reservation still blocks its court

Tested 2 Sep, 5:22 PM. A Court 3 booking whose slot ran 1:00-1:30 PM -- i.e.
**ended 3.9 hours earlier** -- still made `RegisterEventBO.Initialize` refuse
Court 3, while Courts 1 and 2 were allowed. The refusal arrives as
`data.code = 205` with the envelope code left at 0, which is why it used to
surface as a bare "(code 0)".

So `held_courts()` no longer excludes past reservations; it counts every
registered slot. The earlier assumption made the app display "all three free"
while the server disagreed. Code 205 is now in `FATAL_CODES` and named
`CODE_ALREADY_HOLDS_COURT`.

How long a reservation keeps blocking is still unknown -- only that it outlasts
the slot by hours.

## Reservations as of writing

All three held, all starting Wed 2 Sep: Ct3 1:00 PM, Ct2 11:00 PM, Ct1 11:30 PM.
All should free by Thursday morning.

## Thursday's plan

Sep 4 slots open **Thu 3 Sep, 10:30 AM – 9:30 PM**, each 24 h before its start.
Pick one whose open time you'll be awake for (a Fri 7:00 PM slot opens Thu
7:00 PM), arm it a few hours ahead, leave the machine awake and not sleeping.
Then hand over `config\sniper.log`.

## Things that bit us before (don't re-introduce)

- `endDateString` is the **next day** for slots crossing midnight. Use
  `startDateStr` for the start date.
- A successful API response is **not** proof of login — the calendar is public.
  `cleanedUserEmail == "guest"` means signed out.
- The server farm's node clocks disagree by ~1 s, which is why the fire offset is
  **negative** (fire early, let retries walk in). Don't "fix" it to 0.
- Don't infer armed state from `sniper.running`; the worker thread exits on its
  own. There is an explicit `_armed` flag.
- Long waits use the **system clock**, not `perf_counter`, which stops while
  Windows sleeps.

## Never do

- No captcha bypass. No retrying failed logins (IP throttle + account lock).
- No auto-accepting waivers the user hasn't seen.
- No storing passwords. Session cookies live DPAPI-encrypted in `config\session.bin`.
- `config\` is account access — never include it when sharing the app.
  Use `tools\build_share.py` to produce a clean copy.
