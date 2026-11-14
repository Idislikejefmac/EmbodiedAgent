"""H-05: the config gate (T-P0-04-006 to T-P0-04-011; DEC-106).

config.toml and .env become a frozen, typed Config at one gate; a wrong type, a value out of range, a
null, an unknown key, an oversized file or a path that leaves the project is refused with ONE
ConfigError naming the file, the key and the reason; `python -m brain` turns it into exit code 2 and a
message, never a traceback.
"""

import dataclasses
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from brain import __main__ as cli
from brain.config import (DEFAULT_CONFIG, REPO_ROOT, SCHEMA, Config, ConfigError, as_config, key_presence,
                          load_config, parse_config, parse_env_file, schema_paths)
from brain.limits import (CONFIG_MAX_BYTES, CONFIG_PATH_MAX_CHARS, ENV_FILE_MAX_BYTES, ENV_KEY_MAX_CHARS,
                          ENV_VALUE_MAX_CHARS, PORT_MAX, PORT_MIN, SPEND_CAP_SANITY_MAX)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def refused(tmp_path, text):
    path = write(tmp_path, text)
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert str(path) in str(e.value), "the error must name the file"
    return e.value


# ---- the real file and the typed result ------------------------------------------------------------------


def test_the_project_config_passes_the_gate_and_is_frozen():
    cfg = load_config()
    assert isinstance(cfg, Config) and cfg.source == str(DEFAULT_CONFIG)
    assert PORT_MIN <= cfg.network.port <= PORT_MAX and cfg.egress.allowed_hosts == ("api.anthropic.com",)
    assert cfg.paths.data_dir == (REPO_ROOT / "data").resolve()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.network.port = 1  # type: ignore[misc]


def test_the_new_caps_and_logging_keys_are_in_config_toml_with_jeffs_values():
    """DEC-068 (docs/cost-estimate.md) and DEC-025: carried and checked here; enforced from card P0-07."""
    cfg = load_config()
    assert (cfg.caps.session_usd, cfg.caps.daily_usd, cfg.caps.hard_stop_usd) == (1.0, 5.0, 10.0)
    assert (cfg.caps.requests_per_hour, cfg.caps.max_tokens) == (300, 1024)
    assert cfg.logging.full_prompts is True and cfg.logging.capture_max_mb >= 1 and cfg.logging.capture_max_days >= 1


def test_missing_keys_take_the_safe_defaults():
    cfg = parse_config({})
    assert cfg.egress.allowed_hosts == () and cfg.body.kind == "sim"  # deny-all egress, no hardware
    assert cfg.caps.hard_stop_usd <= SPEND_CAP_SANITY_MAX and cfg.paths.secrets_dir is None


def test_schema_paths_cover_config_toml():
    import tomllib

    def paths(d, prefix=()):
        for k, v in d.items():
            yield prefix + (k,)
            if isinstance(v, dict):
                yield from paths(v, prefix + (k,))
    known = set(schema_paths())
    assert set(paths(tomllib.loads(DEFAULT_CONFIG.read_text(encoding="utf-8")))) <= known
    assert set(SCHEMA) == {"network", "body", "models", "caps", "egress", "paths", "logging"}


# ---- one clear error per problem ----------------------------------------------------------------------------


