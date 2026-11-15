"""Profiles (docs/profiles.md): one setup per person on one machine.

Name rules and hostile names, confinement under profiles/, the scaffold (safe defaults, a free port each, never
overwritten), KEY ISOLATION (a profile never sees the environment's key or the root .env: canary keys prove it),
per-profile separation of events, state, spend and capture, distinct token stores, two brains on two ports at
once, and the git-ignore rule for profiles/. Child-process runs of the commands are in tests/test_profiles_cli.py.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest

from brain import __main__ as main_mod
from brain import config as C
from brain import profiles as P
from brain.agent.budget import SpendGuard, load_prices
from brain.agent.model import Usage
from brain.body.sim import SimBody
from brain.chat import ChatOptions, StartRefused, live_model, run_chat
from brain.config import ConfigError, Profile, ProfileError
from brain.egress import EgressDenied, EgressPolicy
from brain.limits import PORT_MAX, PORT_MIN, PROFILE_NAME_MAX_CHARS, PROFILE_PORT_FIRST, PROFILES_MAX
from brain.obs.state import StateStore
from brain.transport import auth
from brain.transport.client import TestBodyClient
from brain.transport.server import BrainServer
from fakes import run

REPO = Path(__file__).resolve().parent.parent
ENV_CANARY = "sk-ant-canary-ENVIRONMENT-0000000000"  # clearly fake; stands for another person's key in the environment
ROOT_CANARY = "sk-ant-canary-ROOTDOTENV-0000000000"  # ... and in Jeff's root .env
OWN_KEY = "sk-ant-canary-OWNPROFILE-0000000000"
KEY = "ANTHROPIC_API_KEY"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """profiles/ in a temp folder, a root config and root .env holding a canary key, canary key in the
    environment, and a private LOCALAPPDATA: nothing here can touch the real profiles/ or token store."""
    monkeypatch.setattr(P, "PROFILES_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(C, "DEFAULT_CONFIG", tmp_path / "config.toml")
    monkeypatch.setattr(C, "DEFAULT_ENV", tmp_path / "root.env")
    (tmp_path / "root.env").write_text(f"{KEY}={ROOT_CANARY}\n", encoding="utf-8")
    monkeypatch.setenv(KEY, ENV_CANARY)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    return tmp_path


def put_key(name: str, value: str = OWN_KEY) -> None:
    (P.PROFILES_ROOT / name / ".env").write_text(f"{KEY}={value}\n", encoding="utf-8")


def set_config(name: str, **changes: str) -> None:
    """Rewrite lines of a profile's config.toml: `key = value` text for each key."""
    path = P.PROFILES_ROOT / name / "config.toml"
    text = path.read_text(encoding="utf-8")
    for key, line in changes.items():
        text, n = re.subn(rf"^{key} = .*$", lambda _m, k=key, v=line: f"{k} = {v}", text, flags=re.M)
        assert n == 1, key
    path.write_text(text, encoding="utf-8")


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---- names -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["mary", "a", "jeff-2", "x_y", "z9", "a" * PROFILE_NAME_MAX_CHARS, "wife", "console2",
                                  "com10", "lpt0", "nulx", "data2", "default2"])
def test_good_names_pass(name):
    assert P.validate_name(name) == name


HOSTILE = ["", " ", "Mary", "MARY", "1abc", "-abc", "_abc", "a b", "a.", "a ", " a", "a\n", "a\r\n", "a" * (PROFILE_NAME_MAX_CHARS + 1),
           "a" * 10_000, "..", ".", "...", "../x", "..\\x", "a/b", "a\\b", "/abs", "C:\\Windows", "C:", "c:x", "\\\\host\\share",
           "~", "%APPDATA%", "$HOME", "a:b", "a;b", "a*b", "a?b", "a|b", "a<b", "a\x00b", "a\x1bb", "a\u200bb", "a\u202eb",
           "\ufeffa", "\u0430lice", "\uff4dary", "caf\u00e9", "a\ud800b", "mary\u0301", "\u2024\u2024", "nul.txt", "con.", "x" + "\t"]
