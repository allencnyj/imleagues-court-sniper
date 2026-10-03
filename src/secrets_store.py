"""Windows DPAPI wrapper, used to encrypt the cached session cookies.

The browser profile already holds the login, and Chromium protects it
with DPAPI. Caching the same cookies as a plain JSON file so the app can
skip a browser launch at startup would be a step down in protection, so
they get the same treatment: CryptProtectData with the current user's
key, meaning only this Windows account on this machine can read them.

No password is ever stored -- only a session cookie, which expires.
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)

#: Tied to this app so a blob cannot be replayed into another program.
_ENTROPY = b"imleagues-badminton-sniper/v1"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


try:
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    AVAILABLE = True
except (OSError, AttributeError):  # pragma: no cover - non-Windows
    _crypt32 = _kernel32 = None
    AVAILABLE = False


def _make(data: bytes):
    """Return (blob, buffer). The buffer must stay referenced during the call."""
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _take(out: _Blob) -> bytes:
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> bytes:
    """Encrypt for the current Windows user. Raises OSError on failure."""
    if not AVAILABLE:
        raise OSError("DPAPI unavailable on this platform")
    in_blob, _in_buf = _make(data)
    ent_blob, _ent_buf = _make(_ENTROPY)
    out = _Blob()
    ok = _crypt32.CryptProtectData(ctypes.byref(in_blob), None, ctypes.byref(ent_blob),
                                   None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    return _take(out)


def unprotect(blob: bytes) -> bytes:
    """Decrypt a blob produced by protect(). Raises OSError on failure."""
    if not AVAILABLE:
        raise OSError("DPAPI unavailable on this platform")
    in_blob, _in_buf = _make(blob)
    ent_blob, _ent_buf = _make(_ENTROPY)
    out = _Blob()
    ok = _crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, ctypes.byref(ent_blob),
                                     None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    return _take(out)


def write_secret(path: Path, data: bytes) -> bool:
    """Encrypt and store. Returns False (and writes nothing) on failure."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(protect(data))
        tmp.replace(path)
        return True
    except OSError as exc:
        log.warning("could not store secret at %s: %s", path, exc)
        return False


def read_secret(path: Path) -> bytes | None:
    """Load and decrypt, or None if missing/unreadable."""
    if not path.exists():
        return None
    try:
        return unprotect(path.read_bytes())
    except OSError as exc:
        log.info("could not read secret at %s (%s); ignoring", path, exc)
        return None


def clear_secret(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
