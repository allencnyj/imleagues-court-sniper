"""Build a clean, shareable copy of the app.

Deliberately excludes everything personal: the browser profile, the
encrypted cookie cache, saved settings (which record your waiver
consent), logs, and the virtualenv. Those are per-person and, in the
case of the profile and cookies, are effectively account access.

Run: .venv\\Scripts\\python.exe tools\\build_share.py
"""
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT.parent / "badminton-sniper-share"

#: Copied verbatim.
INCLUDE_DIRS = ["src", "tests", "tools"]
INCLUDE_FILES = ["README.md", "requirements.txt", "run.bat",
                 "run-debug.bat", ".gitignore"]

#: Never shipped. Any of these leaking would hand over account access.
EXCLUDE_NAMES = {
    "config",            # session.bin, browser-profile, settings.json, logs
    ".venv", "venv",
    "__pycache__", ".pytest_cache", ".git",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".bin", ".tmp"}


def keep(path: Path) -> bool:
    if any(part in EXCLUDE_NAMES for part in path.parts):
        return False
    return path.suffix.lower() not in EXCLUDE_SUFFIXES


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    copied = []
    for name in INCLUDE_DIRS:
        src = ROOT / name
        if not src.exists():
            continue
        for path in sorted(src.rglob("*")):
            if path.is_dir() or not keep(path.relative_to(ROOT)):
                continue
            dest = OUT / path.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            copied.append(dest.relative_to(OUT))

    for name in INCLUDE_FILES:
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, OUT / name)
            copied.append(Path(name))

    (OUT / "setup.bat").write_text(SETUP_BAT, encoding="utf-8")
    (OUT / "SETUP.md").write_text(SETUP_MD, encoding="utf-8")
    copied += [Path("setup.bat"), Path("SETUP.md")]

    print(f"Built {OUT} with {len(copied)} files:\n")
    for rel in sorted(copied):
        print("  ", rel)

    archive = OUT.parent / "badminton-sniper-share.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(OUT.rglob("*")):
            if path.is_file():
                zf.write(path, Path("badminton-sniper") / path.relative_to(OUT))
    print(f"\nZipped to {archive} ({archive.stat().st_size / 1024:.0f} KB)")
    return 0


SETUP_BAT = """@echo off
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on your PATH.
  echo Install Python 3.11 or newer from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)

set "HERE=%CD%"
call :strlen LEN "%HERE%"
if %LEN% GTR 90 (
  echo.
  echo WARNING: this folder path is %LEN% characters long:
  echo   %HERE%
  echo.
  echo Playwright installs deeply nested files and Windows limits paths to 260
  echo characters, so setup will probably fail here. Move this folder somewhere
  echo shorter, such as your Desktop, and run setup.bat again.
  echo.
  pause
  exit /b 1
)

echo Creating the virtual environment...
python -m venv .venv
if errorlevel 1 goto failed

echo Installing dependencies...
.venv\\Scripts\\python.exe -m pip install --upgrade pip
.venv\\Scripts\\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed

echo Downloading the browser used for sign-in (about 150 MB)...
.venv\\Scripts\\python.exe -m playwright install chromium
if errorlevel 1 goto failed

echo Creating the desktop shortcut...
.venv\\Scripts\\python.exe tools\\make_shortcut.py --desktop

echo.
echo Setup finished. Launch it from the "Badminton Sniper" shortcut
echo ^(it carries the app icon^), or from run.bat.
pause
exit /b 0

:failed
echo.
echo Setup failed. Scroll up for the error.
echo If it mentions long paths, move this folder somewhere shorter and retry.
pause
exit /b 1

:strlen
set "s=%~2#"
set "len=0"
:strlen_loop
if "%s%"=="#" goto strlen_done
set "s=%s:~1%"
set /a len+=1
goto strlen_loop
:strlen_done
set "%~1=%len%"
exit /b 0
"""

SETUP_MD = """# Setup

For UT Dallas students booking Rec Center West badminton courts on IMLeagues.
Windows, Python 3.11 or newer.

## 1. Install

Double-click **`setup.bat`**. It creates a virtual environment, installs the
dependencies, and downloads the browser used for signing in (~150 MB). It only
needs to be done once.

If Python is missing, get it from <https://www.python.org/downloads/> and tick
**"Add python.exe to PATH"** during installation.

## 2. Run

Double-click **`run.bat`**. No console window appears -- it is a normal desktop
app. If nothing seems to happen, run **`run-debug.bat`** instead: same app, but
with a console showing the output. Everything is also written to
`config\\sniper.log`.

1. **Sign in** — a browser opens; log in with your own UTD NetID. The app never
   sees or stores your password.
2. **Waiver** — read and accept it once.
3. Pick a date, start time and court, then **Dry run** before arming anything.
4. **Arm** and leave the window open.

`README.md` has the full explanation of how it works and why.

## Notes

- This copy contains **no account data**. The sign-in session is created on your
  own machine, in `config\\`, the first time you sign in. That folder is personal
  — don't copy it to anyone else, it is effectively account access.
- The app is specific to UT Dallas's IMLeagues network. The school ID is in
  `src/constants.py`.
- UTD allows 3 reservations per account, each on a different court. IMLeagues
  enforces this server-side, and the app respects it.
"""


if __name__ == "__main__":
    raise SystemExit(main())
