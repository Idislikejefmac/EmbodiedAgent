"""Profiles through the real command line, in a CHILD PROCESS (style of tests/test_cli_end_to_end.py).

Each child runs `python -m brain` with the repository's config.toml, .env and profiles/ replaced by a temporary
folder (the wrapper below points brain.config and brain.profiles at it) and LOCALAPPDATA at another, so the
real profiles, data and token store are never touched. A canary key sits in the child's environment AND in the
fake root .env: no output may ever show it, and a profile must never use it.

Every child must exit 0, 1 or 2 (2 for any usage, config or profile problem), print no traceback, and finish in
bounded time. Hostile profile names, a missing profile, a corrupt profile config, a misspelt key, a profile
folder that is a file, a read-only config, and a folder that cannot be made are all covered.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from brain.config import REPO_ROOT

pytestmark = pytest.mark.chaos

TIMEOUT_S = 60.0
ENV_CANARY = "sk-ant-canary-ENVIRONMENT-1111111111"
ROOT_CANARY = "sk-ant-canary-ROOTDOTENV-1111111111"
OWN_KEY = "sk-ant-canary-OWNPROFILE-1111111111"
WRAPPER = f"""\
import sys
sys.path.insert(0, {str(REPO_ROOT)!r})
from pathlib import Path
import brain.config as c
import brain.profiles as p
base = Path(sys.argv[1])
c.DEFAULT_CONFIG = base / "config.toml"
c.DEFAULT_ENV = base / "root.env"
p.PROFILES_ROOT = base / "profiles"
from brain import __main__ as m
raise SystemExit(m.main(sys.argv[2:]))
"""
LONG = "x" * 20_000
CONTROL = "a\x01\x07\x1b[2Jb"
HOSTILE_NAMES = ["..", "../x", "..\\x", "a/b", "C:\\Windows", "\\\\host\\share", "Mary", "con", "CON", "aux", "nul", "com1",
                 "lpt9", "default", "profiles", "data", "config", "a.", "a b", "\u0430lice", "\uff4dary", "mary\u200b",
                 CONTROL, LONG, "-x", "%APPDATA%", "~", "caf\u00e9"]


class Box:
    def __init__(self, tmp: Path):
        self.base = tmp / "box"
        self.base.mkdir()
        (self.base / "config.toml").write_text('[network]\nport = 8765\n[paths]\ndata_dir = "data"\n', encoding="utf-8")
        (self.base / "root.env").write_text(f"ANTHROPIC_API_KEY={ROOT_CANARY}\n", encoding="utf-8")
        self.script = tmp / "run_brain.py"
        self.script.write_text(WRAPPER, encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")}
        self.env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                        ANTHROPIC_API_KEY=ENV_CANARY, LOCALAPPDATA=str(tmp / "local"))
        self.profiles = self.base / "profiles"

    def brain(self, *argv: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        p = subprocess.run([sys.executable, str(self.script), str(self.base), *argv], input=stdin, capture_output=True,
                           text=True, timeout=TIMEOUT_S, cwd=self.base, env=self.env, encoding="utf-8", errors="replace")
        assert "Traceback" not in p.stdout + p.stderr, (argv, p.stdout[-600:], p.stderr[-600:])
        assert ENV_CANARY not in p.stdout + p.stderr and ROOT_CANARY not in p.stdout + p.stderr, argv
        assert OWN_KEY not in p.stdout + p.stderr, argv
        return p

    def create(self, name: str) -> Path:
        assert self.brain("profile", "create", name).returncode == 0
        return self.profiles / name


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def snapshot(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")} if root.exists() else set()


# ---- hostile names, everywhere a name can be given ---------------------------------------------------------------


@pytest.mark.parametrize("name", HOSTILE_NAMES, ids=lambda n: repr(n)[:24])
def test_hostile_profile_names_are_refused_by_every_form(box, name):
    before = snapshot(box.base)
    forms = [("info", f"--profile={name}"), ("profile", "create", name)]
    if HOSTILE_NAMES.index(name) % 4 == 0:  # the other commands share the same gate; a sample keeps this quick
        forms += [("chat", "--profile", name), ("serve", "--profile", name), ("profile", "show", name)]
    for argv in forms:
        p = box.brain(*argv)
        assert p.returncode == 2, (argv, p.returncode, p.stderr[-300:])
        assert len(p.stderr) < 2000
    assert snapshot(box.base) == before  # nothing created, whatever the name


def test_missing_or_repeated_profile_option(box):
    for argv, text in ((("info", "--profile"), "name is needed"), (("--profile", "--live", "chat"), "name is needed"),
                       (("info", "--profile", "a", "--profile", "b"), "twice"), (("info", "--profile="), "required"),
                       (("profile", "list", "--profile", "mary"), "own argument"), (("chaos", "--profile", "mary"), "sandbox")):
        p = box.brain(*argv)
        assert p.returncode == 2 and text in p.stderr, (argv, p.stderr[-300:])


def test_the_profile_option_is_listed_on_every_command(box):
    for cmd in ("info", "serve", "probe", "chat", "watch", "health", "demo-trace", "body-check", "pair"):
        p = box.brain(cmd, "--help")
        assert p.returncode == 0 and "--profile" in p.stdout, cmd


# ---- a missing, corrupt or odd profile -------------------------------------------------------------------------------


def test_missing_profile(box):
    for cmd in ("info", "chat", "serve", "health", "watch", "demo-trace", "body-check", "pair", "probe"):
        p = box.brain(cmd, "--profile", "ghost")
        assert p.returncode == 2 and "does not exist; create it with: python -m brain profile create ghost" in p.stderr, cmd
    assert not box.profiles.exists()


def test_corrupt_profile_config_names_the_file(box):
    folder = box.create("mary")
    (folder / "config.toml").write_text("[network\nport = 1", encoding="utf-8")
    p = box.brain("info", "--profile", "mary")
    assert p.returncode == 2 and "not valid TOML" in p.stderr and str(folder / "config.toml") in p.stderr
    (folder / "config.toml").write_bytes(b"\xff\xfe\x00bad")
    assert box.brain("chat", "--profile", "mary").returncode == 2
    (folder / "config.toml").write_text("", encoding="utf-8")  # an empty config is valid: all defaults
    p = box.brain("info", "--profile", "mary")
    assert p.returncode == 0 and "port 8765" in p.stdout  # (the defaults, not Jeff's file)
    (folder / "config.toml").unlink()
    p = box.brain("info", "--profile", "mary")
    assert p.returncode == 2 and "no config.toml" in p.stderr


def test_unknown_keys_get_a_suggestion(box):
    folder = box.create("mary")
    (folder / "config.toml").write_text("[caps]\nsesion_usd = 1.0\n", encoding="utf-8")
    p = box.brain("info", "--profile", "mary")
    assert p.returncode == 2 and "did you mean 'session_usd'" in p.stderr
    (folder / "config.toml").write_text("[netwrk]\nport = 8766\n", encoding="utf-8")
    p = box.brain("chat", "--profile", "mary")
    assert p.returncode == 2 and "did you mean 'network'" in p.stderr
    (folder / "config.toml").write_text('[paths]\ndata_dir = "../../other"\n', encoding="utf-8")
    assert box.brain("info", "--profile", "mary").returncode == 2


def test_a_profile_folder_that_is_a_file(box):
    box.profiles.mkdir()
    (box.profiles / "mary").write_text("a file", encoding="utf-8")
    p = box.brain("info", "--profile", "mary")
    assert p.returncode == 2 and "is a file, not a folder" in p.stderr
    p = box.brain("profile", "create", "mary")
    assert p.returncode == 2 and "already exists" in p.stderr
    assert (box.profiles / "mary").read_text(encoding="utf-8") == "a file"
    assert box.brain("profile", "list").returncode == 0  # a file is simply not a profile


def test_a_profile_config_that_is_a_folder_or_a_secret_file_that_is_a_folder(box):
    folder = box.create("mary")
    (folder / "config.toml").unlink()
    (folder / "config.toml").mkdir()
    assert box.brain("info", "--profile", "mary").returncode == 2
    (folder / "config.toml").rmdir()
    box.create("jo")
    (box.profiles / "jo" / ".env").unlink()
    (box.profiles / "jo" / ".env").mkdir()
    p = box.brain("info", "--profile", "jo")
    assert p.returncode == 2


def test_read_only_files_and_a_folder_that_cannot_be_made(box):
    folder = box.create("mary")
    for name in ("config.toml", ".env"):
        os.chmod(folder / name, stat.S_IREAD)
    try:
        p = box.brain("info", "--profile", "mary")
        assert p.returncode == 0 and "ANTHROPIC_API_KEY: missing" in p.stdout
    finally:
        for name in ("config.toml", ".env"):
            os.chmod(folder / name, stat.S_IREAD | stat.S_IWRITE)
    (box.profiles).rename(box.base / "moved")
    (box.base / "profiles").write_text("a file where the profiles folder belongs", encoding="utf-8")
    p = box.brain("profile", "create", "jo")
    assert p.returncode == 2 and "cannot" in p.stderr
    assert box.brain("profile", "list").returncode == 0
    assert box.brain("info", "--profile", "mary").returncode == 2


def test_a_data_folder_that_is_a_file_ends_cleanly(box):
    folder = box.create("mary")
    (folder / "data").rmdir()
    (folder / "data").write_text("a file", encoding="utf-8")
    p = box.brain("chat", "--profile", "mary", stdin="/quit\n")
    assert p.returncode in (1, 2)  # a clean stop: a message, never a traceback
    assert box.brain("info", "--profile", "mary").returncode == 0  # info only reads


# ---- working profiles --------------------------------------------------------------------------------------------------


def test_create_list_show_info_through_the_cli(box):
    a = box.brain("profile", "create", "mary")
    assert a.returncode == 0 and "port 8766" in a.stdout and "prompt capture off" in a.stdout
    assert box.brain("profile", "create", "jo").returncode == 0
    again = box.brain("profile", "create", "mary")
    assert again.returncode == 2 and "already exists" in again.stderr
    listing = box.brain("profile", "list").stdout
    assert "jo: port 8767" in listing and "mary: port 8766" in listing and "ANTHROPIC_API_KEY missing" in listing
    show = box.brain("profile", "show", "mary").stdout
    assert "prompt capture: off" in show and "live calls off" in show
    infos = {n: box.brain("info", "--profile", n).stdout for n in ("mary", "jo")}
    assert "profiles" in infos["mary"] and "port 8766" in infos["mary"] and "port 8767" in infos["jo"]
    assert "EmbodiedAgent" in infos["mary"] and infos["mary"] != infos["jo"]
    default = box.brain("info").stdout  # the default profile: Jeff's setup, keys from the environment
    assert "key ANTHROPIC_API_KEY: present" in default and "profile:" not in default
    assert "key ANTHROPIC_API_KEY: missing" in infos["mary"]  # same canary in the environment: not hers


def test_a_profile_chat_writes_only_in_its_own_folder(box):
    folder = box.create("mary")
    other = box.create("jo")
    p = box.brain("chat", "--profile", "mary", stdin="hello\n/quit\n")
    assert p.returncode == 0 and "prompt capture off" in p.stdout
    assert any(folder.joinpath("data").rglob("state.json")) and any(folder.joinpath("data", "logs").glob("events-*.jsonl"))
    assert not any(other.joinpath("data").iterdir()) and not (box.base / "data").exists()
    assert not (folder / "data" / "capture").exists()  # capture is off unless the person turns it on


def test_live_chat_for_a_profile_needs_its_own_key(box):
    folder = box.create("mary")
    p = box.brain("chat", "--profile", "mary", "--live", stdin="/quit\n")
    assert p.returncode == 2 and "allow_live_calls" in p.stdout
    text = (folder / "config.toml").read_text(encoding="utf-8").replace("allow_live_calls = false", "allow_live_calls = true")
    (folder / "config.toml").write_text(text, encoding="utf-8")
    p = box.brain("chat", "--profile", "mary", "--live", stdin="/quit\n")
    assert p.returncode == 2 and "no ANTHROPIC_API_KEY in" in p.stdout and "not used for a profile" in p.stdout
    # with her own key but no network and no allowed host in this sandbox the start still stops cleanly
    (folder / ".env").write_text(f"ANTHROPIC_API_KEY={OWN_KEY}\n", encoding="utf-8")
    p = box.brain("info", "--profile", "mary")
    assert p.returncode == 0 and "key ANTHROPIC_API_KEY: present" in p.stdout


def test_demo_trace_and_health_use_the_profiles_folder(box):
    folder = box.create("mary")
    p = box.brain("demo-trace", "--profile", "mary")
    assert p.returncode == 0
    assert (folder / "data" / "health.json").exists() and not (box.base / "data").exists()
    assert box.brain("health", "--profile", "mary").returncode in (0, 1)
    assert box.brain("health", "--profile", "jo").returncode == 2
