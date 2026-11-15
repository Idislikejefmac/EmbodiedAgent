"""Profiles: one setup per person (docs/profiles.md; B10 files, B11 command line).

Two experiments:
- SYS.profiles.name_gate: the boundary-value matrix on the profile name (a letter, then letters, digits, _ or -,
  at most PROFILE_NAME_MAX_CHARS, not reserved), hostile names (traversal, separators, drive and UNC forms,
  device names, Unicode look-alikes, control characters, a huge name), checked against an independent reference
  (a regular expression and a list written out here), and: a refused name creates nothing on disk and an
  accepted one resolves to a direct child of profiles/.
- SYS.profiles.isolation: create and load a profile; two profiles do not share ports, data or token folders;
  KEY ISOLATION (the sandbox puts canary keys in the environment and in the root .env; a profile must see
  neither, and the output must never hold any key); odd folders (a file, a link, a broken config) are refused
  with a message; a failed create leaves no half-made folder.

Everything happens in a fresh folder inside the sandbox: `brain.profiles.PROFILES_ROOT` and the root .env are
pointed there for one case and restored after it. The real profiles/, .env and token store are never touched
(the token store's path is computed, never created).
"""

from __future__ import annotations

import contextlib
import io
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from brain import __main__ as cli
from brain import config as C
from brain import profiles as P
from brain.config import ConfigError, Profile, ProfileError
from brain.limits import PROFILE_NAME_MAX_CHARS, PROFILE_PORT_FIRST, PROFILES_MAX
from brain.transport import auth

from ..api import Case, CaseClass, Ctx, Gen, experiment, failure, hostile, invalid, valid
from ..matrix import Spec, matrix_cases

