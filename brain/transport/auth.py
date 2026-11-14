"""Link authentication (protocol/v0.md section 4.2): a shared pairing token and a mutual HMAC challenge.

Each side sends a fresh 32-byte nonce in its hello. Each proves it holds the token with
HMAC-SHA256(token, label | sha256(text) | sha256(text) | ...) over the exact text frames of the handshake
so far (the transcript), so the proof covers both nonces, the session id and every hello field. The label
differs per role so one side's proof can never be reflected back as the other's. The token itself never
crosses the link and is never logged.

What this does NOT give: no session key and no per-message integrity. It authenticates the two endpoints
once, which is enough only where no third party is on the path (loopback, or a USB/debug-bridge forward).
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import logging
import os
import secrets
import subprocess
from pathlib import Path

log = logging.getLogger("brain.transport.auth")

TOKEN_BYTES = 32
NONCE_BYTES = 32
TOKEN_FILE_NAME = "link_token"
SECRETS_DIR_NAME = "secrets"
LABELS = {"body": b"embodied-agent v0 body proof", "brain": b"embodied-agent v0 brain proof"}


class AuthError(Exception):
    """The peer failed authentication, or this side has no token."""


class TokenStoreError(Exception):
    """The token file or its folder is not private to the current user."""


def new_nonce() -> str:
    return secrets.token_hex(NONCE_BYTES)


def digest(text: str) -> str:
    """sha256 of one handshake text frame exactly as sent and received (UTF-8), in hex."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def proof(token: bytes, role: str, transcript: list[str]) -> str:
    """The MAC that `role` sends: HMAC-SHA256(token, label | digest | digest ...), hex."""
    msg = b"|".join([LABELS[role], *(d.encode("ascii") for d in transcript)])
    return hmac.new(token, msg, hashlib.sha256).hexdigest()


def check(token: bytes, role: str, transcript: list[str], mac: object) -> bool:
    """Constant-time comparison of the peer's proof with the expected one."""
    if not isinstance(mac, str):
        return False
    want = proof(token, role, transcript)
    return hmac.compare_digest(want.encode("ascii"), mac.encode("ascii", "replace"))


def parse_token(text: str) -> bytes:
    t = text.strip().lower()
    if len(t) != TOKEN_BYTES * 2 or any(c not in "0123456789abcdef" for c in t):
        raise ValueError("a link token is 64 hex characters")
    return bytes.fromhex(t)


# ---- token storage: data/secrets/link_token, private to the current user ------------------------------

def current_user() -> str:
    if os.name == "nt":
        return f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
    return str(os.getuid())


def _owner_windows(path: Path) -> str:
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32")
    pp = ctypes.POINTER(ctypes.c_void_p)
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, pp, pp, pp, pp, pp]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR,
                                         ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                                         ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    sid, sd = ctypes.c_void_p(), ctypes.c_void_p()
    rc = advapi.GetNamedSecurityInfoW(str(path), 1, 1, ctypes.byref(sid), None, None, None, ctypes.byref(sd))
    if rc != 0:
        raise OSError(rc, "GetNamedSecurityInfoW failed")
    try:
        name, dom = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        n, d, use = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
        if not advapi.LookupAccountSidW(None, sid, name, ctypes.byref(n), dom, ctypes.byref(d), ctypes.byref(use)):
            raise OSError(ctypes.get_last_error(), "LookupAccountSidW failed")
        return f"{dom.value}\\{name.value}"
    finally:
        kernel.LocalFree(sd)


def acl_problems(path: Path) -> list[str]:
    """Why `path` is not private to the current user (empty list if it is)."""
    me = current_user().lower()
    if os.name != "nt":
        st = path.stat()
        out = []
        if st.st_uid != os.getuid():
            out.append(f"owned by uid {st.st_uid}")
        if st.st_mode & 0o077:
            out.append(f"mode {oct(st.st_mode & 0o777)} lets others in")
        return out
    out = []
    try:
        owner = _owner_windows(path)
        if owner.lower() != me:
            out.append(f"owned by {owner}")
    except OSError as e:
        out.append(f"could not read the owner: {e}")
    r = subprocess.run(["icacls", str(path)], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return out + [f"icacls failed ({r.returncode})"]
    lines = [ln for ln in r.stdout.splitlines() if ":(" in ln]
    for ln in lines:
        entry = ln.replace(str(path), "", 1).strip()
        principal = entry.split(":(", 1)[0].strip()
        if principal.lower() != me:
            out.append(f"{principal} has access")
    if not lines:
        out.append("no access entries found")
    return out


def _restrict(path: Path, *, directory: bool) -> None:
    """Only the current user, no inherited entries. Raises TokenStoreError if it does not work."""
    if os.name != "nt":
        os.chmod(path, 0o700 if directory else 0o600)
    else:
        grant = f"{current_user()}:{'(OI)(CI)F' if directory else 'F'}"
        r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            raise TokenStoreError(f"icacls could not restrict {path} (exit {r.returncode}): "
                                  f"{(r.stderr or r.stdout).strip()[:200]}")
    problems = acl_problems(path)
    if problems:
        raise TokenStoreError(f"{path} is not private after restricting it: {'; '.join(problems)}")


def secrets_dir(data_dir: Path) -> Path:
    """data_dir/secrets, created private to the current user (files created inside inherit that)."""
    d = data_dir / SECRETS_DIR_NAME
    if not d.exists():
        d.mkdir(parents=True)
        _restrict(d, directory=True)
    problems = acl_problems(d)
    if problems:
        raise TokenStoreError(f"{d} is not private to {current_user()}: {'; '.join(problems)}. "
                              "Delete the folder and start the brain again to recreate it.")
    return d


def token_path(data_dir: Path) -> Path:
    return data_dir / SECRETS_DIR_NAME / TOKEN_FILE_NAME


def load_or_create_token(data_dir: Path, *, rotate: bool = False) -> bytes:
    """The brain's pairing token, created (random) on first use or when `rotate` is set.

    Refuses (TokenStoreError) a token file or folder that anyone but the current user owns or can open,
    because a planted or readable token would let someone else act as the body or the brain.
    """
    legacy = data_dir / TOKEN_FILE_NAME
    if legacy.exists():  # P0-05 first draft kept it directly in data/
        legacy.unlink()
        log.warning("removed the old token file %s; a new token is used, pair the body again", legacy)
    d = secrets_dir(data_dir)
    path = d / TOKEN_FILE_NAME
    if path.exists() and not rotate:
        problems = acl_problems(path)
        if problems:
            raise TokenStoreError(f"refusing to use {path}: {'; '.join(problems)}. Delete it and pair again.")
        return parse_token(path.read_text(encoding="ascii"))
    token = secrets.token_bytes(TOKEN_BYTES)
    tmp = d / f".{TOKEN_FILE_NAME}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # inherits the folder's private ACL
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(token.hex() + "\n")
    problems = acl_problems(tmp)
    if problems:
        tmp.unlink()
        raise TokenStoreError(f"new token file would not be private: {'; '.join(problems)}")
    os.replace(tmp, path)
    log.warning("%s link token in %s; pair the body again (python -m brain pair)",
                "rotated the" if rotate else "created a new", path)
    return token
