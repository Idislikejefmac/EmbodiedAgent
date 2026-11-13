"""python -m brain: print the version, load config, and report which keys exist."""

from __future__ import annotations

from . import __version__
from .config import REPO_ROOT, key_presence, load_config


def main() -> int:
    print(f"embodied-agent brain {__version__}")
    config = load_config()
    network = config.get("network", {})
    print(f"config: ok (host {network.get('host')}, port {network.get('port')})")
    data_dir = REPO_ROOT / config.get("paths", {}).get("data_dir", "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"data dir: {data_dir}")
    for name, present in key_presence().items():
        print(f"key {name}: {'present' if present else 'missing'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

