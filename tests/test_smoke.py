import subprocess
import sys

import brain
from brain.config import key_presence, load_config, parse_env_file


def test_version_is_set():
    assert brain.__version__


def test_python_dash_m_brain_prints_version_line():
    out = subprocess.run(
        [sys.executable, "-m", "brain"], capture_output=True, text=True, check=True
    ).stdout
    assert out.splitlines()[0] == f"embodied-agent brain {brain.__version__}"
    assert "config: ok" in out


def test_config_loads_and_has_an_egress_allowlist():
    cfg = load_config()  # a typed, frozen Config since H-05
    assert isinstance(cfg.network.port, int)
    hosts = cfg.egress.allowed_hosts
    assert isinstance(hosts, tuple) and all(isinstance(h, str) for h in hosts)


def test_key_presence_reports_names_never_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-secret-value\nDEEPGRAM_API_KEY=\n")
    presence = key_presence(env_path=env, environ={})
    assert presence["ANTHROPIC_API_KEY"] is True
    assert presence["DEEPGRAM_API_KEY"] is False
    assert all(isinstance(v, bool) for v in presence.values())
    assert "sk-secret-value" not in repr(presence)


def test_parse_env_file_missing_is_empty(tmp_path):
    assert parse_env_file(tmp_path / "nope") == {}

