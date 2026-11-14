"""The config gate (H-05): read config.toml and .env, check every key, and hand out a frozen, typed Config.

    config = load_config()            # config.toml at the repo root; ConfigError if anything is wrong
    config.network.port               # an int from PORT_MIN to PORT_MAX, already checked
    key_presence()                    # which API keys exist in .env or the environment, never their values

Inputs and limits: config.toml is read only if it is at most CONFIG_MAX_BYTES, .env at most
ENV_FILE_MAX_BYTES (checked before parsing). Every key has a type and a range (SCHEMA below); money caps
are bounded by SPEND_CAP_SANITY_MAX. A key the brain does not know STOPS it (DEC-106): the error names
the key and suggests the nearest valid one, because a misspelt cap name would otherwise silently remove
the cap. A missing key takes its default, which is the safe value (the cost-estimate caps, deny-all
egress, data under the repository).

Path rule (S7, 10-F09): `[paths] data_dir` and `[body.sim]` folders must be relative and are resolved
under the folder that holds config.toml (the repo root for the real file); they may not contain `..`
and may not leave that folder (a link that points out is refused too). `[paths] secrets_dir` may also
be absolute (environment variables such as %LOCALAPPDATA% are expanded) but then must be inside the
user profile (or under the config's folder), so the link token stays in a folder private to the user.

Returns and invariants: a Config whose values all passed the schema; paths are absolute and resolved.
Raises: ConfigError (a ValueError) with ONE message naming the file, the key and the reason. Values of
.env are never part of a message. Side effects: none (reads only).
"""

from __future__ import annotations

import difflib
import math
import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from .limits import (CONFIG_MAX_BYTES, CONFIG_PATH_MAX_CHARS, CONFIG_TEXT_MAX_CHARS, EGRESS_HOSTS_MAX,
                     ENV_FILE_MAX_BYTES, ENV_KEY_MAX_CHARS, ENV_VALUE_MAX_CHARS, LLM_REQUESTS_PER_HOUR_MAX,
                     LOG_CAPTURE_DAYS_MAX, LOG_CAPTURE_MB_MAX, MODEL_NAME_MAX_CHARS, OUTPUT_TOKENS_SANITY_MAX,
                     PORT_MAX, PORT_MIN, REQUEST_LOG_RETENTION_DAYS, SPEND_CAP_SANITY_MAX)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config.toml"
DEFAULT_ENV = REPO_ROOT / ".env"

# Keys the brain may use. Presence is reported by name only.
KNOWN_KEYS = ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "DEEPGRAM_API_KEY")
BODY_KINDS = ("sim", "phone")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(ValueError):
    """config.toml or .env cannot be used. `str(e)` is the one message to show: file, key and reason."""

    def __init__(self, source: str, key: str, reason: str):
        self.source, self.key, self.reason = source, key, reason
        super().__init__(f"{source}: {key}: {reason}" if key else f"{source}: {reason}")


class _Bad(Exception):
    """A value failed its check; the gate adds the file and the key."""


# ---- the typed result (made only by parse_config) ----------------------------------------------------------


@dataclass(frozen=True)
class NetworkConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass(frozen=True)
class SimBodyConfig:
    image_dir: Path | None = None  # None: SimBody's built-in synthetic media
    audio_dir: Path | None = None


@dataclass(frozen=True)
class BodyConfig:
    kind: str = "sim"
    sim: SimBodyConfig = field(default_factory=SimBodyConfig)


@dataclass(frozen=True)
class ModelsConfig:
    live_conversation: str = "claude-haiku-5-5"
    careful_looking: str = "claude-sonnet-5-5"
    offline: str = "claude-opus-5-5"


@dataclass(frozen=True)
class CapsConfig:
    """Interim spend caps (docs/cost-estimate.md, DEC-068). Enforced from card P0-07 / P1-04."""

    session_usd: float = 1.0
    daily_usd: float = 5.0
    hard_stop_usd: float = 10.0  # total since the brain started
    requests_per_hour: int = 300
    max_tokens: int = 1024  # per model request


@dataclass(frozen=True)
class EgressConfig:
    allowed_hosts: tuple[str, ...] = ()  # default deny


@dataclass(frozen=True)
class PathsConfig:
    data_dir: Path
    secrets_dir: Path | None = None  # None: the private folder in the user profile (brain/transport/auth.py)


@dataclass(frozen=True)
class LoggingConfig:
    """Full-prompt capture (DEC-025: on during development). Only checked and carried here; card P0-07
    implements the capture."""

    full_prompts: bool = True
    capture_max_mb: int = 200
    capture_max_days: int = REQUEST_LOG_RETENTION_DAYS