KEY = "ANTHROPIC_API_KEY"
# The reference the gate is compared with (written out here, not imported from the product).
REF_PATTERN = rf"[a-z][a-z0-9_-]{{0,{PROFILE_NAME_MAX_CHARS - 1}}}"
REF_RESERVED = {"default", "profiles", "data", "config", "con", "prn", "aux", "nul",
                *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
NOT_JSON = ("wrong_type_bytes",)
FAKE_OWN_KEY = "sk-ant-canary-profile-own-0000000000"


def _reference_accepts(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(REF_PATTERN, value) is not None and value not in REF_RESERVED


def _only(cls: CaseClass, gen: Any) -> Any:
    def pick(g: Gen) -> list[Case]:
        return [c for c in gen(g) if c.cls is cls]
    return pick


# ---- the name gate ---------------------------------------------------------------------------------------------------

GOOD = ["mary", "a", "z9", "jeff-2", "x_y", "a" * PROFILE_NAME_MAX_CHARS, "com10", "lpt0", "console", "data2", "default2"]
ODD_INVALID = ["Mary", "MARY", "1abc", "-a", "_a", "a b", "a.", "a ", " a", "a\n", "a\r\n", "", " ", "a" * (PROFILE_NAME_MAX_CHARS + 1)]
HOSTILE_NAMES = ["..", ".", "...", "../x", "..\\x", "a/b", "a\\b", "/abs", "C:\\Windows", "C:", "c:x", "\\\\host\\share", "~",
                 "%APPDATA%", "$HOME", "a:b", "a;b", "a*b", "a?b", "a|b", "a<b", "a\x00b", "a\x1bb", "a\x07b", "a\u200bb",
                 "a\u202eb", "\ufeffa", "\u0430lice", "\uff4dary", "caf\u00e9", "mary\u0301", "a\ud800b", "nul.txt", "con.",
                 "CON", "Aux", "COM1", "x" * 10_000, "\u2024\u2024", "a\u3000b"]


def _name_case(b: Any) -> dict[str, Any]:
    return {"op": "name", "value": b.value, "accept": b.accept}


def gen_name(g: Gen) -> list[Case]:
    spec = Spec("string", min_length=1, max_length=PROFILE_NAME_MAX_CHARS, pattern=REF_PATTERN, required=True)
    out = matrix_cases("profile_name", spec, make=_name_case, skip=NOT_JSON)
    out += [valid(f"good_{i}", {"op": "name", "value": n, "accept": True}) for i, n in enumerate(GOOD)]
    out += [invalid(f"bad_{i}", {"op": "name", "value": n, "accept": False}, "N3" if not n.strip() else "N6")
            for i, n in enumerate(ODD_INVALID)]
    out += [invalid(f"reserved_{n}", {"op": "name", "value": n, "accept": False}, "N6") for n in sorted(REF_RESERVED)]
    return out


def gen_name_hostile(g: Gen) -> list[Case]:
    return [hostile(f"hostile_{i}", {"op": "name", "value": n, "accept": False}, "N7") for i, n in enumerate(HOSTILE_NAMES)]


@contextlib.contextmanager
def _world(ctx: Ctx) -> Iterator[Path]:
    """profiles/, the root config and the root .env pointed into a fresh sandbox folder for one case."""
    root = ctx.sandbox.fresh_dir("profiles")
    saved = (P.PROFILES_ROOT, C.DEFAULT_CONFIG, C.DEFAULT_ENV)
    P.PROFILES_ROOT, C.DEFAULT_CONFIG, C.DEFAULT_ENV = root / "profiles", ctx.sandbox.config_path, ctx.sandbox.env_path
    try:
        yield root
    finally:
        P.PROFILES_ROOT, C.DEFAULT_CONFIG, C.DEFAULT_ENV = saved


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


@experiment(
    id="SYS.profiles.name_gate", card="SYS",
    entries=("cli_command.brain.profile", "cli_arg.brain.*.--profile", "gate.brain.profiles.validate_name"),
    hypothesis="A profile name that is not a lower-case letter followed by at most 31 lower-case letters, digits, '_' "
               "or '-', or that is a reserved word, is refused with a ProfileError naming the rule (never another "
               "exception) and creates nothing on disk; an accepted name resolves only to a direct child of profiles/.",
    blast_radius="sandbox", valid=_only(CaseClass.VALID, gen_name), invalid=_only(CaseClass.INVALID, gen_name),
    hostile=gen_name_hostile,
    failure=lambda g: [failure("profiles_folder_is_a_file", {"op": "root_is_file"}, "N15")])
def check_name(case: Case, ctx: Ctx) -> Any:
    d = case.data
    with _world(ctx) as root:
        if d["op"] == "root_is_file":
            P.PROFILES_ROOT.write_text("a file where profiles/ belongs", encoding="utf-8")
            try:
                P.create_profile("mary")
            except ProfileError as e:
                ctx.expect("cannot" in str(e), "fail_closed", f"no reason given: {str(e)[:100]}")
                return "refused"
            ctx.expect(False, "fail_closed", "create_profile worked with a file in place of profiles/")
        value = d["value"]
        want = _reference_accepts(value)
        if d["accept"] is True:
            ctx.expect(want, "validate_before_use", f"the reference refuses {value!r:.40}: the case is wrong")
        if d["accept"] is False:
            ctx.expect(not want, "validate_before_use", f"the reference accepts {value!r:.40}: the case is wrong")
        before = _tree(root)
        try:
            got = P.validate_name(value)
        except ProfileError as e:
            ctx.expect(not want, "validate_before_use", f"a good name was refused: {value!r:.40}: {str(e)[:100]}")
            text = str(e)
            ctx.expect("name" in text and ("lower-case" in text or "reserved" in text), "right_to_refuse",
                       f"the refusal does not name the rule: {text[:120]}")
            ctx.expect(len(text) < 600, "bounded_resources", "the refusal repeats a huge name")
            for call in (P.create_profile, P.resolve_profile, P.load_profile_config):
                try:
                    call(value)
                except ProfileError:
                    pass
                else:
                    ctx.expect(False, "validate_before_use", f"{call.__name__} accepted a refused name")
            ctx.expect(_tree(root) == before, "no_forbidden_side_effect", "a refused name left something on disk")
            return "refused"
        ctx.expect(want, "validate_before_use", f"a bad name was accepted: {value!r:.60}")
        ctx.expect(got == value, "validate_before_use", "the validated name differs from the input")
        path, port = P.create_profile(value)
        ctx.expect(path.resolve().parent == P.PROFILES_ROOT.resolve(), "no_forbidden_side_effect",
                   "the profile folder is not a direct child of profiles/")
        ctx.expect({p.name for p in path.iterdir()} == {"data", ".env", "config.toml", "README.txt"},
                   "no_forbidden_side_effect", "create wrote something unexpected")
        ctx.expect(port >= PROFILE_PORT_FIRST, "validate_before_use", "the port is below the first profile port")
        return "accepted"


# ---- isolation, scaffold and failures -----------------------------------------------------------------------------------


def gen_iso_valid(g: Gen) -> list[Case]:
    return [valid(n, {"op": n}) for n in ("create_and_load", "two_profiles_do_not_share", "list_show_hold_no_secret",
                                         "ports_are_distinct", "own_key_is_used_alone", "default_is_unchanged")]


def gen_iso_invalid(g: Gen) -> list[Case]:
    return [invalid("create_existing_refused", {"op": "create_existing"}, "N4"),
            invalid("load_missing_profile", {"op": "missing"}, "N3"),
            invalid("unknown_key_suggests", {"op": "unknown_key"}, "N4"),
            invalid("data_dir_leaves_folder", {"op": "data_escape"}, "N6"),
            invalid("config_empty_is_defaults", {"op": "empty_config"}, "N3")]


def gen_iso_hostile(g: Gen) -> list[Case]:
    return [hostile("key_isolation_environment_and_root_env", {"op": "key_isolation"}, "N8"),
            hostile("key_isolation_info_output", {"op": "info_output"}, "N8"),
            hostile("link_out_of_profiles", {"op": "link_out"}, "N7"),
            hostile("profile_command_hides_no_value", {"op": "hostile_env_file"}, "N8")]


def gen_iso_failure(g: Gen) -> list[Case]:
    return [failure("profile_folder_is_a_file", {"op": "folder_is_file"}, "N15"),
            failure("config_is_corrupt", {"op": "corrupt_config"}, "N13"),
            failure("config_is_missing", {"op": "no_config"}, "N15"),
            failure("env_is_a_folder", {"op": "env_is_folder"}, "N15"),
            failure("create_fails_halfway", {"op": "half_made"}, "N15")]


def _refused(ctx: Ctx, fn: Any, *args: Any, expect: str = "") -> str:
    try:
        fn(*args)
    except ConfigError as e:
        ctx.expect(len(str(e)) < 800, "bounded_resources", "an oversized refusal")
        if expect:
            ctx.expect(expect in str(e), "right_to_refuse", f"the refusal lacks {expect!r}: {str(e)[:160]}")
        return str(e)
    ctx.expect(False, "fail_closed", f"{getattr(fn, '__name__', fn)} did not refuse")
    return ""


def _set_key(path: Path, value: str) -> None:
    (path / ".env").write_text(f"{KEY}={value}\n", encoding="utf-8")


@experiment(
    id="SYS.profiles.isolation", card="SYS",
    entries=("cli_command.brain.profile", "file_site.brain/profiles.py:*", "gate.brain.profiles.create_profile",
             "gate.brain.profiles.load_profile_config", "cli_command.brain.info", "cli_arg.brain.info.--profile"),
    hypothesis="A profile reads its settings, keys and data only from profiles/<name>/: the process environment "
               "and the root .env are never used for its keys (canary keys in both are never seen, shown or "
               "used), two profiles share no port, data or token folder, a missing, corrupt, odd or half-made "
               "profile is refused with one message (ConfigError, exit 2), and an existing profile is never "
               "overwritten.",
    blast_radius="sandbox", valid=gen_iso_valid, invalid=gen_iso_invalid, hostile=gen_iso_hostile, failure=gen_iso_failure)
def check_isolation(case: Case, ctx: Ctx) -> Any:
    op = case.data["op"]
    sb = ctx.sandbox
    canaries = [sb.canaries[KEY]]
    with _world(ctx) as root:
        if op == "create_and_load":
            path, port = P.create_profile("mary")
            cfg = P.load_profile_config("mary")
            ctx.expect(port == PROFILE_PORT_FIRST == cfg.network.port, "validate_before_use", "the first port is not 8766")
            ctx.expect((cfg.caps.session_usd, cfg.caps.daily_usd, cfg.caps.hard_stop_usd, cfg.caps.requests_per_hour)
                       == (1.0, 5.0, 10.0, 300), "validate_before_use", "the caps are not the safe per-person defaults")
            ctx.expect(cfg.logging.full_prompts is False and cfg.models.allow_live_calls is False, "validate_before_use",
                       "capture or live calls are on in a new profile")
            ctx.expect(cfg.paths.data_dir == (path / "data").resolve(), "no_forbidden_side_effect", "data is not in the profile")
            env = (path / ".env").read_text(encoding="utf-8")
            ctx.expect(all(line.endswith("=") for line in env.splitlines() if line and not line.startswith("#")),
                       "no_secret_in_output", "the new .env holds a value")
            return f"port {port}"
        if op == "two_profiles_do_not_share" or op == "ports_are_distinct":
            names = ["a1", "b2", "c3"]
            made = {n: P.create_profile(n)[0] for n in names}
            cfgs = {n: P.load_profile_config(n) for n in names}
            ctx.expect(len({c.network.port for c in cfgs.values()}) == 3, "validate_before_use", "two profiles share a port")
            ctx.expect(len({c.paths.data_dir for c in cfgs.values()}) == 3, "no_forbidden_side_effect", "shared data folder")
            secrets = {cli.secrets_dir(c) for c in cfgs.values()}
            ctx.expect(len(secrets) == 3 and cli.secrets_dir(C.parse_config({}, base=root)) not in secrets,
                       "no_forbidden_side_effect", "two profiles share a token store, or one uses the default's")
            ctx.expect(all(s == auth.profile_secrets_dir(n) for n, s in zip(names, (cli.secrets_dir(cfgs[n]) for n in names), strict=True)),
                       "validate_before_use", "a token store is not under profiles/<name>/secrets")
            ctx.expect(not any(s.exists() for s in secrets), "no_forbidden_side_effect", "a token folder was made by reading config")
            ctx.expect(all(made[n].parent == P.PROFILES_ROOT for n in names), "no_forbidden_side_effect", "outside profiles/")
            return "distinct"
        if op == "list_show_hold_no_secret":
            path, _ = P.create_profile("mary")
            _set_key(path, FAKE_OWN_KEY)
            text = "\n".join(P.list_lines() + P.show_lines("mary"))
            ctx.expect(FAKE_OWN_KEY not in text and not any(c in text for c in canaries), "no_secret_in_output", "a key was shown")
            ctx.expect(f"{KEY} present" in text and f"key {KEY}: present" in text, "validate_before_use",
                       "the key was not reported present")
            return "names only"
        if op == "own_key_is_used_alone" or op == "key_isolation":
            path, _ = P.create_profile("mary")
            prof = P.resolve_profile("mary")
            ctx.expect(os.environ.get(KEY) == sb.canaries[KEY], "validate_before_use", "the sandbox did not put the canary in the environment")
            ctx.expect(C.parse_env_file(C.DEFAULT_ENV).get(KEY) == sb.canaries[KEY], "validate_before_use", "no canary in the root .env")
            ctx.expect(prof.api_key(KEY) is None and not any(prof.key_presence().values()), "no_forbidden_side_effect",
                       "a profile saw the environment's or the root .env's key")
            _set_key(path, FAKE_OWN_KEY)
            ctx.expect(prof.api_key(KEY) == FAKE_OWN_KEY and prof.api_key(KEY) != sb.canaries[KEY], "validate_before_use",
                       "the profile's own key was not the one used")
            ctx.expect(prof.api_key("ELEVENLABS_API_KEY") is None, "no_forbidden_side_effect", "another name's canary leaked in")
            return "isolated"
        if op == "info_output":
            P.create_profile("mary")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.info(P.load_profile_config("mary"))
            text = buf.getvalue()
            ctx.expect(not any(v in text for v in sb.canaries.values()), "no_secret_in_output", "info showed a key value")
            ctx.expect(f"key {KEY}: missing" in text and str(P.PROFILES_ROOT / "mary" / ".env") in text, "validate_before_use",
                       "info does not say the key is missing and which file it would come from")
            return "reported by name"
        if op == "default_is_unchanged":
            ctx.expect(Profile().api_key(KEY) == sb.canaries[KEY] and Profile().key_presence()[KEY], "validate_before_use",
                       "the default profile no longer reads the environment key")
            ctx.expect(Profile().config_path == C.DEFAULT_CONFIG and Profile().env_path == C.DEFAULT_ENV, "validate_before_use",
                       "the default profile moved its files")
            return "unchanged"
        if op == "create_existing":
            path, _ = P.create_profile("mary")
            (path / "config.toml").write_text("# mine\n", encoding="utf-8")
            _set_key(path, FAKE_OWN_KEY)
            snap = {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}
            _refused(ctx, P.create_profile, "mary", expect="already exists")
            ctx.expect(snap == {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}, "no_forbidden_side_effect",
                       "create changed an existing profile")
            return "kept"
        if op == "missing":
            _refused(ctx, P.load_profile_config, "ghost", expect="profile create ghost")
            ctx.expect(not P.PROFILES_ROOT.exists(), "no_forbidden_side_effect", "loading made a folder")
            return "refused"
        if op == "unknown_key":
            path, _ = P.create_profile("mary")
            (path / "config.toml").write_text("[caps]\nsesion_usd = 1.0\n", encoding="utf-8")
            _refused(ctx, P.load_profile_config, "mary", expect="did you mean 'session_usd'")
            return "refused"
        if op == "data_escape":
            path, _ = P.create_profile("mary")
            (path / "config.toml").write_text('[paths]\ndata_dir = "../../elsewhere"\n', encoding="utf-8")
            _refused(ctx, P.load_profile_config, "mary", expect="..")
            return "refused"
        if op == "empty_config":
            path, _ = P.create_profile("mary")
            (path / "config.toml").write_text("", encoding="utf-8")
            cfg = P.load_profile_config("mary")
            ctx.expect(cfg.paths.data_dir == (path / "data").resolve(), "no_forbidden_side_effect",
                       "an empty config moved the data folder out of the profile")
            return "defaults"
        if op == "link_out":
            P.PROFILES_ROOT.mkdir(parents=True)
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            try:
                (P.PROFILES_ROOT / "sneaky").symlink_to(elsewhere, target_is_directory=True)
            except OSError:  # no link rights for this account: the case has nothing to test
                return "skipped (cannot make a link)"
            _refused(ctx, P.resolve_profile, "sneaky", expect="outside")
            _refused(ctx, P.create_profile, "sneaky")
            return "refused"
        if op == "hostile_env_file":
            path, _ = P.create_profile("mary")
            (path / ".env").write_text("ANTHROPIC_API_KEY=" + FAKE_OWN_KEY + "\x00\x07\nno equals sign\n", encoding="utf-8")
            text = _refused(ctx, P.resolve_profile("mary").key_presence)
            ctx.expect(FAKE_OWN_KEY not in text, "no_secret_in_output", "the refusal quotes the key")
            return "refused"
        if op == "folder_is_file":
            P.PROFILES_ROOT.mkdir(parents=True)
            (P.PROFILES_ROOT / "mary").write_text("a file", encoding="utf-8")
            _refused(ctx, P.load_profile_config, "mary", expect="file, not a folder")
            _refused(ctx, P.create_profile, "mary", expect="already exists")
            ctx.expect((P.PROFILES_ROOT / "mary").read_text(encoding="utf-8") == "a file", "no_forbidden_side_effect", "file changed")
            return "refused"
        if op == "corrupt_config":
            path, _ = P.create_profile("mary")
            (path / "config.toml").write_bytes(b"[network\nport = \xff\xfe")
            _refused(ctx, P.load_profile_config, "mary")
            lines = P.list_lines()
            ctx.expect(len(lines) == 1 and "not usable" in lines[0], "fail_closed", "list does not flag the broken profile")
            return "refused"
        if op == "no_config":
            path, _ = P.create_profile("mary")
            (path / "config.toml").unlink()
            _refused(ctx, P.load_profile_config, "mary", expect="no config.toml")
            return "refused"
        if op == "env_is_folder":
            path, _ = P.create_profile("mary")
            (path / ".env").unlink()
            (path / ".env").mkdir()
            _refused(ctx, P.resolve_profile("mary").key_presence)
            return "refused"
        if op == "half_made":
            def full(*a: Any, **k: Any) -> str:
                raise OSError(28, "No space left on device")

            original = P._readme_template
            P._readme_template = full
            try:
                _refused(ctx, P.create_profile, "mary", expect="nothing was kept")
            finally:
                P._readme_template = original
            ctx.expect(not (P.PROFILES_ROOT / "mary").exists(), "no_forbidden_side_effect", "a half-made profile was left")
            ctx.expect(len(P._profile_folders()) <= PROFILES_MAX, "bounded_resources", "unbounded listing")
            return "cleaned up"
    raise AssertionError(f"unknown op {op}")

