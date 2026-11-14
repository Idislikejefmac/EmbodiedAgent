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

from ..limits import ACL_TOOL_TIMEOUT_S
from ..limits import NONCE_BYTES as NONCE_BYTES  # re-exported: the limits live in brain/limits.py (H-11)
from ..limits import TOKEN_BYTES as TOKEN_BYTES

log = logging.getLogger("brain.transport.auth")

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


# ---- token storage, private to the current user ---------------------------------------------------------
#
# Default location: %LOCALAPPDATA%\EmbodiedAgent\secrets (Windows) or ~/.local/share/embodied-agent/secrets,
# i.e. inside the user profile, whose parents other local accounts cannot modify. config.toml
# [paths] secrets_dir overrides it. The token file is opened once and its owner and access list are checked
# on that open handle before it is read, so the file checked is the file read.
#
# What this defends against: another local account reading the token, or planting a token file (which
# would then not be owned by, or private to, the current user). What it does not: code running as the
# current user (it can read the file anyway), administrators, or someone who can edit this program's own
# source (a repo under C:\ inherits "Authenticated Users: Modify"; see README).

TOKEN_FILE_NAME = "link_token"
_ELEVATED_HINT = ("it was probably created from an elevated (Run as administrator) prompt; run the brain from a "
                  "normal prompt, or delete the secrets folder and let it be recreated")


def default_secrets_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "EmbodiedAgent" / "secrets"
    return Path.home() / ".local" / "share" / "embodied-agent" / "secrets"


def token_path(store: Path) -> Path:
    return store / TOKEN_FILE_NAME


def _icacls() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "icacls.exe")


# Windows security descriptors through the Win32 API (no parsing of tool output).
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_ACCESS_ALLOWED_ACE_TYPE = 0
_ACCESS_DENIED_ACE_TYPE = 1
_INHERIT_ONLY_ACE = 0x08


class _ACL(ctypes.Structure):
    _fields_ = [("AclRevision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte), ("AclSize", ctypes.c_ushort),
                ("AceCount", ctypes.c_ushort), ("Sbz2", ctypes.c_ushort)]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", ctypes.c_ushort)]


_win = {}


def _api():
    if not _win:
        from ctypes import wintypes

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        pp = ctypes.POINTER(ctypes.c_void_p)
        advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, pp, pp, pp, pp, pp]
        advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, pp, pp, pp, pp, pp]
        advapi.GetSecurityInfo.restype = wintypes.DWORD
        advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, pp]
        advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        advapi.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR,
                                             ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                                             ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
        advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD)]
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        buf = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
        kernel.CloseHandle(token)
        _win.update(advapi=advapi, kernel=kernel, wt=wintypes, user_buf=buf,
                    user_sid=ctypes.c_void_p.from_buffer(buf).value)
    return _win


def _sid_name(sid: int) -> str:
    w = _api()
    wt = w["wt"]
    name, dom = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
    n, d, use = wt.DWORD(256), wt.DWORD(256), wt.DWORD()
    if not w["advapi"].LookupAccountSidW(None, sid, name, ctypes.byref(n), dom, ctypes.byref(d), ctypes.byref(use)):
        return "an unknown account"
    return f"{dom.value}\\{name.value}" if dom.value else name.value


def _descriptor_problems(owner: int, dacl: int) -> list[str]:
    w = _api()
    me = w["user_sid"]
    out = []
    if not owner or not w["advapi"].EqualSid(owner, me):
        who = _sid_name(owner) if owner else "nobody"
        hint = f" ({_ELEVATED_HINT})" if "administrators" in who.lower() else ""
        out.append(f"owned by {who}, not {current_user()}{hint}")
    if not dacl:
        return out + ["no access list (everyone has access)"]
    acl = _ACL.from_address(dacl)
    for i in range(acl.AceCount):
        ace = ctypes.c_void_p()
        if not w["advapi"].GetAce(dacl, i, ctypes.byref(ace)):
            out.append("unreadable access entry")
            continue
        hdr = _ACE_HEADER.from_address(ace.value)
        if hdr.AceType == _ACCESS_DENIED_ACE_TYPE or hdr.AceFlags & _INHERIT_ONLY_ACE:
            continue
        if hdr.AceType != _ACCESS_ALLOWED_ACE_TYPE:
            out.append(f"unsupported access entry type {hdr.AceType}")
            continue
        sid = ace.value + 8  # ACCESS_ALLOWED_ACE: header, mask, then the SID
        if ctypes.c_uint32.from_address(ace.value + 4).value and not w["advapi"].EqualSid(sid, me):
            out.append(f"{_sid_name(sid)} has access")
    return out


