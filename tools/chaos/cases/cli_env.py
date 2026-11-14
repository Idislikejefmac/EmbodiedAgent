"""The chaos command's own flags (B11), the environment variables the token store reads (B11) and the brain's
log file setup (B10).

Steady state: a bad flag ends in a usage message and exit code 2 (never a traceback); a hostile environment
variable makes the token store refuse with a message (never a non-private store, never an unhandled exception);
setup_logging puts its file inside the data folder or raises OSError, and leaves no handler behind.
Running every brain command in a child process is tests/test_cli_end_to_end.py (a harness case may not start processes).
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
from pathlib import Path
from typing import Any

from brain import __main__ as cli
from brain.transport import auth
from tools.chaos import runner

from .. import fuzz
from ..api import Case, Ctx, Gen, Violation, experiment, failure, hostile, invalid, valid

HOSTILE_ENV = ("", " ", "x" * 5000, "a b", "..\\..\\..", "a\nb", "Everyone:(OI)(CI)F", "C:\\no\\such", "\\\\server\\share", "ünï ✓", "-x",
               "a" * 300, "%USERNAME%", "$(whoami)")
SYSTEMROOT_DEFECT = ("X01B-08: brain/transport/auth.py _icacls() builds the program it starts from the SystemRoot environment variable "
                     "unchecked, so a changed SystemRoot makes the token store start a different executable (the harness refuses it)")
CHAOS_FLAGS = ("--allow-real-data", "--allow-real-phone", "--check-manifest", "--full", "--list", "--manifest-stub", "--no-results",
               "--only", "--plant", "--quick", "--results-dir", "--scan-logs", "--seed", "--strict-coverage")


def gen_valid(g: Gen) -> list[Case]:
    out = [valid("parse_quick", {"op": "parse", "argv": ["--quick"], "ok": True}),
           valid("parse_full", {"op": "parse", "argv": ["--full"], "ok": True}),
           valid("parse_no_args", {"op": "parse", "argv": [], "ok": True}),
           valid("parse_seed_zero", {"op": "parse", "argv": ["--seed", "0"], "ok": True}),
           valid("parse_seed_max", {"op": "parse", "argv": ["--seed", str(2**63 - 1)], "ok": True}),
           valid("parse_seed_repeated", {"op": "parse", "argv": ["--seed", "1", "--seed", "2"], "ok": True}),
           valid("parse_only_repeated", {"op": "parse", "argv": ["--only", "a", "--only", "b/c/1"], "ok": True}),
           valid("parse_flags_without_values", {"op": "parse", "argv": ["--no-results", "--strict-coverage", "--allow-real-data",
                                                                          "--allow-real-phone", "--list"], "ok": True}),
           valid("parse_plant", {"op": "parse", "argv": ["--plant", "recursion"], "ok": True}),
           valid("parse_results_dir", {"op": "parse", "argv": ["--results-dir", "some/dir"], "ok": True}),
           valid("parse_scan_logs_no_value", {"op": "parse", "argv": ["--scan-logs"], "ok": True}),
           valid("main_check_manifest", {"op": "main", "argv": ["--check-manifest"], "codes": (0, 1)}),
           valid("main_manifest_stub", {"op": "main", "argv": ["--manifest-stub"], "codes": (0,)}),
           valid("main_scan_logs_empty_dir", {"op": "scan", "files": {}, "codes": (0,)}),
           valid("setup_logging_plain", {"op": "logging", "mode": "plain"}),
           valid("setup_logging_verbose", {"op": "logging", "mode": "plain", "verbose": True}),
           valid("secrets_dir_default_is_a_path", {"op": "env", "var": "LOCALAPPDATA", "value": None})]
    return out


def gen_invalid(g: Gen) -> list[Case]:
    out = []
    for name, argv in (("seed_negative", ["--seed", "-1"]), ("seed_over_max", ["--seed", str(2**63)]), ("seed_text", ["--seed", "x"]),
                       ("seed_empty", ["--seed", ""]), ("seed_float", ["--seed", "1.5"]), ("seed_huge_digits", ["--seed", "9" * 5000]),
                       ("seed_missing_value", ["--seed"]), ("seed_underscore", ["--seed", "1_0"]), ("seed_fullwidth", ["--seed", "１２"]),
                       ("quick_and_full", ["--quick", "--full"]), ("unknown_flag", ["--definitely-not-a-flag"]), ("only_missing", ["--only"]),
                       ("plant_missing", ["--plant"]), ("results_dir_missing", ["--results-dir"]), ("bare_word", ["quick"]),
                       ("flag_with_value_it_does_not_take", ["--full=1"]), ("single_dash", ["-"]), ("double_dash_only", ["--", "--full"])):
        out.append(invalid(name, {"op": "parse", "argv": argv, "ok": False}, "N6" if "seed" in name else "N3" if "missing" in name else "N4",
                       known_defect=("X01B-07: the chaos --seed flag accepts '1_0' and fullwidth digits (int() is lenient), so the "
                                     "same seed can be written two ways") if name in ("seed_underscore", "seed_fullwidth") else None))
    return out


def gen_hostile(g: Gen) -> list[Case]:
    out = []
    for i, t in enumerate(fuzz.HOSTILE_TEXT[:16] + ("\x00", "a\x00b", "\ud800", "-" * 4000)):
        out.append(hostile(f"only_{i}", {"op": "parse", "argv": ["--only", t], "ok": None}, "N7"))
    for i, t in enumerate(("--full", "--seed", "-1", "--plant")):
        out.append(hostile(f"flag_as_value_{i}", {"op": "parse", "argv": ["--only", t], "ok": None}, "N7"))
    out.append(hostile("scan_logs_nul_dir", {"op": "scan_arg", "value": "a\x00b"}, "N7"))
    out.append(hostile("scan_logs_forged_lines", {"op": "scan", "files": {"x.log": "INFO ok\nERROR forged\nTraceback (most recent call last):\n"}, "codes": (0, 1)}, "N7"))
    for i, v in enumerate(HOSTILE_ENV):
        out.append(hostile(f"localappdata_{i}", {"op": "env", "var": "LOCALAPPDATA", "value": v}, "N7"))
    for i, n in enumerate(("a/b", "..\\x", "x" * 300, "CON", "a\nb", "")):
        out.append(hostile(f"log_name_{i}", {"op": "logging", "mode": "plain", "name": n}, "N7"))
    return out


def gen_failure(g: Gen) -> list[Case]:
    out = [failure("scan_logs_dir_missing", {"op": "scan_arg", "value": "no-such-dir"}, "N15"),
           failure("scan_logs_binary_file", {"op": "scan", "files": {"x.log": "\x00\xff" * 500}, "codes": (0, 1)}, "N13"),
           failure("scan_logs_huge_line", {"op": "scan", "files": {"x.log": "x" * 2_000_000}, "codes": (0, 1)}, "N11"),
           failure("setup_logging_data_dir_is_file", {"op": "logging", "mode": "data_is_file"}, "N15"),
           failure("setup_logging_log_dir_is_file", {"op": "logging", "mode": "logs_is_file"}, "N15")]
    for var in ("USERNAME", "USERDOMAIN", "SystemRoot"):
        for i, v in enumerate(HOSTILE_ENV[:9]):
            if g.mode == "quick" and i not in (0, 1, 3, 5, 6):
                continue  # each one starts icacls (about 0.1 s); full mode runs all nine
            out.append(failure(f"{var.lower()}_{i}", {"op": "env", "var": var, "value": v}, "N15",
                               known_defect=SYSTEMROOT_DEFECT if var == "SystemRoot" and i in (1, 2, 3, 5) else None))
    return out


def _parse(ctx: Ctx, d: dict[str, Any]) -> str:
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            runner.build_parser().parse_args(d["argv"])
        outcome = "accepted"
    except SystemExit as e:
        text = err.getvalue()
        ctx.expect(e.code == 2 and "usage" in text and "Traceback" not in text, "fail_closed", f"refused without usage and exit 2 ({e.code})")
        outcome = "refused"
    if d["ok"] is True:
        ctx.expect(outcome == "accepted", "validate_before_use", f"valid argv {d['argv']} refused")
    elif d["ok"] is False:
        ctx.expect(outcome == "refused", "validate_before_use", f"bad argv {str(d['argv'])[:60]} accepted")
    return outcome


def _run_main(ctx: Ctx, argv: list[str], codes: tuple[int, ...]) -> int:
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = runner.main(argv)
    except SystemExit as e:
        code = int(e.code) if isinstance(e.code, int) else 2
    ctx.expect(code in codes or code == 2, "fail_closed", f"exit code {code}")
    ctx.expect("Traceback" not in err.getvalue() + out.getvalue(), "no_crash", "a traceback was printed")
    return code


def _logging(ctx: Ctx, d: dict[str, Any]) -> str:
    cfg = ctx.sandbox.config
    mode = d["mode"]
    if mode in ("data_is_file", "logs_is_file"):
        root = ctx.sandbox.fresh_dir("log")
        (root / "f").write_text("x", encoding="utf-8")
        data = root / "f" if mode == "data_is_file" else root
        if mode == "logs_is_file":
            (root / "logs").write_text("x", encoding="utf-8")
        cfg = ctx.sandbox.typed({**ctx.sandbox.raw_config, "paths": {"data_dir": str(data.relative_to(ctx.sandbox.root)),
                                                                     "secrets_dir": str(ctx.sandbox.secrets_dir)}})
    before = list(logging.getLogger().handlers)
    level = logging.getLogger().level
    try:
        path = cli.setup_logging(cfg, d.get("name", "chaos"), d.get("verbose", False))
        ctx.expect(Path(path).resolve().is_relative_to(ctx.sandbox.root.resolve()), "no_forbidden_side_effect", "the log file is outside the sandbox")
        return "ok"
    except OSError as e:
        ctx.expect(mode != "plain" or d.get("name") is not None, "validate_before_use", f"a good data folder was refused: {e}")
        return "OSError"
    finally:
        for h in list(logging.getLogger().handlers):
            if h not in before:
                logging.getLogger().removeHandler(h)
                h.close()
        logging.getLogger().setLevel(level)


def _env(ctx: Ctx, d: dict[str, Any]) -> str:
    var, value = d["var"], d["value"]
    old = os.environ.get(var)
    if value is not None:
        os.environ[var] = value
    try:
        if var == "LOCALAPPDATA":
            where = auth.default_secrets_dir()
            ctx.expect(isinstance(where, Path), "validate_before_use", "no secrets folder path")
            if case_is_hostile(value):
                return str(where)[:60]  # the value only ever names the folder; the config gate confines paths separately
            return "default"
        store = ctx.sandbox.fresh_dir("env") / "secrets"
        cfg = ctx.sandbox.typed({**ctx.sandbox.raw_config, "paths": {"data_dir": "data", "secrets_dir": str(store)}})
        token, problem = cli.load_token(cfg)
        if token is not None:
            ctx.expect(not auth.acl_problems(store), "fail_closed", f"{var}={str(value)[:20]!r}: a token store that is not private was used")
            return "private store"
        ctx.expect(bool(problem) and "Traceback" not in problem, "fail_closed", "a refusal without a message")
        return "refused"
    finally:
        if old is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = old


def case_is_hostile(value: str | None) -> bool:
    return value is not None


@experiment(
    id="X-01.cli_env.flags_and_environment", card="X-01",
    entries=("cli_arg.chaos.*", "env_var.LOCALAPPDATA", "env_var.SystemRoot", "env_var.USERDOMAIN", "env_var.USERNAME",
             "file_site.brain/__main__.py:setup_logging"),
    hypothesis="A bad chaos flag ends in a usage message and exit code 2; a hostile LOCALAPPDATA, SystemRoot, USERNAME or "
               "USERDOMAIN makes the token store refuse with a message or use a private store, never an unhandled "
               "exception or a store that is not private; setup_logging writes inside the data folder or raises OSError "
               "and leaves no handler behind.",
    blast_radius="sandbox", valid=gen_valid, invalid=gen_invalid, hostile=gen_hostile, failure=gen_failure,
    allow_processes=("icacls.exe",), abort_after_failures=10)
def check(case: Case, ctx: Ctx) -> Any:
    d = case.data
    op = d["op"]
    if op == "parse":
        return _parse(ctx, d)
    if op == "main":
        return _run_main(ctx, d["argv"], d["codes"])
    if op == "scan_arg":
        value = d["value"]
        if "\x00" not in value:
            value = str(ctx.sandbox.fresh_dir("scan") / value)
        return _run_main(ctx, ["--scan-logs", value, "--no-results"], (0, 1, 2))
    if op == "scan":
        folder = ctx.sandbox.fresh_dir("logs")
        for name, text in d["files"].items():
            (folder / name).write_text(text, encoding="utf-8", errors="replace", newline="")
        return _run_main(ctx, ["--scan-logs", str(folder)], d["codes"])
    if op == "logging":
        return _logging(ctx, d)
    if op == "env":
        return _env(ctx, d)
    raise Violation("no_crash", f"unknown op {op!r}")