@dataclass(frozen=True)
class Config:
    network: NetworkConfig
    body: BodyConfig
    models: ModelsConfig
    caps: CapsConfig
    egress: EgressConfig
    paths: PathsConfig
    logging: LoggingConfig
    source: str  # where it came from (a file path), for messages
    base: Path  # the folder relative paths were resolved under


# ---- value checks ---------------------------------------------------------------------------------------------

Check = Callable[[object], object]


def _kind(v: object) -> str:
    names = {type(None): "null", bool: "true/false", int: "a whole number", float: "a number", str: "text",
             list: "a list", dict: "a table"}
    return names.get(type(v), type(v).__name__)


def _text(max_chars: int) -> Check:
    def check(v: object) -> str:
        if not isinstance(v, str):
            raise _Bad(f"must be text, not {_kind(v)}")
        if not 1 <= len(v) <= max_chars:
            raise _Bad(f"must be 1 to {max_chars} characters, not {len(v)}")
        if not v.isprintable() or any(c.isspace() for c in v):
            raise _Bad("must be printable text without spaces or control characters")
        return v
    return check


def _path_text(v: object) -> str:
    if not isinstance(v, str):
        raise _Bad(f"must be a folder name (text), not {_kind(v)}")
    if not 1 <= len(v) <= CONFIG_PATH_MAX_CHARS:
        raise _Bad(f"must be 1 to {CONFIG_PATH_MAX_CHARS} characters, not {len(v)}")
    if not v.isprintable():
        raise _Bad("must not contain control or invisible characters")
    return v


def _whole(low: int, high: int) -> Check:
    def check(v: object) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise _Bad(f"must be a whole number from {low} to {high}, not {_kind(v)}")
        if not low <= v <= high:
            raise _Bad(f"must be from {low} to {high}, not {v if abs(v) < 10**12 else 'a huge number'}")
        return v
    return check


def _money(v: object) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _Bad(f"must be a number of US dollars from 0 to {SPEND_CAP_SANITY_MAX:g}, not {_kind(v)}")
    if not math.isfinite(v) or not 0 <= v <= SPEND_CAP_SANITY_MAX:
        shown = v if math.isfinite(v) and abs(v) < 10**12 else ("NaN" if v != v else "a huge number")
        raise _Bad(f"must be from 0 to {SPEND_CAP_SANITY_MAX:g} US dollars, not {shown}")
    return float(v)


def _flag(v: object) -> bool:
    if not isinstance(v, bool):
        raise _Bad(f"must be true or false, not {_kind(v)}")
    return v


def _choice(options: tuple[str, ...]) -> Check:
    def check(v: object) -> str:
        if not isinstance(v, str) or v not in options:
            shown = repr(v) if isinstance(v, str) and len(v) <= 40 and v.isprintable() else _kind(v)
            raise _Bad(f"must be one of {', '.join(repr(o) for o in options)}, not {shown}")
        return v
    return check


def _hosts(v: object) -> tuple[str, ...]:
    if not isinstance(v, list):
        raise _Bad(f'must be a list of "host" or "host:port" strings, not {_kind(v)}')
    if len(v) > EGRESS_HOSTS_MAX:
        raise _Bad(f"has {len(v)} entries; at most {EGRESS_HOSTS_MAX}")
    for i, entry in enumerate(v):
        if not isinstance(entry, str):
            raise _Bad(f'entry {i + 1} must be a "host" or "host:port" string, not {_kind(entry)}')
    from .egress import EgressPolicy  # here, not at the top: egress imports this module

    try:
        EgressPolicy.from_hosts(list(v))
    except ValueError as e:
        raise _Bad(str(e)[:200]) from None
    return tuple(v)


# Every key config.toml may hold. A dict is a sub-table. Unknown keys stop the brain (DEC-106).
SCHEMA: dict[str, dict[str, object]] = {
    "network": {"host": _text(CONFIG_TEXT_MAX_CHARS), "port": _whole(PORT_MIN, PORT_MAX)},
    "body": {"kind": _choice(BODY_KINDS), "sim": {"image_dir": _path_text, "audio_dir": _path_text}},
    "models": {k: _text(MODEL_NAME_MAX_CHARS) for k in ("live_conversation", "careful_looking", "offline")},
    "caps": {"session_usd": _money, "daily_usd": _money, "hard_stop_usd": _money,
             "requests_per_hour": _whole(1, LLM_REQUESTS_PER_HOUR_MAX), "max_tokens": _whole(1, OUTPUT_TOKENS_SANITY_MAX)},
    "egress": {"allowed_hosts": _hosts},
    "paths": {"data_dir": _path_text, "secrets_dir": _path_text},
    "logging": {"full_prompts": _flag, "capture_max_mb": _whole(1, LOG_CAPTURE_MB_MAX),
                "capture_max_days": _whole(1, LOG_CAPTURE_DAYS_MAX)},
}


