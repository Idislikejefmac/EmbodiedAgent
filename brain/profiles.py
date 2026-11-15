"""Profiles: one setup per person on one machine (docs/profiles.md).

    python -m brain profile create NAME     # scaffold profiles/NAME/ (config.toml, .env, README.txt, data/)
    python -m brain profile list            # name, port, live calls, key present/missing, capture, last used
    python -m brain profile show NAME       # the effective config, no secrets
    python -m brain chat --profile NAME     # any command takes --profile NAME

Resolution (the precedence rules): no --profile means the default profile, today's behaviour (repo-root
config.toml and .env, data/, keys from the environment or that .env). --profile NAME means ONLY
profiles/NAME/: its config.toml, its .env (the sole source of its keys: the process environment and the
root .env are never read for it), its data/, and the token store under
%LOCALAPPDATA%\\EmbodiedAgent\\profiles\\NAME\\secrets. A profile never falls back to the default's files.

Names: a letter, then letters, digits, _ or - (at most PROFILE_NAME_MAX_CHARS), lower case only, and not a
reserved word (Windows device names, `default`, `profiles`, `data`, `config`). Paths are built from the
validated name alone and must resolve to a direct child of profiles/ (a link that points out is refused).

Errors are ProfileError (a ConfigError): one message, exit code 2, never a traceback. Nothing is ever
deleted by this module except the half-made folder of a `create` that failed. Values of .env are never read
except by the key lookups and never shown.
"""

from __future__ import annotations

import re
import shutil
import tomllib
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from . import config as _config
from .config import KNOWN_KEYS, Config, Profile, ProfileError, load_config
from .limits import CONFIG_MAX_BYTES, PORT_MAX, PROFILE_NAME_MAX_CHARS, PROFILE_PORT_FIRST, PROFILES_MAX

PROFILES_ROOT = _config.REPO_ROOT / "profiles"  # a module variable so tests and tools can point it elsewhere
NAME_RULE = (f"a profile name is a lower-case letter, then lower-case letters, digits, '_' or '-', "
             f"at most {PROFILE_NAME_MAX_CHARS} characters in all")
_NAME = re.compile(rf"[a-z][a-z0-9_-]{{0,{PROFILE_NAME_MAX_CHARS - 1}}}")
RESERVED = frozenset({"default", "profiles", "data", "config", "con", "prn", "aux", "nul",
                      *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})
README_NAME = "README.txt"
NETWORK_DEFAULT_PORT = _config.NetworkConfig.port  # 8765: the default profile's, never given to a profile


def _shown(text: object) -> str:
    return repr(text if isinstance(text, str) else type(text).__name__)[:48]


def validate_name(name: object) -> str:
    """The name, if it follows the rules; else ProfileError naming the rule it breaks. Never echoes a long or odd value."""
    if not isinstance(name, str) or not name:
        raise ProfileError(f"a profile name is required; {NAME_RULE}")
    if len(name) > PROFILE_NAME_MAX_CHARS:
        raise ProfileError(f"the profile name is {len(name)} characters; {NAME_RULE}")
    if name != name.strip() or name.rstrip(" .") != name:
        raise ProfileError(f"the profile name {_shown(name)} ends with a space or a dot; {NAME_RULE}")
    if any(c in name for c in "/\\:") or ".." in name or name.startswith("."):
        raise ProfileError(f"the profile name {_shown(name)} looks like a path; {NAME_RULE}")
    if not _NAME.fullmatch(name):
        raise ProfileError(f"the profile name {_shown(name)} is not allowed; {NAME_RULE}")
    if name in RESERVED:
        raise ProfileError(f"{name!r} is a reserved name (Windows device names, default, profiles, data, config); "
                           "choose another")
    return name


def profiles_root() -> Path:
    return PROFILES_ROOT