@pytest.mark.parametrize("text, key, reason", [
    ("[network]\nport = 65536\n", "[network] port", "from 1 to 65535"),
    ("[network]\nport = 0\n", "[network] port", "from 1 to 65535"),
    ('[network]\nport = "8765"\n', "[network] port", "whole number"),
    ("[network]\nport = 8765.0\n", "[network] port", "whole number"),
    ("[network]\nport = true\n", "[network] port", "whole number"),
    ('[network]\nhost = ""\n', "[network] host", "1 to"),
    ('[network]\nhost = "a b"\n', "[network] host", "without spaces"),
    ("[caps]\nsession_usd = -0.01\n", "[caps] session_usd", "from 0 to"),
    ("[caps]\ndaily_usd = 100.01\n", "[caps] daily_usd", "from 0 to"),
    ("[caps]\nhard_stop_usd = nan\n", "[caps] hard_stop_usd", "NaN"),
    ("[caps]\nhard_stop_usd = inf\n", "[caps] hard_stop_usd", "huge"),
    ('[caps]\nsession_usd = "1"\n', "[caps] session_usd", "US dollars"),
    ("[caps]\nrequests_per_hour = 0\n", "[caps] requests_per_hour", "from 1"),
    ("[caps]\nmax_tokens = 1.5\n", "[caps] max_tokens", "whole number"),
    ('[egress]\nallowed_hosts = "api.anthropic.com"\n', "[egress] allowed_hosts", "list"),
    ("[egress]\nallowed_hosts = [443]\n", "[egress] allowed_hosts", "entry 1"),
    ('[egress]\nallowed_hosts = ["*.example.org"]\n', "[egress] allowed_hosts", "exact host name"),
    ('[body]\nkind = "robot"\n', "[body] kind", "'sim', 'phone'"),
    ('[logging]\nfull_prompts = "yes"\n', "[logging] full_prompts", "true or false"),
    ("[logging]\ncapture_max_days = 0\n", "[logging] capture_max_days", "from 1"),
    ('network = 5\n', "[network]", "must be a table"),
    ('[body]\nsim = "x"\n', "[body.sim]", "must be a table"),
])
def test_wrong_type_or_range_is_one_clear_error(tmp_path, text, key, reason):
    e = refused(tmp_path, text)
    assert e.key == key and reason in e.reason, str(e)
    assert str(e).count("\n") == 0


@pytest.mark.parametrize("text, wrong, suggestion", [
    ("[caps]\nsesion_usd = 1.0\n", "sesion_usd", "session_usd"),
    ("[caps]\ndaily_cap = 1.0\n", "daily_cap", "daily_usd"),
    ("[netwrk]\nport = 8765\n", "netwrk", "network"),
    ("[paths]\ndatadir = 'data'\n", "datadir", "data_dir"),
    ("[body.sim]\nimage_dirs = 'x'\n", "image_dirs", "image_dir"),
])
def test_unknown_keys_stop_the_brain_and_suggest_the_nearest(tmp_path, text, wrong, suggestion):
    """DEC-106: a misspelt cap name would otherwise silently remove the cap."""
    e = refused(tmp_path, text)
    assert wrong in e.key and f"did you mean '{suggestion}'?" in e.reason, str(e)


def test_an_unknown_key_with_nothing_near_lists_the_valid_ones(tmp_path):
    e = refused(tmp_path, "[caps]\nzzzz = 1\n")
    assert "valid keys here:" in e.reason and "session_usd" in e.reason


def test_null_and_odd_values_from_a_table_are_refused():
    for raw in ({"network": {"port": None}}, {"network": None}, {"caps": {"session_usd": math.nan}},
                {"egress": {"allowed_hosts": [None]}}, {1: {}}, {"paths": {"data_dir": None}}):
        with pytest.raises(ConfigError):
            parse_config(raw)  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        parse_config([])  # type: ignore[arg-type]


@pytest.mark.parametrize("port, ok", [(PORT_MIN - 1, False), (PORT_MIN, True), (PORT_MAX, True), (PORT_MAX + 1, False)])
def test_port_edges(port, ok):
    if ok:
        assert parse_config({"network": {"port": port}}).network.port == port
    else:
        with pytest.raises(ConfigError, match="port"):
            parse_config({"network": {"port": port}})


@pytest.mark.parametrize("value, ok", [(0, True), (SPEND_CAP_SANITY_MAX, True), (SPEND_CAP_SANITY_MAX + 0.01, False),
                                       (-0.01, False)])
def test_spend_cap_edges(value, ok):
    if ok:
        assert parse_config({"caps": {"daily_usd": value}}).caps.daily_usd == value
    else:
        with pytest.raises(ConfigError, match="daily_usd"):
            parse_config({"caps": {"daily_usd": value}})


