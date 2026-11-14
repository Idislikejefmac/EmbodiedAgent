"""The harness's blast radius: a temporary data folder with canary secrets (plan card X-01 step 10).

Cases never see the real data folder, the real `.env` or the real token store. The sandbox holds:

    <root>/data/              data_dir for the case's config
    <root>/secrets/link_token  a canary pairing token in a folder private to the current user
    <root>/.env                canary API keys
    <root>/config.toml         a config pointing at the folders above, with no egress hosts
    <root>/tmp/                tempfile's folder while the run lasts

Canary values are random per run and look like the real thing, so a leak is recognisable: the
secret scan looks for each of them in every log record, exception, case output and results file.
"""

from __future__ import annotations

import os
import secrets
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from brain.config import DEFAULT_ENV, KNOWN_KEYS, REPO_ROOT, Config, parse_config
from brain.transport import auth

SANDBOX_PORT = 8765  # a valid [network] port (the gate refuses 0); nothing listens on it
CANARY_ENV = {name: f"sk-canary-{name.split('_')[0].lower()}-" for name in KNOWN_KEYS}


@dataclass
class Sandbox:
    root: Path
    canaries: dict[str, str] = field(default_factory=dict)  # label -> secret text
    raw_config: dict[str, Any] = field(default_factory=dict)  # the sandbox's config table, before the gate
    _saved_env: dict[str, str | None] = field(default_factory=dict)
    _saved_tempdir: str | None = None
    _saved_bytecode: bool = False

    @property
    def config(self) -> Config:
        """The sandbox's config through the config gate, relative paths under the sandbox root."""
        return self.typed(self.raw_config)

    def typed(self, raw: dict[str, Any]) -> Config:
        """Any config table through the gate as if it were the sandbox's config.toml (ConfigError if bad)."""
        return parse_config(raw, source=str(self.config_path), base=self.root)

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def secrets_dir(self) -> Path:
        return self.root / "secrets"

    @property
    def env_path(self) -> Path:
        return self.root / ".env"

    @property
    def config_path(self) -> Path:
        return self.root / "config.toml"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    @property
    def token_hex(self) -> str:
        return self.canaries["link_token"]

    @property
    def token(self) -> bytes:
        return bytes.fromhex(self.token_hex)

    def sim_body(self, **options: Any) -> Any:
        """A SimBody reading the sandbox's copy of the default media (generating it per case is slow)."""
        from brain.body.sim import SimBody

        return SimBody(image_dir=self.root / "sim" / "images", audio_dir=self.root / "sim" / "audio", **options)

    def fresh_dir(self, name: str) -> Path:
        """A new empty folder inside the sandbox for one case."""
        d = Path(tempfile.mkdtemp(prefix=f"{name[:20]}-", dir=self.tmp_dir))
        return d


def protected_roots() -> list[Path]:
    """What a case may never touch: the real data folder, the real .env and the real token store."""
    return [REPO_ROOT / "data", DEFAULT_ENV, auth.default_secrets_dir()]


def create(parent: Path | None = None) -> Sandbox:
    """Make the sandbox (one per run). Uses the token store's own code to make the private secrets
    folder (on Windows that runs icacls once, here in setup, outside any case)."""
    root = Path(tempfile.mkdtemp(prefix="ea-chaos-", dir=parent))
    sb = Sandbox(root)
    sb.data_dir.mkdir()
    sb.tmp_dir.mkdir()
    from brain.body import sim_assets

    sim_assets.write_default_media(root / "sim" / "images", root / "sim" / "audio")
    auth.ensure_store(sb.secrets_dir)
    sb.canaries["link_token"] = secrets.token_hex(auth.TOKEN_BYTES)
    (sb.secrets_dir / auth.TOKEN_FILE_NAME).write_text(sb.canaries["link_token"] + "\n", encoding="ascii")
    lines = []
    for name, prefix in CANARY_ENV.items():
        value = prefix + secrets.token_hex(20)
        sb.canaries[name] = value
        lines.append(f"{name}={value}")
    sb.env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # data_dir is relative: the config gate confines it to the folder that holds config.toml (H-05), which
    # here is the sandbox root. The port is never bound (server cases start their own on port 0).
    sb.raw_config = {
        "network": {"host": "127.0.0.1", "port": SANDBOX_PORT},
        "body": {"kind": "sim"},
        "egress": {"allowed_hosts": []},
        "paths": {"data_dir": "data", "secrets_dir": str(sb.secrets_dir)},
    }
    sb.config_path.write_text(  # TOML literal strings ('...') take Windows paths as they are
        f'[network]\nhost = "127.0.0.1"\nport = {SANDBOX_PORT}\n\n[body]\nkind = "sim"\n\n[egress]\nallowed_hosts = []\n\n'
        f"[paths]\ndata_dir = 'data'\nsecrets_dir = '{sb.secrets_dir}'\n", encoding="utf-8")
    return sb


def enter(sb: Sandbox) -> None:
    """For the length of the run: canary keys in the environment, temp files in the sandbox, no .pyc
    writes (they would land outside it)."""
    for name in CANARY_ENV:
        sb._saved_env[name] = os.environ.get(name)
        os.environ[name] = sb.canaries[name]
    sb._saved_tempdir = tempfile.tempdir
    tempfile.tempdir = str(sb.tmp_dir)
    sb._saved_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True


def leave(sb: Sandbox) -> None:
    for name, old in sb._saved_env.items():
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old
    sb._saved_env.clear()
    tempfile.tempdir = sb._saved_tempdir
    sys.dont_write_bytecode = sb._saved_bytecode


def destroy(sb: Sandbox) -> None:
    shutil.rmtree(sb.root, ignore_errors=True)