def _folder(name: str) -> Path:
    """profiles/<name> from the validated name only, confined: its resolved form must be a direct child of profiles/."""
    validate_name(name)
    base = profiles_root()
    try:
        path = base / name
        if path.resolve().parent != base.resolve():
            raise ProfileError(f"profile {name!r} resolves outside {base} (a link that points elsewhere is refused)")
    except ProfileError:
        raise
    except (OSError, ValueError, RuntimeError) as e:
        raise ProfileError(f"profile {name!r} cannot be located ({type(e).__name__})") from None
    return path


def resolve_profile(name: str) -> Profile:
    """The Profile for an EXISTING folder. ProfileError if the folder is missing or is not a folder."""
    path = _folder(name)
    try:
        exists, is_dir = path.exists(), path.is_dir()
    except OSError as e:
        raise ProfileError(f"profile {name!r} cannot be read ({e.strerror or type(e).__name__})") from None
    if not exists:
        raise ProfileError(f"profile {name!r} does not exist; create it with: python -m brain profile create {name}")
    if not is_dir:
        raise ProfileError(f"profile {name!r} is not usable: {path} is a file, not a folder")
    return Profile(name, path.resolve())


def load_profile_config(name: str) -> Config:
    """The checked Config of a profile: profiles/<name>/config.toml, relative paths confined to that folder."""
    profile = resolve_profile(name)
    try:
        return load_config(profile.config_path, profile=profile)
    except ProfileError:
        raise
    except _config.ConfigError:
        if not profile.config_path.exists() and not profile.config_path.is_symlink():
            raise ProfileError(f"profile {name!r} has no config.toml at {profile.config_path}; "
                               "restore it, or delete the folder by hand and create the profile again") from None
        raise


# ---- create ---------------------------------------------------------------------------------------------------


def _port_of(path: Path) -> int | None:
    """The [network] port of a config.toml, read leniently (a broken file simply has no known port)."""
    try:
        with open(path, "rb") as f:
            data = f.read(CONFIG_MAX_BYTES + 1)
        if len(data) > CONFIG_MAX_BYTES:
            return None
        port = tomllib.loads(data.decode("utf-8-sig")).get("network", {}).get("port")
    except (OSError, ValueError, AttributeError, RecursionError):  # swallowed: an unreadable file has no known port
        return None
    return port if isinstance(port, int) and not isinstance(port, bool) else None


def ports_in_use() -> set[int]:
    """The ports the root config.toml and every existing profile's config.toml say they listen on."""
    used = {NETWORK_DEFAULT_PORT}
    root_port = _port_of(_config.DEFAULT_CONFIG)
    if root_port is not None:
        used.add(root_port)
    for name, path in _profile_folders():
        port = _port_of(path / "config.toml")
        if port is not None:
            used.add(port)
    return used


def next_free_port() -> int:
    used = ports_in_use()
    for port in range(PROFILE_PORT_FIRST, PORT_MAX + 1):
        if port not in used:
            return port
    raise ProfileError("no free port is left to give a new profile", "profile create")


def _config_template(port: int) -> str:
    return f"""\
# A person's own Embodied Agent brain configuration. No secrets here; the API key goes in .env (same folder).
# Every key is checked at start; a misspelt one stops the brain (docs/profiles.md, docs/cost-estimate.md).

[network]
# Each person's brain needs its OWN port (and its own port forward on the body's side).
host = "127.0.0.1"
port = {port}

[body]
kind = "sim"

[models]
live_conversation = "claude-haiku-5-5"
careful_looking = "claude-sonnet-5-5"
offline = "claude-opus-5-5"
# Consent to real, paid model calls (`python -m brain chat --profile NAME --live`). Off until you turn it on.
allow_live_calls = false

[caps]
# Spend caps for THIS person's key: the brain stops asking the model when one is reached.
session_usd = 1.0
daily_usd = 5.0
hard_stop_usd = 10.0     # total since the brain started
requests_per_hour = 300
max_tokens = 1024

[egress]
# Default deny: the only outside destinations this brain may contact.
allowed_hosts = ["api.anthropic.com"]

[paths]
# Inside this folder. The link token is kept outside the repository (see docs/profiles.md).
data_dir = "data"

[logging]
# Full prompts and replies (including what the microphone heard) are saved to data/capture ONLY if you turn
# this on. It starts off for a profile; turn it on only if you want it for troubleshooting.
full_prompts = false
capture_max_mb = 200
capture_max_days = 14
"""


