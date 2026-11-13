"""Load config.toml and report which API keys are present, never their values."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config.toml"
DEFAULT_ENV = REPO_ROOT / ".env"

# Keys the brain may use. Presence is reported by name only.
KNOWN_KEYS = ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "DEEPGRAM_API_KEY")


def load_config(path: Path | str | None = None) -> dict:
    """Read config.toml (default: the one at the repo root)."""
    p = Path(path) if path else DEFAULT_CONFIG
    with open(p, "rb") as f:
        return tomllib.load(f)


def parse_env_file(path: Path | str | None = None) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Missing file gives an empty dict."""
    p = Path(path) if path else DEFAULT_ENV
    if not p.exists():
        return {}
    values: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def key_presence(
    env_path: Path | str | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, bool]:
    """Return {key name: present?} for the known keys. Values are never returned."""
    env_file = parse_env_file(env_path)
    environ = os.environ if environ is None else environ
    return {
        name: bool(environ.get(name) or env_file.get(name))
        for name in KNOWN_KEYS
    }