RESERVED_NAMES = ["default", "profiles", "data", "config", "con", "prn", "aux", "nul",
                  *[f"com{i}" for i in range(1, 10)], *[f"lpt{i}" for i in range(1, 10)]]


@pytest.mark.parametrize("name", HOSTILE + RESERVED_NAMES, ids=lambda n: repr(n)[:30])
def test_bad_and_reserved_names_are_refused_with_the_rule(name):
    with pytest.raises(ProfileError) as e:
        P.validate_name(name)
    text = str(e.value)
    assert len(text) < 400 and "name" in text and ("lower-case" in text or "reserved" in text)


@pytest.mark.parametrize("value", [None, 7, 1.5, b"mary", ["mary"], {"a": 1}, True])
def test_non_text_names_are_refused(value):
    with pytest.raises(ProfileError):
        P.validate_name(value)


def test_name_length_boundary():
    P.validate_name("a" * PROFILE_NAME_MAX_CHARS)
    with pytest.raises(ProfileError, match="characters"):
        P.validate_name("a" * (PROFILE_NAME_MAX_CHARS + 1))


@pytest.mark.parametrize("name", HOSTILE + RESERVED_NAMES, ids=lambda n: repr(n)[:30])
def test_hostile_names_never_make_or_touch_a_path(home, name):
    before = {p for p in home.rglob("*")}
    for call in (P.create_profile, P.resolve_profile, P.load_profile_config):
        with pytest.raises(ProfileError):
            call(name)
    assert {p for p in home.rglob("*")} == before  # nothing was created anywhere