def _env_template() -> str:
    return ("# This person's keys. Put each key after its name, on the same line, with no spaces or quotes.\n"
            "# This file is git-ignored and is the ONLY place this profile reads keys from.\n"
            + "".join(f"{k}=\n" for k in KNOWN_KEYS))


def _readme_template(name: str, port: int) -> str:
    return f"""\
Profile {name} for the Embodied Agent brain

This folder is one person's own setup: config.toml, .env (the keys), data/ (logs, state, spend counters, captures).

Where to put your API key
  Open the file named .env in this folder and put your key after ANTHROPIC_API_KEY=, on the same line:
  the name, an equals sign, then your key, with no spaces or quotes. Only this file is used for this profile:
  an ANTHROPIC_API_KEY set elsewhere on the computer, or in the main project's .env, is ignored on purpose.
  Never send your key to anyone or put it in a document, chat or the config.toml.

Running it
  python -m brain info --profile {name}                    check the setup (shows key present or missing, never the key)
  python -m brain chat --profile {name}                    talk to the scripted model: free, no key needed
  python -m brain chat --profile {name} --live             the real model (needs the key and allow_live_calls = true)

Your port is {port} (config.toml [network]). It must differ from everyone else's on this computer.

Prompt capture is off. To turn it on set full_prompts = true in config.toml [logging]; it records everything said
to the agent, so leave it off unless you are troubleshooting.

Deleting this profile and its data is done by hand: close the brain, delete this folder, and delete the link token
folder %LOCALAPPDATA%\\EmbodiedAgent\\profiles\\{name}. No command does it for you.
"""


def create_profile(name: str) -> tuple[Path, int]:
    """Scaffold profiles/<name>/ and return (folder, port). Never overwrites: an existing profile is refused.
    A failure part-way (read-only folder, full disk) removes only the folder this call made."""
    path = _folder(name)
    base = profiles_root()
    try:
        if path.exists() or path.is_symlink():
            raise ProfileError(f"profile {name!r} already exists at {path}; it was not changed (to start again, "
                               "delete its folder by hand: see docs/profiles.md)", "profile create")
        port = next_free_port()
        base.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ProfileError(f"cannot prepare {base} ({e.strerror or type(e).__name__})", "profile create") from None
    try:
        path.mkdir()
    except FileExistsError:
        raise ProfileError(f"profile {name!r} already exists at {path}; it was not changed", "profile create") from None
    except OSError as e:
        raise ProfileError(f"cannot create {path} ({e.strerror or type(e).__name__})", "profile create") from None
    try:
        (path / "data").mkdir()
        (path / "config.toml").write_text(_config_template(port), encoding="utf-8")
        (path / ".env").write_text(_env_template(), encoding="utf-8")
        (path / README_NAME).write_text(_readme_template(name, port), encoding="utf-8")
    except OSError as e:
        shutil.rmtree(path, ignore_errors=True)  # our own half-made folder, made a moment ago
        raise ProfileError(f"could not write the profile files in {path} ({e.strerror or type(e).__name__}); "
                           "nothing was kept", "profile create") from None
    return path, port


# ---- list and show ----------------------------------------------------------------------------------------------


def _profile_folders() -> list[tuple[str, Path]]:
    """(name, folder) of each valid-named folder under profiles/, sorted, at most PROFILES_MAX."""
    base = profiles_root()
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name)
    except OSError:  # swallowed: no profiles folder yet (or unreadable) means no profiles
        return []
    out: list[tuple[str, Path]] = []
    for p in entries:
        try:
            if p.is_dir() and _NAME.fullmatch(p.name) and p.name not in RESERVED:
                out.append((p.name, p))
        except OSError:  # swallowed: an entry that cannot be examined is not listed
            continue
        if len(out) >= PROFILES_MAX:
            break
    return out


