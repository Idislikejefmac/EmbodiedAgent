"""PhoneBody: the one place in the brain allowed to know about the Android phone.

Everything else under brain/ must stay body-agnostic; tests/test_boundary_lint.py enforces this, and
only brain/body/ and brain/__main__.py may import this module. Other code gets a body from
`brain.body.create_body()` and sees only the `Body` interface.

PhoneBody is the protocol v0 LinkBody plus the one thing that is specific to this phone: how it
reaches the laptop. The laptop firewall blocks inbound connections from the phone, so the phone
reaches the brain through `adb reverse tcp:<port> tcp:<port>` over wireless debugging and dials
127.0.0.1:<port>. Status and events work today; the other methods raise NotYetImplemented until
their cards (see LinkBody), except navigate, which a body carried by a person will never have.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .base import NotSupported
from .link import LinkBody
from .types import Capability, NavGoal

log = logging.getLogger("brain.body.phone")

LINK_RECHECK_S = 60


def find_adb() -> str | None:
    """adb from ANDROID_HOME / ANDROID_SDK_ROOT, the default SDK folder, or PATH."""
    candidates = []
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var):
            candidates.append(Path(os.environ[var]) / "platform-tools")
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk" / "platform-tools")
    for folder in candidates:
        for name in ("adb.exe", "adb"):
            if (folder / name).is_file():
                return str(folder / name)
    return shutil.which("adb")


APP_ID = "com.jmd.embodied.body"
DEBUG_RECEIVER = f"{APP_ID}/.DebugCommandReceiver"
DEBUG_ACTION = f"{APP_ID}.debug."


def debug_broadcast_args(action: str, *extras: str) -> list[str]:
    """`adb shell am broadcast` arguments for the body's debug-only command receiver.

    The receiver exists only in debug builds and requires android.permission.DUMP. DUMP is a
    signature|privileged|development permission: the adb shell holds it, and so can preinstalled privileged
    apps and any app that declares it and is granted it with `adb shell pm grant`; ordinary installed apps
    cannot obtain it on their own. --allow-background-activity-starts lets the receiver start the link's
    foreground service while the app is in the background (Android 12+ refuses it otherwise).
    """
    return ["shell", "am", "broadcast", "--allow-background-activity-starts", "-n", DEBUG_RECEIVER,
            "-a", DEBUG_ACTION + action, *extras]


def pair(token: bytes) -> tuple[bool, str]:
    """Give the phone app the brain's link token (protocol/v0.md section 4.2). Never logs the token.

    The token is written to adb's stdin, never onto a laptop command line, so other processes and
    process-creation auditing on the laptop do not see it. On the phone a shell `read` receives it and
    passes it to `am broadcast`, so it is in that one `am` process's arguments for a moment, visible to
    the shell user and root only (apps cannot see other apps' processes).
    """
    adb = find_adb()
    if adb is None:
        return False, "adb not found (set ANDROID_HOME or install platform-tools)"
    script = 'read -r t; ' + " ".join(debug_broadcast_args("PAIR")[1:]) + ' --es token "$t"'
    try:
        out = subprocess.run([adb, "shell", script], input=token.hex() + "\n",
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"pairing failed: {e}"
    text = (out.stdout or "") + (out.stderr or "")
    if out.returncode == 0 and 'data="paired"' in text:
        return True, "phone paired with this brain's link token"
    return False, ("pairing failed (is a debug build of the body app installed?): "
                   + text.replace(token.hex(), "<token>").strip()[:300])


def ensure_link(port: int) -> tuple[bool, str]:
    """Run `adb reverse tcp:<port> tcp:<port>`. Returns (ok, human-readable message); never raises."""
    adb = find_adb()
    if adb is None:
        return False, "adb not found (set ANDROID_HOME or install platform-tools)"
    try:
        out = subprocess.run([adb, "reverse", f"tcp:{port}", f"tcp:{port}"],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"adb reverse failed: {e}"
    if out.returncode != 0:
        return False, f"adb reverse failed: {(out.stderr or out.stdout).strip()}"
    return True, f"phone link up: adb reverse tcp:{port} tcp:{port}"


async def keep_link(port: int, logger: logging.Logger = log, recheck_s: float = LINK_RECHECK_S) -> None:
    """Re-run the reverse port forward every minute, so a dropped phone link heals by itself. Runs until
    cancelled; logs only when the state changes."""
    last = None
    while True:
        ok, message = await asyncio.to_thread(ensure_link, port)
        if ok != last:
            (logger.info if ok else logger.warning)(message)
            last = ok
        await asyncio.sleep(recheck_s)


class PhoneBody(LinkBody):
    """The Android phone as a Body. `phone_link=False` skips the adb port forward (tests, or a phone
    that reaches the brain some other way)."""

    def __init__(self, *, token: bytes, host: str = "127.0.0.1", port: int = 8765, phone_link: bool = True,
                 **options: float):
        super().__init__(token=token, host=host, port=port, name="phone", **options)
        self._phone_link = phone_link
        self._link_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await super().start()
        if self._phone_link and self._link_task is None:
            self._link_task = asyncio.create_task(keep_link(self.port))

    async def close(self) -> None:
        if self._link_task is not None:
            self._link_task.cancel()
            self._link_task = None
        await super().close()

    async def navigate(self, goal: NavGoal) -> str:
        raise NotSupported(Capability.NAVIGATE, "this body is carried by a person and cannot move itself; "
                                                "the guidance loop gives spoken directions instead")