def _problems_win(get) -> list[str]:
    w = _api()
    owner, dacl, sd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    rc = get(_SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION, ctypes.byref(owner), None,
             ctypes.byref(dacl), None, ctypes.byref(sd))
    if rc != 0:
        return [f"could not read its security descriptor (error {rc})"]
    try:
        return _descriptor_problems(owner.value, dacl.value)
    finally:
        w["kernel"].LocalFree(sd)


def acl_problems(path: Path) -> list[str]:
    """Why `path` (file or folder) is not private to the current user (empty list if it is)."""
    if os.name != "nt":
        return _posix_problems(os.stat(path))
    w = _api()
    return _problems_win(lambda *a: w["advapi"].GetNamedSecurityInfoW(str(path), *a))


def fd_problems(fd: int) -> list[str]:
    """The same check on an open file, so the file checked is the file read."""
    if os.name != "nt":
        return _posix_problems(os.fstat(fd))
    import msvcrt

    w = _api()
    handle = msvcrt.get_osfhandle(fd)
    return _problems_win(lambda *a: w["advapi"].GetSecurityInfo(handle, *a))


def _posix_problems(st) -> list[str]:
    out = []
    if st.st_uid != os.getuid():
        out.append(f"owned by uid {st.st_uid}")
    if st.st_mode & 0o077:
        out.append(f"mode {oct(st.st_mode & 0o777)} lets others in")
    return out


def current_user() -> str:
    if os.name == "nt":
        return f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
    return str(os.getuid())


def _restrict_dir(path: Path) -> None:
    """Only the current user, no inherited entries. Raises TokenStoreError if it does not work."""
    if os.name != "nt":
        os.chmod(path, 0o700)
    else:
        r = subprocess.run([_icacls(), str(path), "/inheritance:r", "/grant:r", f"{current_user()}:(OI)(CI)F"],
                           capture_output=True, text=True, timeout=ACL_TOOL_TIMEOUT_S)
        if r.returncode != 0:
            raise TokenStoreError(f"icacls could not restrict {path} (exit {r.returncode}): "
                                  f"{(r.stderr or r.stdout).strip()[:200]}")
    problems = acl_problems(path)
    if problems:
        raise TokenStoreError(f"{path} is not private after restricting it: {'; '.join(problems)}")


def ensure_store(store: Path) -> Path:
    """The secrets folder, created private to the current user (files created inside inherit that)."""
    if not store.exists():
        store.mkdir(parents=True)
        _restrict_dir(store)
    problems = acl_problems(store)
    if problems:
        raise TokenStoreError(f"{store} is not private to {current_user()}: {'; '.join(problems)}. "
                              "Delete the folder and start the brain again to recreate it.")
    return store


def _read_checked(path: Path) -> bytes | None:
    """Open once, check owner and access on the handle, read from the same handle. None if absent."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        return None
    try:
        problems = fd_problems(fd)
        if problems:
            raise TokenStoreError(f"refusing to use {path}: {'; '.join(problems)}. Delete it and pair again.")
        data = os.read(fd, 256).decode("ascii", "replace")
    finally:
        os.close(fd)
    try:
        return parse_token(data)
    except ValueError:
        raise TokenStoreError(f"{path} does not hold a valid token; delete it and pair again") from None


def load_or_create_token(store: Path, *, rotate: bool = False, legacy: tuple[Path, ...] = ()) -> bytes:
    """The brain's pairing token from `store`, created (random) on first use or when `rotate` is set.

    Raises TokenStoreError for a folder or file that is not private to the current user, or a corrupt file.
    Token files at `legacy` paths (earlier P0-05 drafts kept it under the repo's data/ folder) are deleted.
    """
    for old in legacy:
        if old.exists():
            old.unlink()
            log.warning("deleted the old token file %s (no longer used)", old)
    ensure_store(store)
    path = token_path(store)
    if not rotate:
        token = _read_checked(path)
        if token is not None:
            return token
    token = secrets.token_bytes(TOKEN_BYTES)
    tmp = store / f".{TOKEN_FILE_NAME}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, (token.hex() + "\n").encode("ascii"))
        problems = fd_problems(fd)
    finally:
        os.close(fd)
    if problems:
        tmp.unlink()
        raise TokenStoreError(f"a new token file would not be private: {'; '.join(problems)}")
    os.replace(tmp, path)
    log.warning("%s link token in %s; pair the body again (python -m brain pair)",
                "rotated the" if rotate else "created a new", path)
    return token
