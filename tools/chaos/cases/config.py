"""P0-04 configuration, .env and the token store: fail closed (B10, B11; invariant 7).

A missing, corrupt or wrong-typed file stops the feature with a named error or a refusal; nothing
falls back to a weaker mode; no secret appears in the refusal. Everything happens in the sandbox: a
fresh folder per case, and fresh private token stores (made with the token store's own code, which on
Windows runs icacls: the one process this experiment may start).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from brain import __main__ as cli
from brain.config import Config, ConfigError, as_config, key_presence, load_config, parse_env_file
from brain.egress import EgressPolicy
from brain.limits import CONFIG_MAX_BYTES, ENV_FILE_MAX_BYTES
from brain.transport import auth

from ..api import Case, Ctx, Gen, experiment, failure, hostile, invalid, valid


def gen_valid(g: Gen) -> list[Case]:
    return [valid("sandbox_config_loads", {"op": "load_config"}),
            valid("sandbox_env_names_only", {"op": "key_presence"}),
            valid("sandbox_token_loads", {"op": "load_token"}),
            valid("egress_from_sandbox_config", {"op": "egress_from_config"})]


def gen_invalid(g: Gen) -> list[Case]:
    return [
        invalid("config_not_toml", {"op": "load_config_text", "text": "[network\nport = 8765"}, "N4"),
        invalid("config_not_utf8", {"op": "load_config_bytes", "data": b"[x]\nname = \"\xff\xfe\"\n"}, "N5"),
        invalid("config_empty", {"op": "egress_from_text", "text": ""}, "N3"),
        invalid("egress_hosts_string", {"op": "egress_from_text", "text": '[egress]\nallowed_hosts = "api.example.org"\n'},
                "N1"),
        invalid("egress_hosts_wildcard", {"op": "egress_from_text", "text": '[egress]\nallowed_hosts = ["*.example.org"]\n'},
                "N5"),
        invalid("egress_hosts_number", {"op": "egress_from_text", "text": "[egress]\nallowed_hosts = [443]\n"}, "N1"),
        invalid("env_garbage_lines", {"op": "env_text", "text": "no equals sign\n=novalue\n\x00\nANTHROPIC_API_KEY\n"},
                "N5"),
        invalid("env_value_nul", {"op": "env_text", "text": "ANTHROPIC_API_KEY=sk-a\x00b\n"}, "N5"),
        invalid("env_oversize", {"op": "env_text", "text": "# x\n" * (ENV_FILE_MAX_BYTES // 4 + 1)}, "N2"),
        invalid("unknown_key_typo", {"op": "load_config_text", "text": "[caps]\nsesion_usd = 1.0\n",
                                     "expect": "did you mean 'session_usd'"}, "N4"),
        invalid("unknown_section", {"op": "load_config_text", "text": "[netwrk]\nport = 8765\n",
                                    "expect": "did you mean 'network'"}, "N4"),
        invalid("config_oversize", {"op": "load_config_text", "text": "# x\n" * (CONFIG_MAX_BYTES // 4 + 1)}, "N2"),
        invalid("port_wrong_type", {"op": "load_config_text", "text": '[network]\nport = "8765"\n'}, "N1"),
        invalid("cap_negative", {"op": "load_config_text", "text": "[caps]\ndaily_usd = -1.0\n"}, "N6"),
        invalid("cap_nan", {"op": "load_config_text", "text": "[caps]\nsession_usd = nan\n"}, "N6"),
        invalid("egress_entry_table", {"op": "load_config_text", "text": "[egress]\nallowed_hosts = [{a = 1}]\n"}, "N1"),
    ]


def gen_hostile(g: Gen) -> list[Case]:
    return [
        hostile("data_dir_absolute_outside", {"op": "data_dir", "value": "C:\\Windows\\Temp\\ea-elsewhere"}, "N7"),
        hostile("data_dir_traversal", {"op": "data_dir", "value": "..\\..\\elsewhere"}, "N7"),
        hostile("data_dir_drive_relative", {"op": "data_dir", "value": "C:elsewhere"}, "N7"),
        hostile("secrets_dir_outside_profile", {"op": "secrets_dir", "value": "C:\\Windows\\Temp\\ea-secrets"}, "N7"),
        hostile("sim_dir_traversal", {"op": "sim_dir", "value": "data/../../elsewhere"}, "N7"),
    ]


def gen_failure(g: Gen) -> list[Case]:
    corrupt = [("token_empty", b""), ("token_short", b"abcd\n"), ("token_not_hex", b"z" * 64 + b"\n"),
               ("token_binary", bytes(range(256))), ("token_too_long", b"a" * 200)]
    out = [failure(n, {"op": "corrupt_token", "data": data}, "N13") for n, data in corrupt]
    out += [failure("config_missing", {"op": "load_config_missing"}, "N15"),
            failure("secrets_dir_not_private", {"op": "token_unprivate"}, "N15"),
            failure("secrets_dir_is_a_file", {"op": "token_store_is_file"}, "N15")]
    return out


def _fresh(ctx: Ctx, name: str) -> Path:
    path: Path = ctx.sandbox.fresh_dir(name)
    return path


def _config_for(ctx: Ctx, secrets_dir: Path) -> Config:
    cfg = dict(ctx.sandbox.raw_config)
    cfg["paths"] = {"data_dir": "data", "secrets_dir": str(secrets_dir)}
    typed: Config = ctx.sandbox.typed(cfg)
    return typed


def _refused(ctx: Ctx, token: bytes | None, problem: str) -> str:
    ctx.expect(token is None, "fail_closed", "a broken token store still produced a token")
    ctx.expect(bool(problem), "fail_closed", "the refusal does not say what is wrong")
    return problem


@experiment(
    id="P0-04.config.fail_closed", card="P0-04",
    entries=("config_key.*", "store.*", "env_var.*_API_KEY", "file_site.brain/config.py:*",
             "file_site.brain/transport/auth.py:*", "gate.brain.config.load_config", "gate.brain.config.parse_config",
             "gate.brain.config.parse_env_file", "gate.brain.transport.auth.load_or_create_token",
             "gate.brain.transport.auth.parse_token", "cli_command.brain.info", "cli_command.brain.pair"),
    hypothesis="A missing, corrupt, oversized, unprivate, misspelt or wrong-typed config, .env or token store is "
               "refused with a named error (ConfigError names the file, the key and the reason) or a refusal "
               "message that holds no secret; nothing falls back to a weaker mode, and the data folder cannot be "
               "moved outside the repository.",
    blast_radius="sandbox", valid=gen_valid, invalid=gen_invalid, hostile=gen_hostile, failure=gen_failure,
    allow_processes=("icacls.exe",))
def check(case: Case, ctx: Ctx) -> Any:
    d = case.data
    op = d["op"]
    sb = ctx.sandbox
    if op == "load_config":
        cfg = load_config(sb.config_path)
        ctx.expect(cfg.egress.allowed_hosts == (), "fail_closed", "sandbox config has egress hosts")
        ctx.expect(cfg.paths.data_dir == sb.data_dir.resolve(), "validate_before_use", "data_dir not under the root")
        return f"{cfg.network.host}:{cfg.network.port}"
    if op == "key_presence":
        presence = key_presence(env_path=sb.env_path, environ={})
        ctx.expect(all(presence.values()), "validate_before_use", "a canary key was not seen")
        return presence  # names and booleans only; the secret scan proves no value is in it
    if op == "load_token":
        token, problem = cli.load_token(sb.config)
        ctx.expect(token == sb.token, "fail_closed", f"the sandbox token was not loaded: {problem}")
        return "token loaded"
    if op == "egress_from_config":
        return sorted(EgressPolicy.from_config(load_config(sb.config_path)).allowed)
    if op in ("load_config_text", "load_config_bytes"):
        path = _fresh(ctx, "cfg") / "config.toml"
        path.write_bytes(d["data"] if "data" in d else d["text"].encode("utf-8"))
        try:
            load_config(path)
        except ConfigError as e:
            ctx.expect(str(path) in str(e), "fail_closed", f"the refusal does not name the file: {str(e)[:120]}")
            if d.get("expect"):
                ctx.expect(d["expect"] in str(e), "fail_closed", f"the refusal lacks {d['expect']!r}: {str(e)[:160]}")
            return "refused"
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValueError) as e:
            ctx.expect(False, "fail_closed", f"refused with {type(e).__name__}, not a ConfigError")
            return "refused"
        ctx.expect(False, "fail_closed", "a corrupt config.toml loaded")
    if op == "egress_from_text":
        try:
            policy = EgressPolicy.from_config(tomllib.loads(d["text"]))
        except ValueError:
            return "refused"
        ctx.expect(policy.allowed == frozenset(), "fail_closed", f"a malformed [egress] allowed {policy.allowed}")
        return "deny-all"
    if op == "env_text":
        path = _fresh(ctx, "env") / ".env"
        path.write_text(d["text"], encoding="utf-8")
        try:
            values = parse_env_file(path)
        except ConfigError as e:
            ctx.expect("sk-" not in str(e), "no_secret_in_output", "a .env refusal shows a value")
            return "refused"
        ctx.expect(not values.get("ANTHROPIC_API_KEY"), "fail_closed", "a key without a value counts as present")
        return sorted(values)
    if op in ("data_dir", "secrets_dir", "sim_dir"):
        raw = ({"body": {"sim": {"image_dir": d["value"]}}} if op == "sim_dir" else {"paths": {op: d["value"]}})
        try:
            cfg = as_config(raw)  # what the brain does with config.toml: base = the repo root
        except ConfigError:
            return "refused"
        where = (cfg.paths.data_dir if op == "data_dir" else cfg.paths.secrets_dir if op == "secrets_dir"
                 else cfg.body.sim.image_dir)
        ctx.expect(False, "fail_closed", f"{op} {d['value']!r} was accepted and puts the folder at {where}")
        return str(where)
    if op == "load_config_missing":
        try:
            load_config(_fresh(ctx, "nocfg") / "config.toml")
        except ConfigError:
            return "refused"
        ctx.expect(False, "fail_closed", "a missing config.toml did not stop the load")
    if op == "corrupt_token":
        store = _fresh(ctx, "store") / "secrets"
        auth.ensure_store(store)
        (store / auth.TOKEN_FILE_NAME).write_bytes(d["data"])
        token, problem = cli.load_token(_config_for(ctx, store))
        return _refused(ctx, token, problem)
    if op == "token_unprivate":
        store = _fresh(ctx, "open")  # inherits the temp folder's access list: not private to this user
        (store / auth.TOKEN_FILE_NAME).write_text(sb.token_hex, encoding="ascii")
        token, problem = cli.load_token(_config_for(ctx, store))
        return _refused(ctx, token, problem)
    if op == "token_store_is_file":
        store = _fresh(ctx, "file") / "secrets"
        store.write_text("not a folder", encoding="ascii")
        token, problem = cli.load_token(_config_for(ctx, store))
        return _refused(ctx, token, problem)
    raise ValueError(f"unknown op {op!r}")