def test_a_link_that_points_out_of_profiles_is_refused(home, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (P.PROFILES_ROOT).mkdir()
    link = P.PROFILES_ROOT / "sneaky"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        if completed.returncode != 0:
            pytest.skip("this account cannot make a directory link")
    with pytest.raises(ProfileError, match="outside"):
        P.resolve_profile("sneaky")
    with pytest.raises(ProfileError):
        P.create_profile("sneaky")


def test_profile_argument_forms(home):
    assert main_mod.split_profile_arg(["chat", "--profile", "mary", "--quiet"]) == ("mary", ["chat", "--quiet"])
    assert main_mod.split_profile_arg(["--profile=mary", "info"]) == ("mary", ["info"])
    assert main_mod.split_profile_arg(["info"]) == (None, ["info"])
    for bad in (["--profile"], ["info", "--profile", "--live"], ["--profile", "a", "--profile", "b"],
                ["--profile=", "info"], ["--profile", "../x"], ["--profile=con"]):
        with pytest.raises(ProfileError):
            main_mod.split_profile_arg(bad)


# ---- create, list, show ------------------------------------------------------------------------------------------


def test_create_scaffolds_safe_per_person_defaults(home):
    path, port = P.create_profile("mary")
    assert path == P.PROFILES_ROOT / "mary" and port == PROFILE_PORT_FIRST
    assert sorted(p.name for p in path.iterdir()) == [".env", "README.txt", "config.toml", "data"]
    assert (path / "data").is_dir() and not list((path / "data").iterdir())
    cfg = P.load_profile_config("mary")
    assert (cfg.network.port, cfg.caps.session_usd, cfg.caps.daily_usd, cfg.caps.hard_stop_usd, cfg.caps.requests_per_hour) == (
        PROFILE_PORT_FIRST, 1.0, 5.0, 10.0, 300)
    assert cfg.logging.full_prompts is False and cfg.models.allow_live_calls is False
    assert cfg.paths.data_dir == (path / "data").resolve() and cfg.profile == Profile("mary", path.resolve())


def test_create_env_has_key_names_only_and_readme_has_no_value(home):
    path, _ = P.create_profile("mary")
    lines = [ln for ln in (path / ".env").read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
    assert lines == [f"{k}=" for k in C.KNOWN_KEYS]
    text = (path / "README.txt").read_text(encoding="utf-8")
    assert ".env" in text and "sk-" not in text and "profile mary" in text.lower() and "by hand" in text
    assert C.parse_env_file(path / ".env") == {k: "" for k in C.KNOWN_KEYS}


def test_ports_are_distinct_and_skip_those_in_use(home):
    (home / "config.toml").write_text('[network]\nport = 8766\n', encoding="utf-8")  # the root config uses 8766
    ports = [P.create_profile(n)[1] for n in ("a1", "a2", "a3")]
    assert ports == [8767, 8768, 8769] and 8765 not in ports
    set_config("a2", port="8770")  # a profile moved to another port: its old one is free again
    assert P.next_free_port() == 8768


def test_create_never_overwrites(home):
    path, _ = P.create_profile("mary")
    put_key("mary")
    (path / "config.toml").write_text("# mine\n[network]\nport = 9001\n", encoding="utf-8")
    snapshot = {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}
    with pytest.raises(ProfileError, match="already exists"):
        P.create_profile("mary")
    assert {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()} == snapshot


def test_create_over_a_file_is_refused(home):
    P.PROFILES_ROOT.mkdir()
    (P.PROFILES_ROOT / "mary").write_text("not a folder", encoding="utf-8")
    with pytest.raises(ProfileError, match="already exists"):
        P.create_profile("mary")
    with pytest.raises(ProfileError, match="file, not a folder"):
        P.resolve_profile("mary")


def test_a_failed_create_leaves_no_half_made_folder(home, monkeypatch):
    def full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(P, "_readme_template", full)
    with pytest.raises(ProfileError, match="nothing was kept"):
        P.create_profile("mary")
    assert not (P.PROFILES_ROOT / "mary").exists()


def test_create_when_profiles_cannot_be_made(home):
    (home / "profiles").write_text("a file where the folder should be", encoding="utf-8")
    with pytest.raises(ProfileError, match="cannot"):
        P.create_profile("mary")


def test_missing_and_broken_profiles_say_what_to_do(home):
    with pytest.raises(ProfileError, match="does not exist; create it with: python -m brain profile create ghost"):
        P.load_profile_config("ghost")
    P.create_profile("mary")
    (P.PROFILES_ROOT / "mary" / "config.toml").unlink()
    with pytest.raises(ProfileError, match="no config.toml"):
        P.load_profile_config("mary")
    (P.PROFILES_ROOT / "mary" / "config.toml").write_text("[network\nport = 1", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        P.load_profile_config("mary")
    (P.PROFILES_ROOT / "mary" / "config.toml").write_text("[caps]\nsesion_usd = 1.0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="did you mean 'session_usd'"):
        P.load_profile_config("mary")


def test_a_profile_config_cannot_leave_its_folder(home):
    P.create_profile("mary")
    set_config("mary", data_dir='"../../elsewhere"')
    with pytest.raises(ConfigError, match=r"\.\."):
        P.load_profile_config("mary")
    set_config("mary", data_dir=json.dumps(str(home / "abs")))
    with pytest.raises(ConfigError, match="relative"):
        P.load_profile_config("mary")


def test_list_and_show(home, capsys):
    assert main_mod.main(["profile", "list"]) == 0
    assert "no profiles yet" in capsys.readouterr().out
    P.create_profile("mary")
    P.create_profile("jo")
    put_key("jo")
    P.PROFILES_ROOT.joinpath("zz.txt").write_text("ignored", encoding="utf-8")
    (P.PROFILES_ROOT / "Not Valid").mkdir()
    assert main_mod.main(["profile", "list"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "jo: port 8767; live calls off; ANTHROPIC_API_KEY present; prompt capture off; last used never",
        "mary: port 8766; live calls off; ANTHROPIC_API_KEY missing; prompt capture off; last used never"]
    assert OWN_KEY not in out
    assert main_mod.main(["profile", "show", "jo"]) == 0
    out = capsys.readouterr().out
    assert "profile: jo" in out and "key ANTHROPIC_API_KEY: present" in out and "prompt capture: off" in out
    assert OWN_KEY not in out and ENV_CANARY not in out and ROOT_CANARY not in out
    assert main_mod.main(["profile", "show", "ghost"]) == 2
    assert main_mod.main(["profile", "show", "../x"]) == 2
    P.PROFILES_ROOT.joinpath("mary", "config.toml").write_text("garbage = [", encoding="utf-8")
    assert main_mod.main(["profile", "list"]) == 0
    assert "mary: not usable" in capsys.readouterr().out


def test_list_is_bounded(home, monkeypatch):
    P.PROFILES_ROOT.mkdir()
    for i in range(PROFILES_MAX + 5):
        (P.PROFILES_ROOT / f"p{i:03d}").mkdir()
    assert len(P._profile_folders()) == PROFILES_MAX


def test_profile_command_with_the_global_option_is_refused(home, capsys):
    assert main_mod.main(["--profile", "mary", "profile", "list"]) == 2
    assert "own argument" in capsys.readouterr().err
    assert main_mod.main(["chaos", "--profile", "mary"]) == 2
    assert "sandbox" in capsys.readouterr().err


# ---- key isolation: the important rule -----------------------------------------------------------------------------


def test_a_profile_never_sees_the_environment_or_the_root_env_key(home, capsys):
    P.create_profile("mary")
    prof = P.resolve_profile("mary")
    assert prof.api_key(KEY) is None
    assert prof.key_presence() == {k: False for k in C.KNOWN_KEYS}
    assert main_mod.info(P.load_profile_config("mary")) == 0  # (main() would install the process-wide egress guard)
    out = capsys.readouterr().out
    assert f"key {KEY}: missing" in out and ENV_CANARY not in out and ROOT_CANARY not in out
    assert str(prof.env_path) in out and "never used for a profile" in out
    # the default profile keeps today's behaviour: the environment first, then the root .env
    assert Profile().api_key(KEY) == ENV_CANARY
    assert Profile().key_presence()[KEY] is True


def test_default_profile_without_the_environment_reads_the_root_env(home, monkeypatch):
    monkeypatch.delenv(KEY)
    assert Profile().api_key(KEY) == ROOT_CANARY
    P.create_profile("mary")
    assert P.resolve_profile("mary").api_key(KEY) is None


def test_the_profile_key_is_its_own_not_the_canaries(home):
    P.create_profile("mary")
    put_key("mary")
    prof = P.resolve_profile("mary")
    assert prof.api_key(KEY) == OWN_KEY and prof.key_presence()[KEY] is True
    assert Profile().api_key(KEY) == ENV_CANARY  # and the default's key is not hers


def test_live_start_for_a_profile_without_its_own_key_is_refused_and_builds_no_client(home, monkeypatch):
    P.create_profile("mary")
    set_config("mary", allow_live_calls="true")

    def must_not_run(key, **kw):
        raise AssertionError("a model client was built for a profile that has no key of its own")

    monkeypatch.setattr("brain.egress.build_anthropic_client", must_not_run)
    cfg = P.load_profile_config("mary")
    with pytest.raises(StartRefused, match="no ANTHROPIC_API_KEY in .*mary.*\\.env.*not used for a profile"):
        run(live_model(cfg, load_prices()))
    out: list[str] = []
    assert run(run_chat(cfg, SimBody(), options=ChatOptions(live=True), out=out.append)) == 2
    assert "no ANTHROPIC_API_KEY" in out[-1] and ENV_CANARY not in "".join(out)


def test_live_start_for_a_profile_uses_exactly_its_own_key(home, monkeypatch):
    P.create_profile("mary")
    set_config("mary", allow_live_calls="true")
    put_key("mary")
    seen: list[str] = []

    def spy(key, **kw):
        seen.append(key)
        raise EgressDenied("test: stop here")

    monkeypatch.setattr("brain.egress.build_anthropic_client", spy)
    with pytest.raises(StartRefused, match="egress"):
        run(live_model(P.load_profile_config("mary"), load_prices()))
    assert seen == [OWN_KEY]


# ---- separation: events, state, spend, capture, tokens, ports, egress ------------------------------------------------


def _chat(cfg, lines):
    async def feed():
        for line in lines:
            yield line
            await asyncio.sleep(0.05)

    out: list[str] = []
    assert run(run_chat(cfg, SimBody(), options=ChatOptions(trace=False), lines=feed(), out=out.append)) == 0
    return "\n".join(out)


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_two_profiles_write_only_under_their_own_data(home):
    for n in ("mary", "jo"):
        P.create_profile(n)
    set_config("mary", full_prompts="true")
    cfgs = {n: P.load_profile_config(n) for n in ("mary", "jo")}
    before = _files(home)
    _chat(cfgs["mary"], ["hello from mary", "/quit"])
    mid_jo = _files(P.PROFILES_ROOT / "jo" / "data")
    assert mid_jo == set()  # nothing of Mary's session reached Jo's data
    _chat(cfgs["jo"], ["hello from jo", "/quit"])
    for n in ("mary", "jo"):
        data = P.PROFILES_ROOT / n / "data"
        names = _files(data)
        assert "state.json" in names and any(x.startswith("logs") and "events-" in x for x in names), (n, names)
    mary_capture = _files(P.PROFILES_ROOT / "mary" / "data" / "capture")
    assert mary_capture and not (P.PROFILES_ROOT / "jo" / "data" / "capture").exists()  # capture off by default
    assert "hello from jo" not in "".join(p.read_text(encoding="utf-8", errors="replace")
                                          for p in (P.PROFILES_ROOT / "mary" / "data").rglob("*") if p.is_file())
    new_outside = {f for f in _files(home) - before if not f.startswith("profiles")}
    assert not new_outside, new_outside  # nothing leaked to a root data/ or anywhere else


def test_spend_in_one_profile_does_not_change_the_others_counters(home):
    for n in ("mary", "jo"):
        P.create_profile(n)
    prices = load_prices()
    guards = {}
    for n in ("mary", "jo"):
        cfg = P.load_profile_config(n)
        store = StateStore(cfg.paths.data_dir)
        guards[n] = SpendGuard(cfg.caps, prices, store, persist=True)
        guards[n].start()
    g = guards["mary"]
    res = g.reserve("claude-haiku-5-5", 1000, 100)
    assert not hasattr(res, "reason")
    g.settle(res, Usage(input_tok=1000, output_tok=100))
    mary = json.loads((P.PROFILES_ROOT / "mary" / "data" / "state.json").read_text(encoding="utf-8"))
    jo_file = P.PROFILES_ROOT / "jo" / "data" / "state.json"
    assert mary["spend_usd_total"] > 0
    jo = json.loads(jo_file.read_text(encoding="utf-8")) if jo_file.exists() else {"spend_usd_total": 0.0}
    assert jo["spend_usd_total"] == 0.0 and guards["jo"].spent("session") == 0.0 and guards["jo"].spent("daily") == 0.0
    guards["jo"].start()  # a restart of Jo's guard still sees nothing of Mary's spend
    assert guards["jo"].spent("daily") == 0.0 and guards["jo"].spent("hard_stop") == 0.0


def test_token_stores_differ_per_profile_and_from_the_default(home):
    for n in ("mary", "jo"):
        P.create_profile(n)
    cfgs = {n: P.load_profile_config(n) for n in ("mary", "jo")}
    default = main_mod.secrets_dir(C.parse_config({}, base=home))
    paths = {n: main_mod.secrets_dir(c) for n, c in cfgs.items()}
    assert paths["mary"] != paths["jo"] and default not in paths.values()
    assert paths["mary"] == Path(home / "local" / "EmbodiedAgent" / "profiles" / "mary" / "secrets")
    assert paths["mary"] == auth.profile_secrets_dir("mary")
    assert default == home / "local" / "EmbodiedAgent" / "secrets"
    tokens = {n: main_mod.load_token(c)[0] for n, c in cfgs.items()}
    assert all(t is not None for t in tokens.values()) and tokens["mary"] != tokens["jo"]
    assert main_mod.load_token(cfgs["mary"])[0] == tokens["mary"]  # stable across loads


def test_a_configured_secrets_dir_still_wins(home):
    P.create_profile("mary")
    mine = home / "local" / "mine"
    (P.PROFILES_ROOT / "mary" / "config.toml").write_text(f"[paths]\nsecrets_dir = '{mine}'\n", encoding="utf-8")
    assert main_mod.secrets_dir(P.load_profile_config("mary")) == mine.resolve()


def test_egress_comes_from_the_profiles_own_config(home):
    for n in ("mary", "jo"):
        P.create_profile(n)
    set_config("jo", allowed_hosts="[]")
    mary, jo = (EgressPolicy.from_config(P.load_profile_config(n)) for n in ("mary", "jo"))
    assert mary.allowed == frozenset({("api.anthropic.com", 443)}) and jo.allowed == frozenset()


def test_two_brains_with_different_ports_run_at_the_same_time(home):
    for n in ("mary", "jo"):
        P.create_profile(n)
    ports = {"mary": free_port(), "jo": free_port()}
    for n, port in ports.items():
        set_config(n, port=str(port))
    cfgs = {n: P.load_profile_config(n) for n in ports}
    assert cfgs["mary"].network.port != cfgs["jo"].network.port and PORT_MIN <= cfgs["jo"].network.port <= PORT_MAX
    tokens = {n: main_mod.load_token(c)[0] for n, c in cfgs.items()}

    async def go() -> None:
        servers = {n: BrainServer(c.network.host, c.network.port, token=tokens[n]) for n, c in cfgs.items()}
        for s in servers.values():
            await s.start()
        try:
            for n, c in cfgs.items():
                async with TestBodyClient(f"ws://127.0.0.1:{c.network.port}/", token=tokens[n]) as client:
                    assert (await client.echo_test(3, 10)).ok
            with pytest.raises(auth.AuthError):  # Jo's token does not open Mary's brain
                async with TestBodyClient(f"ws://127.0.0.1:{cfgs['mary'].network.port}/", token=tokens["jo"]):
                    pass
        finally:
            for s in servers.values():
                await s.stop()

    asyncio.run(asyncio.wait_for(go(), 60))


# ---- git: nothing under profiles/ can be staged -----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["profiles/mary/.env", "profiles/mary/config.toml", "profiles/mary/data/state.json",
                                  "profiles/mary/README.txt", "profiles/x/data/capture/c.jsonl", "profiles/.env"])
def test_profiles_are_git_ignored(path):
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO, capture_output=True)
    assert r.returncode == 0, f"{path} is not ignored: a profile folder holds a .env and must never be staged"


def test_nothing_under_profiles_can_be_added(tmp_path):
    """A real attempt in a throwaway repo that uses the project's .gitignore: git add refuses the paths."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text((REPO / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "profiles" / "mary").mkdir(parents=True)
    (tmp_path / "profiles" / "mary" / ".env").write_text("ANTHROPIC_API_KEY=\n", encoding="utf-8")
    (tmp_path / "profiles" / "mary" / "config.toml").write_text("[network]\n", encoding="utf-8")
    r = subprocess.run(["git", "add", "profiles"], cwd=tmp_path, capture_output=True, text=True)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert staged.strip() == "" and (r.returncode != 0 or "ignored" in r.stderr.lower() or r.stderr == "")


# ---- gate edges (E1b): below / at / above each limit, and a wrong type ---------------------------------------------------

VALIDATE = "brain.profiles.validate_name"
LOAD = "brain.profiles.load_profile_config"
CREATE = "brain.profiles.create_profile"


@pytest.mark.gate(VALIDATE, limit="PROFILE_NAME_MAX_CHARS", edge="below")
def test_name_one_under_the_limit_is_accepted():
    assert P.validate_name("a" * (PROFILE_NAME_MAX_CHARS - 1))


@pytest.mark.gate(VALIDATE, limit="PROFILE_NAME_MAX_CHARS", edge="at")
def test_name_at_the_limit_is_accepted():
    assert P.validate_name("a" * PROFILE_NAME_MAX_CHARS)


@pytest.mark.gate(VALIDATE, limit="PROFILE_NAME_MAX_CHARS", edge="above")
def test_name_one_over_the_limit_is_refused():
    with pytest.raises(ProfileError, match="characters"):
        P.validate_name("a" * (PROFILE_NAME_MAX_CHARS + 1))


@pytest.mark.gate(VALIDATE, edge="wrong_type")
def test_name_of_the_wrong_type_is_refused():
    for value in (None, 1, b"a", ["a"]):
        with pytest.raises(ProfileError):
            P.validate_name(value)


@pytest.mark.gate(LOAD, limit="PROFILE_NAME_MAX_CHARS", edge="below")
def test_load_a_name_one_under_the_limit_reaches_the_folder_check(home):
    with pytest.raises(ProfileError, match="does not exist"):
        P.load_profile_config("a" * (PROFILE_NAME_MAX_CHARS - 1))


@pytest.mark.gate(LOAD, limit="PROFILE_NAME_MAX_CHARS", edge="at")
def test_load_a_name_at_the_limit_works(home):
    name = "a" * PROFILE_NAME_MAX_CHARS
    P.create_profile(name)
    assert P.load_profile_config(name).profile.name == name


@pytest.mark.gate(LOAD, limit="PROFILE_NAME_MAX_CHARS", edge="above")
def test_load_a_name_over_the_limit_is_refused_by_the_name_rule(home):
    with pytest.raises(ProfileError, match="characters in all"):
        P.load_profile_config("a" * (PROFILE_NAME_MAX_CHARS + 1))


def _config_of_size(name: str, size: int) -> None:
    path = P.PROFILES_ROOT / name / "config.toml"
    body = "[network]\nport = 8800\n"
    path.write_bytes((body + "#" * (size - len(body) - 1) + "\n").encode("ascii"))
    assert path.stat().st_size == size


@pytest.mark.gate(LOAD, limit="CONFIG_MAX_BYTES", edge="below")
def test_load_a_config_one_byte_under_the_cap(home):
    from brain.limits import CONFIG_MAX_BYTES

    P.create_profile("mary")
    _config_of_size("mary", CONFIG_MAX_BYTES - 1)
    assert P.load_profile_config("mary").network.port == 8800


@pytest.mark.gate(LOAD, limit="CONFIG_MAX_BYTES", edge="at")
def test_load_a_config_exactly_at_the_cap(home):
    from brain.limits import CONFIG_MAX_BYTES

    P.create_profile("mary")
    _config_of_size("mary", CONFIG_MAX_BYTES)
    assert P.load_profile_config("mary").network.port == 8800


@pytest.mark.gate(LOAD, limit="CONFIG_MAX_BYTES", edge="above")
def test_load_a_config_one_byte_over_the_cap_is_refused(home):
    from brain.limits import CONFIG_MAX_BYTES

    P.create_profile("mary")
    _config_of_size("mary", CONFIG_MAX_BYTES + 1)
    with pytest.raises(ConfigError, match="larger than"):
        P.load_profile_config("mary")


@pytest.mark.gate(LOAD, edge="wrong_type")
def test_load_with_a_name_of_the_wrong_type_is_refused(home):
    with pytest.raises(ProfileError):
        P.load_profile_config(7)  # type: ignore[arg-type]


@pytest.mark.gate(CREATE, limit="PROFILE_PORT_FIRST", edge="below")
def test_first_port_is_not_below_the_first_profile_port(home):
    assert P.create_profile("a1")[1] == PROFILE_PORT_FIRST  # 8765 (the default's) is in use; nothing below the first is given


@pytest.mark.gate(CREATE, limit="PROFILE_PORT_FIRST", edge="at")
def test_first_port_is_exactly_the_first_profile_port(home):
    assert P.next_free_port() == PROFILE_PORT_FIRST


@pytest.mark.gate(CREATE, limit="PROFILE_PORT_FIRST", edge="above")
def test_when_the_first_port_is_taken_the_next_is_given(home):
    P.create_profile("a1")
    assert P.create_profile("a2")[1] == PROFILE_PORT_FIRST + 1


def test_when_every_port_is_taken_create_refuses_and_makes_nothing(home, monkeypatch):
    monkeypatch.setattr(P, "ports_in_use", lambda: set(range(PROFILE_PORT_FIRST, PORT_MAX + 1)))
    with pytest.raises(ProfileError, match="no free port"):
        P.create_profile("mary")
    assert not (P.PROFILES_ROOT / "mary").exists()
    monkeypatch.setattr(P, "ports_in_use", lambda: set(range(PROFILE_PORT_FIRST, PORT_MAX)))
    assert P.create_profile("mary")[1] == PORT_MAX


@pytest.mark.gate(CREATE, edge="wrong_type")
def test_create_with_a_name_of_the_wrong_type_is_refused(home):
    with pytest.raises(ProfileError):
        P.create_profile(None)  # type: ignore[arg-type]
