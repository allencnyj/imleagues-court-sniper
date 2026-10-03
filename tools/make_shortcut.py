"""Create a Windows shortcut with the shuttlecock icon.

Setting the icon at runtime (WM_SETICON) fixes the title bar, Alt-Tab and
the taskbar button of a *running* window. It cannot change how the
launcher itself looks: a .bat file always shows the generic batch-script
icon in Explorer, on the Desktop, and when pinned. Only a real .lnk can
carry a custom icon there.

Run: .venv\\Scripts\\python.exe tools\\make_shortcut.py [--desktop]
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from config import APP_ICON  # noqa: E402
from tray import make_app_icon  # noqa: E402

NAME = "Badminton Sniper.lnk"

PS = """
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut('{lnk}')
$s.TargetPath       = '{target}'
$s.Arguments        = '"{script}"'
$s.WorkingDirectory = '{cwd}'
$s.IconLocation     = '{icon},0'
$s.Description      = 'Book UTD badminton courts on IMLeagues'
$s.Save()
"""


def create(dest: Path) -> bool:
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        print(f"  {pythonw} not found - run setup.bat first")
        return False
    if not APP_ICON.exists() and not make_app_icon(APP_ICON):
        print("  could not generate the icon")
        return False

    script = PS.format(lnk=dest, target=pythonw, script=ROOT / "src" / "main.py",
                       cwd=ROOT, icon=APP_ICON)
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True)
    if result.returncode != 0 or not dest.exists():
        print("  failed:", (result.stderr or result.stdout).strip()[:300])
        return False
    print(f"  created {dest}")
    return True


def main() -> int:
    ok = create(ROOT / NAME)
    if "--desktop" in sys.argv:
        desktop = Path.home() / "Desktop"
        if desktop.is_dir():
            ok = create(desktop / NAME) and ok
        else:
            print(f"  {desktop} not found; skipped")
    if ok:
        print("\nLaunch from the shortcut to get the shuttlecock everywhere: "
              "Explorer, Desktop, taskbar and pinned items.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