# ---- files: size, encoding, syntax, absence --------------------------------------------------------------------


def test_config_size_is_checked_before_parsing(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(b"#" * (CONFIG_MAX_BYTES - 1) + b"\n")  # exactly CONFIG_MAX_BYTES
    assert isinstance(load_config(path), Config)
    path.write_bytes(b"#" * CONFIG_MAX_BYTES + b"\n")
    with pytest.raises(ConfigError, match="larger than"):
        load_config(path)


def test_config_not_utf8_not_toml_or_missing_is_a_config_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(b'[network]\nhost = "\xff"\n')
    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(path)
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(write(tmp_path, "[network\nport = 1"))
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml")
    with pytest.raises(ConfigError, match="cannot be read"):
        load_config(tmp_path)  # a folder


def test_a_config_with_a_byte_order_mark_loads(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xef\xbb\xbf[network]\nport = 8765\n")
    assert load_config(path).network.port == 8765


# ---- path confinement (10-F09, S7) ------------------------------------------------------------------------------


@pytest.mark.parametrize("value, why", [
    ("C:\\Windows\\Temp\\elsewhere", "absolute"), ("\\elsewhere", "absolute"), ("C:elsewhere", "absolute"),
    ("..\\elsewhere", "'..'"), ("data/../../elsewhere", "'..'"), ("data\\..\\..\\x", "'..'"), (".", "inside"),
    ("a" * (CONFIG_PATH_MAX_CHARS + 1), "characters"), ("da\x00ta", "control"),
])
def test_data_dir_must_stay_inside_the_project(value, why):
    with pytest.raises(ConfigError) as e:
        parse_config({"paths": {"data_dir": value}})
    assert e.value.key == "[paths] data_dir" and why in e.value.reason, str(e.value)


def test_relative_paths_resolve_under_the_config_folder(tmp_path):
    cfg = load_config(write(tmp_path, "[paths]\ndata_dir = 'my data'\n[body.sim]\nimage_dir = 'media/img'\n"))
    assert cfg.paths.data_dir == (tmp_path / "my data").resolve() and cfg.base == tmp_path.resolve()
    assert cfg.body.sim.image_dir == (tmp_path / "media" / "img").resolve()
    assert parse_config({"paths": {"data_dir": "a" * CONFIG_PATH_MAX_CHARS}}, base=tmp_path).paths.data_dir.parent \
        == tmp_path.resolve()


def test_secrets_dir_absolute_only_inside_the_user_profile(tmp_path):
    home_dir = Path.home() / "AppData" / "Local" / "EmbodiedAgent-test-secrets"
    assert parse_config({"paths": {"secrets_dir": str(home_dir)}}).paths.secrets_dir == home_dir.resolve()
    assert parse_config({"paths": {"secrets_dir": "%LOCALAPPDATA%\\EmbodiedAgent\\secrets"}}).paths.secrets_dir \
        == (Path(os.environ["LOCALAPPDATA"]) / "EmbodiedAgent" / "secrets").resolve()
    for bad, why in (("C:\\Windows\\Temp\\secrets", "user profile"), ("%NO_SUCH_VARIABLE_EA%\\s", "not set"),
                     ("..\\secrets", "'..'")):
        with pytest.raises(ConfigError) as e:
            parse_config({"paths": {"secrets_dir": bad}})
        assert why in e.value.reason, str(e.value)
    assert parse_config({"paths": {"secrets_dir": "s"}}, base=tmp_path).paths.secrets_dir == (tmp_path / "s").resolve()


# ---- .env --------------------------------------------------------------------------------------------------------


def env(tmp_path, text: str | bytes) -> Path:
    path = tmp_path / ".env"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def test_env_lines_are_checked_and_values_never_shown(tmp_path):
    secret = "sk-ant-THIS-IS-SECRET"
    assert parse_env_file(env(tmp_path, f"# c\n\nANTHROPIC_API_KEY = '{secret}'\nX=\n")) == {
        "ANTHROPIC_API_KEY": secret, "X": ""}
    for text, where in ((f"{secret}\n", "line 1"), (f"BAD-NAME={secret}\n", "line 1"),
                        (f"# ok\nANTHROPIC_API_KEY={secret}\x00\n", "line 2 (ANTHROPIC_API_KEY)"),
                        (f"=novalue{secret}\n", "line 1"), ("A" * (ENV_KEY_MAX_CHARS + 1) + "=1\n", "line 1"),
                        ("K=" + "v" * (ENV_VALUE_MAX_CHARS + 1) + "\n", "line 1 (K)")):
        with pytest.raises(ConfigError) as e:
            parse_env_file(env(tmp_path, text))
        assert e.value.key == where and secret not in str(e.value), str(e.value)
    assert parse_env_file(env(tmp_path, "K=" + "v" * ENV_VALUE_MAX_CHARS + "\n"))["K"] == "v" * ENV_VALUE_MAX_CHARS


def test_env_size_and_encoding_are_checked(tmp_path):
    assert parse_env_file(env(tmp_path, b"#" * (ENV_FILE_MAX_BYTES - 1) + b"\n")) == {}
    with pytest.raises(ConfigError, match="larger than"):
        parse_env_file(env(tmp_path, b"#" * ENV_FILE_MAX_BYTES + b"\n"))
    with pytest.raises(ConfigError, match="UTF-8"):
        parse_env_file(env(tmp_path, b"K=\xff\n"))
    assert key_presence(env(tmp_path, b"\xef\xbb\xbfANTHROPIC_API_KEY=x\n"), environ={})["ANTHROPIC_API_KEY"]


# ---- the brain stops cleanly: exit code 2, one message, no traceback ---------------------------------------------


@pytest.mark.parametrize("command", [["info"], ["serve"], ["probe"], ["pair"], ["body-check"], ["chaos", "--list"], []])
def test_every_command_stops_with_exit_2_on_a_config_error(monkeypatch, capsys, command):
    def broken(*a, **k):
        return parse_config({"network": {"port": 65536}}, source="config.toml")
    monkeypatch.setattr(cli, "load_config", broken)
    assert cli.main(command) == 2
    err = capsys.readouterr().err
    assert err.startswith("config error: config.toml: [network] port:") and "Traceback" not in err


def test_a_bad_env_file_stops_info_with_exit_2(monkeypatch, capsys, tmp_path):
    bad = env(tmp_path, "garbage line\n")
    monkeypatch.setattr(cli, "load_config", lambda: parse_config({}))  # deny-all egress: the suite's own guard policy
    monkeypatch.setattr(cli, "key_presence", lambda: key_presence(bad, environ={}))
    assert cli.main(["info"]) == 2
    assert "line 1" in capsys.readouterr().err


def test_python_dash_m_brain_with_a_broken_config_gives_exit_2_and_no_traceback(tmp_path):
    """End to end in a child process: the real entry point, a broken config.toml next to a copy of the code."""
    code = ("import sys, brain.config as c, brain.__main__ as m\n"
            f"m.load_config = lambda: c.load_config(r'{write(tmp_path, '[caps]' + chr(10) + 'sesion_usd = 1.0' + chr(10))}')\n"
            "sys.exit(m.main(['info']))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT, timeout=120)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "did you mean 'session_usd'?" in out.stderr and "Traceback" not in out.stderr


def test_as_config_passes_a_config_through_and_gates_a_table():
    cfg = parse_config({})
    assert as_config(cfg) is cfg
    with pytest.raises(ConfigError):
        as_config({"paths": {"data_dir": "C:\\elsewhere"}})
    assert cli.data_dir({"paths": {"data_dir": "data"}}) == (REPO_ROOT / "data").resolve()
    token, problem = cli.load_token({"paths": {"data_dir": "..\\x"}})
    assert token is None and "data_dir" in problem  # load_token never raises
