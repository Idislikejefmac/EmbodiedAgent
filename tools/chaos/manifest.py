"""The entry-point manifest and its drift check (plan card X-01 step 1; check E8 in docs/testing/00).

`tools/chaos/manifest.toml` lists everything the system exposes. This module DISCOVERS the same set
from the real code (the tool catalogue, the protocol schema and constants, the Body base class, the
argparse parsers, an AST scan for config keys, environment variables, file access sites and network
or process imports, the Android manifests and Kotlin action constants) and compares the two. The run
fails when the code has an entry point the manifest lacks, when the manifest lists one the code no
longer has, or when a security-relevant attribute differs (a tool's channels or class, a message's
senders, a component's exported flag). It also checks the `[[gate]]` table (doc 00 section 4.4): every
gate must import, and every limit it names must be in `[limits]` with a home that resolves.

Why TOML and not a Python registry: the manifest is data that a reviewer reads in a diff; it cannot run
code; `tomllib` is in the standard library. Discovery is code; the record of what was reviewed is not.

Command line (through the runner):
    python -m brain chaos --check-manifest          drift check only; exit 1 on drift
    python -m brain chaos --manifest-stub           print TOML lines for entries the manifest lacks
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import re
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from brain.config import REPO_ROOT

MANIFEST_PATH = Path(__file__).with_name("manifest.toml")
MANIFEST_MAX_BYTES = 1024 * 1024
SCHEMA_VERSION = 1

KINDS = (
    "tool", "reserved_tool", "tool_param",
    "message", "field", "command", "command_arg", "error_code", "close_code", "binary_type", "ws_option",
    "body_method", "body_impl", "body_impl_method", "capability", "body_kind",
    "cli_command", "cli_arg", "config_key", "env_var", "store", "file_site", "net_module", "egress_host",
    "android_component", "android_action",
)
# Attributes the drift check compares (security-relevant); other keys are reviewer annotations.
CHECKED_ATTRS: dict[str, tuple[str, ...]] = {
    "tool": ("tool_class", "channels", "sensitive"),
    "reserved_tool": ("tool_class", "channels", "sensitive"),
    "message": ("senders",),
    "close_code": ("code",),
    "binary_type": ("code",),
    "android_component": ("exported", "permission"),
}
COMMON_KEYS = frozenset({"id", "kind", "card", "boundary", "note"})
_CARD = re.compile(r"^(P\d-\d\d|X-01|SYS|AND)$")
_BOUNDARY = re.compile(r"^B\d{1,2}$")
_LIMIT_HOME = re.compile(r"^([a-z_][a-z0-9_.]*:[A-Z][A-Z0-9_]*|spec:.+|literal:.+|provisional(:.*)?)$")

# Where discovery looks. Module attributes so a test can point them at a planted copy.
SCAN_ROOTS: list[Path] = [REPO_ROOT / "brain"]
SCAN_EXTRA_FILES: list[Path] = [REPO_ROOT / "tools" / "migrate.py"]
CONFIG_TOML = REPO_ROOT / "config.toml"
ANDROID_APP = REPO_ROOT / "body-android" / "app" / "src"
ANDROID_MANIFESTS: dict[str, Path] = {"main": ANDROID_APP / "main" / "AndroidManifest.xml",
                                      "debug": ANDROID_APP / "debug" / "AndroidManifest.xml"}
KOTLIN_ACTION_SOURCES: dict[str, Path] = {
    "LinkService": ANDROID_APP / "main" / "java" / "com" / "jmd" / "embodied" / "body" / "LinkService.kt",
    "debug": ANDROID_APP / "debug" / "java" / "com" / "jmd" / "embodied" / "body" / "DebugCommandReceiver.kt",
}
CLI_MAINS: dict[str, Callable[[], Callable[..., Any]]] = {}  # filled below: program -> loader of its main()

NETWORK_MODULES = frozenset({
    "socket", "_socket", "ssl", "socketserver", "http", "urllib", "urllib3", "requests", "httpx", "httpcore",
    "aiohttp", "websockets", "websocket", "anthropic", "openai", "deepgram", "elevenlabs", "grpc", "pycurl", "dns",
    "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc", "paramiko", "boto3", "google", "azure", "zmq",
    "webbrowser", "_overlapped", "_winapi", "winreg",
})
PROCESS_MODULES = frozenset({"subprocess", "multiprocessing", "ctypes", "_ctypes", "pty", "pexpect", "sh", "cffi",
                             "_posixsubprocess"})
FILE_CALLS = frozenset({"open", "read_text", "read_bytes", "write_text", "write_bytes", "mkdir", "makedirs", "unlink",
                        "rmdir", "rename", "touch", "iterdir", "glob", "rglob", "extractall",
                        "RotatingFileHandler", "FileHandler", "mkdtemp", "NamedTemporaryFile", "TemporaryDirectory",
                        "rmtree", "copyfile", "copytree", "listdir", "scandir"})
# Only as os.<name>: str.replace and list.remove are not file calls.
OS_FILE_CALLS = frozenset({"replace", "walk", "chmod", "remove"})


class ManifestError(ValueError):
    """The manifest file is unusable (exit code 2)."""


@dataclass
class Manifest:
    entries: dict[str, dict[str, Any]]
    gates: list[dict[str, Any]]
    limits: dict[str, str]
    path: Path = MANIFEST_PATH

    def ids(self, kind: str | None = None) -> set[str]:
        return {i for i, e in self.entries.items() if kind is None or e["kind"] == kind}

    def all_ids(self) -> set[str]:
        """Entry ids plus `gate.<name>` for each gate (experiments may name either)."""
        return set(self.entries) | {f"gate.{g['name']}" for g in self.gates}

    def card_of(self, entry_id: str) -> str | None:
        e = self.entries.get(entry_id)
        if e is not None:
            return str(e["card"])
        for g in self.gates:
            if f"gate.{g['name']}" == entry_id:
                return g.get("card")
        return None


# ---- loading (a gate: the manifest is an input to the harness) -------------------------------------------


def load_manifest(path: Path | None = None, text: str | None = None) -> Manifest:
    """Parse and check the manifest. Raises ManifestError listing every problem (never a partial result)."""
    p = path or MANIFEST_PATH
    if text is None:
        try:
            raw = p.read_bytes()
        except OSError as err:
            raise ManifestError(f"cannot read the manifest {p}: {err}") from None
        if len(raw) > MANIFEST_MAX_BYTES:
            raise ManifestError(f"manifest {p} is over {MANIFEST_MAX_BYTES} bytes")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ManifestError(f"manifest {p} is not UTF-8") from None
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise ManifestError(f"manifest {p} is not valid TOML: {err}") from None
    problems: list[str] = []
    if doc.get("schema") != SCHEMA_VERSION:
        problems.append(f"schema must be {SCHEMA_VERSION}")
    unknown_top = set(doc) - {"schema", "entry", "gate", "limits"}
    if unknown_top:
        problems.append(f"unknown top-level keys {sorted(unknown_top)}")
    entries: dict[str, dict[str, Any]] = {}
    raw_entries = doc.get("entry", [])
    if not isinstance(raw_entries, list):
        problems.append("entry must be an array of tables")
        raw_entries = []
    for i, e in enumerate(raw_entries):
        where = f"entry {i}"
        if not isinstance(e, dict):
            problems.append(f"{where} is not a table")
            continue
        missing = [k for k in ("id", "kind", "card", "boundary") if k not in e]
        if missing:
            problems.append(f"{where} ({e.get('id', '?')}) lacks {missing}")
            continue
        eid, kind = e["id"], e["kind"]
        if not isinstance(eid, str) or kind not in KINDS or not eid.startswith(f"{kind}."):
            problems.append(f"{where}: id {eid!r} must start with its kind, and kind {kind!r} must be one of KINDS")
            continue
        if not isinstance(e["card"], str) or not _CARD.match(e["card"]):
            problems.append(f"{eid}: card {e['card']!r} is not a card id")
        if not isinstance(e["boundary"], str) or not _BOUNDARY.match(e["boundary"]):
            problems.append(f"{eid}: boundary {e['boundary']!r} is not a B-id (docs/testing/00 section 3)")
        extra = set(e) - COMMON_KEYS - set(CHECKED_ATTRS.get(kind, ()))
        if extra:
            problems.append(f"{eid}: unknown keys {sorted(extra)}")
        absent = [a for a in CHECKED_ATTRS.get(kind, ()) if a not in e]
        if absent:
            problems.append(f"{eid}: lacks checked attribute(s) {absent}")
        if eid in entries:
            problems.append(f"{eid}: listed twice")
        entries[eid] = e
    gates: list[dict[str, Any]] = []
    raw_gates = doc.get("gate", [])
    seen_gates: set[str] = set()
    for i, g in enumerate(raw_gates if isinstance(raw_gates, list) else []):
        if not isinstance(g, dict) or not all(k in g for k in ("name", "boundary", "produces", "limits")):
            problems.append(f"gate {i} needs name, boundary, produces and limits")
            continue
        if set(g) - {"name", "boundary", "produces", "limits", "card", "note"}:
            problems.append(f"gate {g['name']}: unknown keys {sorted(set(g) - {'name', 'boundary', 'produces', 'limits', 'card', 'note'})}")
        if not isinstance(g["limits"], list) or not all(isinstance(x, str) for x in g["limits"]):
            problems.append(f"gate {g['name']}: limits must be a list of names")
        if not isinstance(g["boundary"], str) or not _BOUNDARY.match(g["boundary"]):
            problems.append(f"gate {g['name']}: boundary {g['boundary']!r} is not a B-id")
        if g["name"] in seen_gates:
            problems.append(f"gate {g['name']}: listed twice")
        seen_gates.add(g["name"])
        gates.append(g)
    limits = doc.get("limits", {})
    if not isinstance(limits, dict) or not all(isinstance(v, str) and _LIMIT_HOME.match(v) for v in limits.values()):
        problems.append("limits must map names to 'module:ATTR', 'spec:...', 'literal:...' or 'provisional'")
        limits = {}
    if problems:
        raise ManifestError("manifest rejected:\n  " + "\n  ".join(problems))
    return Manifest(entries, gates, dict(limits), p)


# ---- discovery -------------------------------------------------------------------------------------------

Discovered = dict[str, dict[str, Any]]  # id -> {"kind": ..., checked attrs ...}


def _add(out: Discovered, kind: str, name: str, **attrs: Any) -> None:
    out[f"{kind}.{name}"] = {"kind": kind, **attrs}


def discover_tools(out: Discovered) -> None:
    from brain.agent import tools as tools_mod

    cat = tools_mod.load_catalogue()
    for kind, specs in (("tool", cat.tools), ("reserved_tool", cat.reserved)):
        for name, spec in specs.items():
            _add(out, kind, name, tool_class=spec.tool_class, channels=sorted(c.value for c in spec.channels),
                 sensitive=spec.sensitive)
            for param in (spec.input_schema or {}).get("properties", {}):
                _add(out, "tool_param", f"{name}.{param}")


def _schema_fields(schema: dict[str, Any], node: Any, prefix: str, out: set[str], depth: int = 0) -> None:
    if depth > 6 or not isinstance(node, dict):
        return
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        _schema_fields(schema, schema["$defs"].get(ref.split("/")[-1], {}), prefix, out, depth + 1)
    for sub in node.get("allOf", []) if isinstance(node.get("allOf"), list) else []:
        if isinstance(sub, dict) and "if" not in sub:
            _schema_fields(schema, sub, prefix, out, depth + 1)
    for name, sub in (node.get("properties") or {}).items():
        path = f"{prefix}.{name}"
        out.add(path)
        _schema_fields(schema, sub, path, out, depth + 1)
    if isinstance(node.get("items"), dict):
        _schema_fields(schema, node["items"], prefix + "[]", out, depth + 1)


def discover_protocol(out: Discovered) -> None:
    from brain.transport import protocol as p

    s = p.schema()
    types = set(p.MESSAGE_TYPES) | set(s["properties"]["type"]["enum"])
    for t in sorted(types):
        _add(out, "message", t, senders=sorted(p.SENDERS.get(t, ())))
    for name in s["properties"]:
        _add(out, "field", f"envelope.{name}")
    for branch in s.get("allOf", []):
        t = branch.get("if", {}).get("properties", {}).get("type", {}).get("const")
        payload = branch.get("then", {}).get("properties", {}).get("payload")
        if t and payload:
            found: set[str] = set()
            _schema_fields(s, payload, t, found)
            for f in found:
                _add(out, "field", f)
    for c in set(p.COMMANDS) | set(s["$defs"]["command_name"]["enum"]):
        _add(out, "command", c)
    for branch in s["$defs"]["command"].get("allOf", []):
        c = branch.get("if", {}).get("properties", {}).get("name", {}).get("const")
        args = branch.get("then", {}).get("properties", {}).get("args", {}).get("properties", {})
        for a in args:
            _add(out, "command_arg", f"{c}.{a}")
    for code in set(p.ERROR_CODES) | set(s["$defs"]["error_code"]["enum"]):
        _add(out, "error_code", code)
    for name in dir(p):
        if name.startswith("CLOSE_") and isinstance(getattr(p, name), int):
            _add(out, "close_code", name[6:].lower(), code=getattr(p, name))
    for name, code in p.BINARY_TYPE_CODES.items():
        _add(out, "binary_type", name, code=code)


def _py_files() -> list[Path]:
    files = [f for root in SCAN_ROOTS for f in sorted(root.rglob("*.py"))]
    return files + [f for f in SCAN_EXTRA_FILES if f.exists()]


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def discover_ws_options(out: Discovered) -> None:
    for path in _py_files():
        rel = _rel(path)
        if "/transport/" not in rel:
            continue
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if name in ("serve", "ws_connect"):
                    for kw in node.keywords:
                        if kw.arg:
                            _add(out, "ws_option", f"{Path(rel).stem}.{kw.arg}")


def discover_body(out: Discovered) -> None:
    from brain import body as body_pkg
    from brain.body import base

    public = sorted(n for n in vars(base.Body) if not n.startswith("_"))
    for n in public:
        _add(out, "body_method", n)
    for c in base.Capability if hasattr(base, "Capability") else []:
        _add(out, "capability", c.value)
    for k in body_pkg.BODY_KINDS:
        _add(out, "body_kind", k)
    body_dir = Path(base.__file__).parent
    for path in sorted(body_dir.glob("*.py")):
        for node in _parse(path).body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in node.bases]
            if not any(b.endswith("Body") for b in bases):
                continue
            _add(out, "body_impl", node.name)
            for item in node.body:
                if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and not item.name.startswith("_")
                        and item.name not in public):
                    _add(out, "body_impl_method", f"{node.name}.{item.name}")


class _Captured(Exception):
    def __init__(self, parser: argparse.ArgumentParser):
        self.parser = parser


def capture_parser(main: Callable[..., Any], argv: list[str] | None = None) -> argparse.ArgumentParser:
    """Run a program's main() until it calls parse_args, and return the parser it built. Nothing after
    parse_args runs (no guard, no command), so this is safe on any CLI built with argparse."""
    real = argparse.ArgumentParser.parse_args

    def stop(self: argparse.ArgumentParser, *a: Any, **k: Any) -> Any:
        raise _Captured(self)

    argparse.ArgumentParser.parse_args = stop  # type: ignore[method-assign]
    try:
        main(argv if argv is not None else [])
    except _Captured as c:
        return c.parser
    finally:
        argparse.ArgumentParser.parse_args = real  # type: ignore[method-assign]
    raise RuntimeError(f"{main!r} returned without calling parse_args")


def _brain_main() -> Callable[..., Any]:
    from brain import __main__ as brain_main

    return brain_main.main


def _migrate_main() -> Callable[..., Any]:
    spec = importlib.util.spec_from_file_location("ea_migrate_for_manifest", REPO_ROOT / "tools" / "migrate.py")
    if spec is None or spec.loader is None:
        raise ImportError("tools/migrate.py cannot be loaded")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main  # type: ignore[no-any-return]


def _chaos_main() -> Callable[..., Any]:
    from . import runner

    return runner.main


CLI_MAINS.update({"brain": _brain_main, "migrate": _migrate_main, "chaos": _chaos_main})


def _arg_name(action: argparse.Action) -> str:
    return max(action.option_strings, key=len) if action.option_strings else action.dest


def discover_cli(out: Discovered) -> None:
    for program, loader in CLI_MAINS.items():
        parser = capture_parser(loader())
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    _add(out, "cli_command", f"{program}.{name}")
                    for a in sub._actions:
                        if not isinstance(a, argparse._HelpAction):
                            _add(out, "cli_arg", f"{program}.{name}.{_arg_name(a)}")
            elif not isinstance(action, argparse._HelpAction):
                _add(out, "cli_arg", f"{program}.{_arg_name(action)}")


# Config keys: an AST walk that follows `config.get("a", {}).get("b")`, `config["a"]`, simple aliases
# (`net = config.get("network", {})`) and loops over constant tuples (`for k in ("x", "y"): sim[k]`).
CONFIG_ROOT_NAMES = frozenset({"config", "cfg"})


def _str_consts(node: ast.AST) -> tuple[str, ...] | None:
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and node.elts and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        return tuple(e.value for e in node.elts)  # type: ignore[attr-defined]
    return None


def _scope_nodes(scope: ast.AST) -> list[ast.AST]:
    """Nodes of one function (or the module), without descending into nested functions or classes."""
    out: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        n = stack.pop()
        out.append(n)
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(n))
    return out


def config_paths_in(tree: ast.Module) -> set[tuple[str, ...]]:
    found: set[tuple[str, ...]] = set()
    scopes: list[ast.AST] = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for scope in scopes:
        nodes = _scope_nodes(scope)
        loops: dict[str, tuple[str, ...]] = {}
        for n in nodes:
            target = iter_ = None
            if isinstance(n, (ast.For, ast.comprehension)):
                target, iter_ = n.target, n.iter
            if isinstance(target, ast.Name) and iter_ is not None:
                consts = _str_consts(iter_)
                if consts:
                    loops[target.id] = consts
        env: dict[str, set[tuple[str, ...]]] = {}

        def keys(node: ast.AST) -> tuple[str, ...] | None:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return (node.value,)
            if isinstance(node, ast.Name) and node.id in loops:
                return loops[node.id]
            return None

        def resolve(node: ast.AST) -> set[tuple[str, ...]]:
            if isinstance(node, ast.Name):
                if node.id in CONFIG_ROOT_NAMES:
                    return {()}
                return env.get(node.id, set())
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                    and node.args):
                base, ks = resolve(node.func.value), keys(node.args[0])
                return {b + (k,) for b in base for k in ks} if base and ks else set()
            if isinstance(node, ast.Subscript):
                base, ks = resolve(node.value), keys(node.slice)
                return {b + (k,) for b in base for k in ks} if base and ks else set()
            return set()

        for _ in range(2):  # twice, so an alias defined after first use is still followed
            for n in nodes:
                if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                    paths = resolve(n.value)
                    if paths and n.targets[0].id not in CONFIG_ROOT_NAMES:
                        env[n.targets[0].id] = paths
        for n in nodes:
            for path in resolve(n):
                for i in range(1, len(path) + 1):
                    found.add(path[:i])
    return found


def _toml_paths(d: dict[str, Any], prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    out: set[tuple[str, ...]] = set()
    for k, v in d.items():
        out.add(prefix + (k,))
        if isinstance(v, dict):
            out |= _toml_paths(v, prefix + (k,))
    return out


def discover_config_keys(out: Discovered) -> None:
    paths: set[tuple[str, ...]] = set()
    for path in _py_files():
        paths |= config_paths_in(_parse(path))
    if CONFIG_TOML.exists():
        paths |= _toml_paths(tomllib.loads(CONFIG_TOML.read_text(encoding="utf-8")))
    for p in paths:
        _add(out, "config_key", ".".join(p))


def discover_env_vars(out: Discovered) -> None:
    from brain import config, egress

    names: set[str] = set(config.KNOWN_KEYS) | set(egress.PROXY_VARIABLES)
    for path in _py_files():
        for node in ast.walk(_parse(path)):
            target = None
            if isinstance(node, ast.Call) and node.args:
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr == "get" and isinstance(f.value, ast.Attribute) \
                        and f.value.attr == "environ":
                    target = node.args[0]
                elif isinstance(f, ast.Attribute) and f.attr == "getenv":
                    target = node.args[0]
            elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "environ":
                target = node.slice
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                names.add(target.value)
    for n in names:
        _add(out, "env_var", n)


def _store_id(name: str) -> str:
    return re.sub(r"[^a-z0-9_.-]", "_", name.lower()).strip("._") or "unnamed"


def discover_stores(out: Discovered) -> None:
    """Named stores the brain reads or writes, from the constants that locate them."""
    from brain import config
    from brain.agent import tools as tools_mod
    from brain.transport import auth, protocol

    for name in (config.DEFAULT_CONFIG.name, config.DEFAULT_ENV.name, tools_mod.CATALOGUE_PATH.name,
                 protocol.SCHEMA_PATH.name, auth.TOKEN_FILE_NAME):
        _add(out, "store", _store_id(name))
    mig = (REPO_ROOT / "tools" / "migrate.py")
    if mig.exists() and "MAGIC" in mig.read_text(encoding="utf-8"):
        _add(out, "store", "migration_archive")


def _qualname_index(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    out: list[tuple[str, ast.AST]] = [("<module>", tree)]

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{prefix}{child.name}"
                if not isinstance(child, ast.ClassDef):
                    out.append((q, child))
                visit(child, q + ".")

    visit(tree, "")
    return out


def _is_file_call(node: ast.Call) -> bool:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id in ("open", "RotatingFileHandler", "FileHandler")
    if isinstance(f, ast.Attribute):
        if isinstance(f.value, ast.Name) and f.value.id == "os" and f.attr in OS_FILE_CALLS | FILE_CALLS:
            return True
        return f.attr in FILE_CALLS
    return False


def discover_file_sites(out: Discovered) -> None:
    for path in _py_files():
        rel = _rel(path)
        for qual, scope in _qualname_index(_parse(path)):
            if any(isinstance(n, ast.Call) and _is_file_call(n) for n in _scope_nodes(scope)):
                _add(out, "file_site", f"{rel}:{qual}")


def discover_net_modules(out: Discovered) -> None:
    for path in _py_files():
        rel = _rel(path)
        mods: set[str] = set()
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
        for m in mods & (NETWORK_MODULES | PROCESS_MODULES):
            _add(out, "net_module", f"{rel}:{m}")


def discover_egress_hosts(out: Discovered) -> None:
    from brain.egress import EgressPolicy

    if CONFIG_TOML.exists():
        policy = EgressPolicy.from_config(tomllib.loads(CONFIG_TOML.read_text(encoding="utf-8")))
        for host, port in policy.allowed:
            _add(out, "egress_host", f"{host}:{port}")


_ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


def discover_android(out: Discovered) -> None:
    for variant, path in ANDROID_MANIFESTS.items():
        if not path.exists():
            continue
        root = ET.fromstring(path.read_text(encoding="utf-8-sig"))
        app = root.find("application")
        for comp in app if app is not None else []:
            if comp.tag not in ("activity", "service", "receiver", "provider", "activity-alias"):
                continue
            name = (comp.get(_ANDROID_NS + "name") or "?").lstrip(".")
            _add(out, "android_component", f"{variant}.{comp.tag}.{name}",
                 exported=comp.get(_ANDROID_NS + "exported", "unset"),
                 permission=comp.get(_ANDROID_NS + "permission", ""))
    patterns = {"LinkService": re.compile(r'const val ACTION_([A-Z_]+)\s*=\s*"'),
                "debug": re.compile(r'^\s*"([A-Z_]+)"\s*->', re.M)}
    for owner, path in KOTLIN_ACTION_SOURCES.items():
        if path.exists():
            for name in patterns[owner].findall(path.read_text(encoding="utf-8-sig")):
                _add(out, "android_action", f"{owner}.{name}")


DISCOVERERS: list[Callable[[Discovered], None]] = [
    discover_tools, discover_protocol, discover_ws_options, discover_body, discover_cli, discover_config_keys,
    discover_env_vars, discover_stores, discover_file_sites, discover_net_modules, discover_egress_hosts,
    discover_android,
]


def discover() -> Discovered:
    out: Discovered = {}
    for d in DISCOVERERS:
        d(out)
    return dict(sorted(out.items()))


# ---- the drift check -------------------------------------------------------------------------------------


@dataclass
class Drift:
    missing_in_manifest: list[str] = field(default_factory=list)  # in the code, not in the manifest
    missing_in_code: list[str] = field(default_factory=list)  # in the manifest, not in the code
    changed: list[str] = field(default_factory=list)  # attribute differences
    gate_problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing_in_manifest or self.missing_in_code or self.changed or self.gate_problems)

    def lines(self) -> list[str]:
        out = [f"code has an entry point the manifest lacks: {i}" for i in self.missing_in_manifest]
        out += [f"manifest lists an entry point the code no longer has: {i}" for i in self.missing_in_code]
        out += [f"attribute changed (review it, then update the manifest): {c}" for c in self.changed]
        out += [f"gate table: {g}" for g in self.gate_problems]
        return out


def _resolve_dotted(name: str) -> object:
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        try:
            obj: object = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        for attr in parts[i:]:
            obj = getattr(obj, attr)
        return obj
    raise ImportError(name)


def check_gates(m: Manifest) -> list[str]:
    problems: list[str] = []
    for g in m.gates:
        try:
            _resolve_dotted(g["name"])
        except (ImportError, AttributeError):
            problems.append(f"gate {g['name']} does not resolve to anything in the code")
        for lim in g["limits"]:
            if lim not in m.limits:
                problems.append(f"gate {g['name']}: limit {lim} is not in [limits]")
    for name, home in m.limits.items():
        if ":" in home and not home.startswith(("spec:", "literal:", "provisional")):
            module, attr = home.split(":", 1)
            try:
                value = getattr(importlib.import_module(module), attr)
            except (ImportError, AttributeError):
                problems.append(f"limit {name}: {home} does not resolve")
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float, tuple)):
                problems.append(f"limit {name}: {home} is not a number")
    return problems


def drift(manifest: Manifest | None = None, discovered: Discovered | None = None) -> Drift:
    m = manifest or load_manifest()
    d = discovered if discovered is not None else discover()
    out = Drift()
    out.missing_in_manifest = sorted(set(d) - set(m.entries))
    out.missing_in_code = sorted(set(m.entries) - set(d))
    for eid in sorted(set(d) & set(m.entries)):
        for attr in CHECKED_ATTRS.get(d[eid]["kind"], ()):
            want, have = m.entries[eid].get(attr), d[eid].get(attr)
            if want != have:
                out.changed.append(f"{eid}.{attr}: manifest {want!r}, code {have!r}")
    out.gate_problems = check_gates(m)
    return out


# ---- writing stubs (TOML) --------------------------------------------------------------------------------

DEFAULT_CARD = {
    "tool": "P0-06", "reserved_tool": "P0-06", "tool_param": "P0-06", "message": "P0-05", "field": "P0-05",
    "command": "P0-05", "command_arg": "P0-05", "error_code": "P0-05", "close_code": "P0-05", "binary_type": "P0-05",
    "ws_option": "P0-05", "body_method": "P0-06", "body_impl": "P0-06", "body_impl_method": "P0-06",
    "capability": "P0-06", "body_kind": "P0-06", "cli_command": "P0-04", "cli_arg": "P0-04", "config_key": "P0-04",
    "env_var": "P0-04", "store": "P0-04", "file_site": "P0-04", "net_module": "P0-06", "egress_host": "P0-06",
    "android_component": "P0-05", "android_action": "P0-05",
}
DEFAULT_BOUNDARY = {
    "tool": "B6", "reserved_tool": "B6", "tool_param": "B7", "message": "B1", "field": "B1", "command": "B1",
    "command_arg": "B1", "error_code": "B1", "close_code": "B1", "binary_type": "B2", "ws_option": "B1",
    "body_method": "B3", "body_impl": "B3", "body_impl_method": "B3", "capability": "B3", "body_kind": "B10",
    "cli_command": "B11", "cli_arg": "B11", "config_key": "B10", "env_var": "B11", "store": "B10",
    "file_site": "B10", "net_module": "B13", "egress_host": "B13", "android_component": "B12",
    "android_action": "B12",
}


def _card_for(eid: str, kind: str) -> str:
    text = eid.lower()
    if kind in ("cli_command", "cli_arg"):
        for key, card in (("brain.serve", "P0-05"), ("brain.probe", "P0-05"), ("brain.pair", "P0-05"),
                          ("brain.body-check", "P0-06"), ("brain.chaos", "X-01"), ("chaos.", "X-01")):
            if text.startswith(f"{kind}.{key}"):
                return card
    if kind in ("file_site", "net_module"):
        if "/transport/" in text:
            return "P0-05"
        if "/body/" in text or "/agent/" in text or "egress" in text:
            return "P0-06"
    if kind == "config_key" and (".body" in text or ".egress" in text):
        return "P0-06"
    return DEFAULT_CARD[kind]


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def stub_line(eid: str, attrs: dict[str, Any]) -> str:
    kind = attrs["kind"]
    fields = {"id": eid, "kind": kind, "card": _card_for(eid, kind), "boundary": DEFAULT_BOUNDARY[kind]}
    fields.update({a: attrs[a] for a in CHECKED_ATTRS.get(kind, ()) if a in attrs})
    return "{ " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in fields.items()) + " },"


def stub(missing: Iterable[str], discovered: Discovered) -> str:
    """TOML lines to paste into the manifest's `entry = [...]` array (review card and boundary)."""
    return "\n".join(stub_line(i, discovered[i]) for i in sorted(missing))