def last_used(profile: Profile, config: Config | None = None) -> str:
    """When the profile last wrote its state (state.json), or 'never'. One stat call."""
    data = config.paths.data_dir if config is not None else (profile.root or Path()) / "data"
    try:
        stamp = (data / "state.json").stat().st_mtime
        return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):  # swallowed: no state file yet
        return "never"


def list_lines() -> list[str]:
    """One line per profile; a profile whose config does not load says so instead of failing the list."""
    lines: list[str] = []
    for name, _folder_path in _profile_folders():
        try:
            profile = resolve_profile(name)
            cfg = load_profile_config(name)
            keys = profile.key_presence()
        except _config.ConfigError as e:
            lines.append(f"{name}: not usable: {str(e)[:160]}")
            continue
        key = "ANTHROPIC_API_KEY"
        lines.append(f"{name}: port {cfg.network.port}; live calls {'allowed' if cfg.models.allow_live_calls else 'off'}; "
                     f"{key} {'present' if keys.get(key) else 'missing'}; "
                     f"prompt capture {'on' if cfg.logging.full_prompts else 'off'}; last used {last_used(profile, cfg)}")
    return lines


def describe(config: Config) -> list[str]:
    """The effective settings of a loaded config as text lines. No secret: key names and present/missing only."""
    from .transport.auth import profile_secrets_dir

    p = config.profile
    secrets = config.paths.secrets_dir or (profile_secrets_dir(p.name) if p.name else None)
    c = config.caps
    lines = [f"profile: {p.label}", f"config file: {config.source}", f"data dir: {config.paths.data_dir}",
             f"secrets dir (link token): {secrets if secrets else '(the default private folder)'}",
             f"network: {config.network.host}:{config.network.port}", f"body: {config.body.kind}",
             f"models: live {config.models.live_conversation}, careful {config.models.careful_looking}, "
             f"offline {config.models.offline}; live calls {'ALLOWED' if config.models.allow_live_calls else 'off'}",
             f"caps: session ${c.session_usd:g}, daily ${c.daily_usd:g}, hard stop ${c.hard_stop_usd:g}, "
             f"{c.requests_per_hour} requests/hour, {c.max_tokens} tokens/request",
             f"egress allowed hosts: {', '.join(config.egress.allowed_hosts) or '(none)'}",
             f"prompt capture: {'on' if config.logging.full_prompts else 'off'} "
             f"(max {config.logging.capture_max_mb} MB, {config.logging.capture_max_days} days)",
             f"keys are read from: {p.key_source()}"]
    lines += [f"key {k}: {'present' if v else 'missing'}" for k, v in p.key_presence().items()]
    return lines


def show_lines(name: str) -> list[str]:
    return describe(load_profile_config(name))


def profile_command(args: Mapping[str, object]) -> int:
    """Run `profile create|list|show` from parsed arguments; prints, returns 0. Errors are ProfileError."""
    action = args.get("action")
    if action == "create":
        path, port = create_profile(str(args.get("name")))
        name = path.name
        print(f"created profile {name} at {path}")
        print(f"  port {port}; caps session $1, daily $5, hard stop $10; live calls off; prompt capture off")
        print(f"  put the API key in {path / '.env'} (see {path / README_NAME}), then: python -m brain info --profile {name}")
        return 0
    if action == "list":
        lines = list_lines()
        print("\n".join(lines) if lines else f"no profiles yet; create one with: python -m brain profile create NAME")
        return 0
    if action == "show":
        print("\n".join(show_lines(str(args.get("name")))))
        return 0
    raise ProfileError("say what to do: profile create NAME, profile list, or profile show NAME", "profile")
