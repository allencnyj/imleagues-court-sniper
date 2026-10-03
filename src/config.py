"""On-disk settings.

Everything lives under config/ next to the project, which is also where
the browser profile goes. No credentials are stored here -- the browser
profile holds the session, and the app never sees a password.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from accounts import Account
from models import Target
from payload import Consent
from planner import Request
from scheduler import FireSettings

log = logging.getLogger(__name__)


def _project_root() -> Path:
    """Where config/ lives.

    Frozen by PyInstaller, __file__ points inside a temporary extraction
    directory that is wiped on exit, so settings and the saved session
    would vanish between runs. Anchor to the executable instead.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _project_root()
CONFIG_DIR = PROJECT_ROOT / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
PROFILE_DIR = CONFIG_DIR / "browser-profile"
#: DPAPI-encrypted cache of the session cookies, so startup can verify the
#: session over plain HTTP instead of launching a browser to read them.
COOKIE_CACHE = CONFIG_DIR / "session.bin"
LOG_PATH = CONFIG_DIR / "sniper.log"
#: Generated on first run; not shipped, so it regenerates per install.
APP_ICON = CONFIG_DIR / "app.ico"
#: Per-account browser profiles and cookie caches live here.
ACCOUNTS_DIR = CONFIG_DIR / "accounts"

#: The first account keeps the legacy single-account id, so an existing
#: install's saved session is migrated forward rather than orphaned.
PRIMARY_ACCOUNT_ID = "primary"


def account_paths(account_id: str) -> tuple[Path, Path]:
    """(browser profile dir, cookie-cache file) for one account."""
    base = ACCOUNTS_DIR / account_id
    return base / "browser-profile", base / "session.bin"


def _migrate_primary_session() -> None:
    """Carry a pre-multi-account session into the primary account slot.

    Only the tiny cookie cache is copied; that alone re-mints the full
    session via the seeded-cookie path, so the live browser profile is
    left untouched (and unlocked).
    """
    _, primary_cache = account_paths(PRIMARY_ACCOUNT_ID)
    if COOKIE_CACHE.exists() and not primary_cache.exists():
        try:
            primary_cache.parent.mkdir(parents=True, exist_ok=True)
            primary_cache.write_bytes(COOKIE_CACHE.read_bytes())
            log.info("migrated legacy session into the primary account")
        except OSError as exc:
            log.warning("could not migrate legacy session: %s", exc)


@dataclass
class AppConfig:
    #: Timing knobs are global to the whole run.
    fire: FireSettings = field(default_factory=FireSettings)
    #: One or more accounts, each owning its own targets. There is always
    #: at least one (the primary), created on first load.
    accounts: list[Account] = field(default_factory=list)
    #: "auto": the user lists times + how many courts, and the planner
    #: decides which account books which court. "manual": each account's
    #: own target rows are used as-is.
    mode: str = "auto"
    #: The auto-mode wish list. Global, not per account -- that is the
    #: whole point of it.
    requests: list[Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.accounts:
            self.accounts = [Account(id=PRIMARY_ACCOUNT_ID)]

    # --- account helpers ---------------------------------------------

    def account(self, account_id: str) -> Account | None:
        return next((a for a in self.accounts if a.id == account_id), None)

    @property
    def primary(self) -> Account:
        return self.accounts[0]

    def add_account(self, label: str = "") -> Account:
        existing = {a.id for a in self.accounts}
        n = 2
        while f"account{n}" in existing:
            n += 1
        acc = Account(id=f"account{n}", label=label)
        self.accounts.append(acc)
        return acc

    def save(self, path: Path | None = None) -> None:
        # Resolve at call time, not bind at def time: tests reassign
        # SETTINGS_PATH, and a bound default would still point at the real
        # config -- which once let a test write into the live settings.
        path = path or SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fire": self.fire.to_json(),
            "accounts": [a.to_json() for a in self.accounts],
            "mode": self.mode,
            "requests": [r.to_json() for r in self.requests],
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        path = path or SETTINGS_PATH
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not read %s (%s); starting fresh", path, exc)
            return cls()

        if data.get("accounts"):
            accounts = [Account.from_json(d) for d in data["accounts"]]
        else:
            # Migrate a single-account config: its top-level consent,
            # form model, and targets become the primary account, and its
            # saved session is carried into the primary account slot.
            targets = [t for t in (Target.from_json(d)
                                   for d in data.get("targets") or []) if t]
            accounts = [Account(
                id=PRIMARY_ACCOUNT_ID,
                consent=Consent.from_json(data.get("consent")),
                form_model=data.get("form_model") or {},
                requirements_seen=data.get("requirements_seen") or {},
                targets=targets,
            )]
            _migrate_primary_session()

        requests = [r for r in (Request.from_json(d)
                                for d in data.get("requests") or []) if r]
        mode = "manual" if data.get("mode") == "manual" else "auto"
        return cls(fire=FireSettings.from_json(data.get("fire")), accounts=accounts,
                   mode=mode, requests=requests)