def schema_paths(schema: Mapping[str, object] | None = None, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every section and key path in SCHEMA, for the manifest drift check and tests."""
    out: list[tuple[str, ...]] = []
    for key, sub in (SCHEMA if schema is None else schema).items():
        out.append(prefix + (key,))
        if isinstance(sub, dict):
            out += schema_paths(sub, prefix + (key,))
    return out


def _where(path: tuple[str, ...]) -> str:
    return f"[{'.'.join(path[:-1])}] {path[-1]}" if len(path) > 1 else f"[{path[0]}]"


def _unknown(source: str, path: tuple[str, ...], valid: list[str]) -> NoReturn:
    key = path[-1]
    shown = key if key.isprintable() and len(key) <= 64 else f"{key[:64]!r}"
    near = difflib.get_close_matches(key, valid, n=1, cutoff=0.6)
    hint = f"did you mean '{near[0]}'?" if near else f"valid keys here: {', '.join(sorted(valid))}"
    where = f"[{'.'.join(path[:-1])}] " if len(path) > 1 else ""
    raise ConfigError(source, f"{where}{shown}", f"unknown key; {hint} (the brain stops on unknown keys so a "
                                                 f"misspelt setting cannot be silently ignored)")


def _walk(raw: Mapping[str, object], schema: Mapping[str, object], source: str,
          prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], object]:
    """Check every key of `raw` against `schema`; return {key path: checked value}."""
    out: dict[tuple[str, ...], object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ConfigError(source, _where(prefix + ("?",)), "key names must be text")
        path = prefix + (key,)
        if key not in schema:
            _unknown(source, path, list(schema))
        sub = schema[key]
        if isinstance(sub, dict):
            if not isinstance(value, dict):
                raise ConfigError(source, f"[{'.'.join(path)}]", f"must be a table, not {_kind(value)}")
            out.update(_walk(value, sub, source, path))
        else:
            try:
                out[path] = sub(value) if callable(sub) else value
            except _Bad as e:
                raise ConfigError(source, _where(path), str(e)) from None
    return out


def _inside(path: Path, root: Path) -> bool:
    return path != root and root in path.parents


def _confine(text: str, where: str, source: str, base: Path, *, profile_ok: bool) -> Path:
    """The path rule in the module docstring."""
    expanded = os.path.expanduser(os.path.expandvars(text)) if profile_ok else text
    if profile_ok and "%" in expanded:
        raise ConfigError(source, where, "names an environment variable that is not set")
    p = Path(expanded)
    if ".." in p.parts or ".." in re.split(r"[\\/]", expanded):
        raise ConfigError(source, where, "may not contain '..' (paths must stay inside their folder)")
    if p.anchor and not profile_ok:
        raise ConfigError(source, where, f"must be a folder relative to {base} (absolute paths are refused, so "
                                         "the data cannot be moved out of the project)")
    try:
        resolved = p.resolve() if p.anchor else (base / p).resolve()
        home = Path.home().resolve()
    except (OSError, ValueError, RuntimeError) as e:
        raise ConfigError(source, where, f"is not a usable path ({type(e).__name__})") from None
    if p.anchor:
        if not (_inside(resolved, home) or _inside(resolved, base)):
            raise ConfigError(source, where, f"an absolute folder must be inside your user profile ({home}), "
                                             "where other accounts cannot reach it")
        return resolved
    if not _inside(resolved, base):
        raise ConfigError(source, where, f"must be a folder inside {base} (it resolves outside it)")
    return resolved


def parse_config(raw: Mapping[str, object], *, source: str = "config", base: Path | None = None) -> Config:
    """THE GATE: turn a parsed config table into a Config, or raise ConfigError. `base` is the folder
    relative paths are resolved under (default: the repo root)."""
    if not isinstance(raw, Mapping):
        raise ConfigError(source, "", f"must be a table of sections, not {_kind(raw)}")
    root = (REPO_ROOT if base is None else Path(base)).resolve()
    v = _walk(raw, SCHEMA, source)

    def get[T](path: tuple[str, ...], default: T) -> T:
        return v.get(path, default)  # type: ignore[return-value]  # _walk checked the type for this path

    def folder(path: tuple[str, ...], *, profile_ok: bool = False) -> Path | None:
        text = v.get(path)
        return None if text is None else _confine(str(text), _where(path), source, root, profile_ok=profile_ok)

    d = CapsConfig()
    m = ModelsConfig()
    lg = LoggingConfig()
    data_dir = folder(("paths", "data_dir")) or _confine("data", "[paths] data_dir", source, root, profile_ok=False)
    return Config(
        network=NetworkConfig(get(("network", "host"), NetworkConfig.host), get(("network", "port"), NetworkConfig.port)),
        body=BodyConfig(get(("body", "kind"), BodyConfig.kind),
                        SimBodyConfig(folder(("body", "sim", "image_dir")), folder(("body", "sim", "audio_dir")))),
        models=ModelsConfig(get(("models", "live_conversation"), m.live_conversation),
                            get(("models", "careful_looking"), m.careful_looking), get(("models", "offline"), m.offline)),
        caps=CapsConfig(get(("caps", "session_usd"), d.session_usd), get(("caps", "daily_usd"), d.daily_usd),
                        get(("caps", "hard_stop_usd"), d.hard_stop_usd),
                        get(("caps", "requests_per_hour"), d.requests_per_hour), get(("caps", "max_tokens"), d.max_tokens)),
        egress=EgressConfig(get(("egress", "allowed_hosts"), ())),
        paths=PathsConfig(data_dir, folder(("paths", "secrets_dir"), profile_ok=True)),
        logging=LoggingConfig(get(("logging", "full_prompts"), lg.full_prompts),
                              get(("logging", "capture_max_mb"), lg.capture_max_mb),
                              get(("logging", "capture_max_days"), lg.capture_max_days)),
        source=source, base=root)


def as_config(config: Config | Mapping[str, object] | None, *, base: Path | None = None) -> Config:
    """A Config as it is, or a raw table (tests, tools) through the gate; None gives the defaults."""
    if isinstance(config, Config):
        return config
    return parse_config({} if config is None else config, source="configuration", base=base)


def _read_capped(path: Path, limit: int, what: str) -> bytes:
    """The file's bytes, refusing a missing, unreadable or larger-than-`limit` file before parsing it."""
    try:
        with open(path, "rb") as f:
            data = f.read(limit + 1)
    except FileNotFoundError:
        raise ConfigError(str(path), "", f"{what} not found") from None
    except OSError as e:
        raise ConfigError(str(path), "", f"{what} cannot be read ({e.strerror or type(e).__name__})") from None
    if len(data) > limit:
        raise ConfigError(str(path), "", f"{what} is larger than {limit} bytes; refusing to read it")
    return data


def _decode(data: bytes, source: str, what: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ConfigError(source, "", f"{what} is not UTF-8 text (byte {e.start})") from None


def load_config(path: Path | str | None = None) -> Config:
    """Read and check config.toml (default: the one at the repo root). Relative paths in it resolve under
    the folder that holds it. Raises ConfigError (see the module docstring)."""
    p = Path(path) if path else DEFAULT_CONFIG
    source = str(p)
    text = _decode(_read_capped(p, CONFIG_MAX_BYTES, "config file"), source, "config file")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(source, "", f"is not valid TOML: {str(e)[:200]}") from None
    except RecursionError:
        raise ConfigError(source, "", "is nested too deeply") from None
    return parse_config(raw, source=source, base=p.resolve().parent)


def parse_env_file(path: Path | str | None = None) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. A missing file gives an empty dict. Blank lines and lines
    starting with # are skipped; any other line must be NAME=VALUE with a plain NAME (letters, digits, _),
    a value of at most ENV_VALUE_MAX_CHARS without control characters. Raises ConfigError naming the
    line, never its value."""
    p = Path(path) if path else DEFAULT_ENV
    if not p.exists():
        return {}
    source = str(p)
    text = _decode(_read_capped(p, ENV_FILE_MAX_BYTES, ".env file"), source, ".env file")
    values: dict[str, str] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(source, f"line {n}", "expected NAME=VALUE")
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not _ENV_KEY.match(key) or len(key) > ENV_KEY_MAX_CHARS:
            raise ConfigError(source, f"line {n}", f"the name before '=' must be letters, digits and _ "
                                                   f"(at most {ENV_KEY_MAX_CHARS})")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if len(value) > ENV_VALUE_MAX_CHARS:
            raise ConfigError(source, f"line {n} ({key})", f"the value is longer than {ENV_VALUE_MAX_CHARS} characters")
        if not value.isprintable():
            raise ConfigError(source, f"line {n} ({key})", "the value holds a control or invisible character")
        values[key] = value
    return values


def key_presence(
    env_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, bool]:
    """Return {key name: present?} for the known keys. Values are never returned."""
    env_file = parse_env_file(env_path)
    env = os.environ if environ is None else environ
    return {
        name: bool(env.get(name) or env_file.get(name))
        for name in KNOWN_KEYS
    }
