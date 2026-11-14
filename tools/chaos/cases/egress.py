"""P0-06 egress chokepoint (B13): nothing leaves except to the allowlist, loopback only to this process.

The harness runs under a loopback-only guard (no [egress] destinations), so every outside
destination must be refused by the guard before any packet leaves. The cases use addresses that
route nowhere (RFC 5737) and closed loopback ports, so even a broken guard sends nothing anywhere.
"""

from __future__ import annotations

import asyncio
import os
import socket
from typing import Any

from brain import egress
from brain.egress import EgressDenied, EgressPolicy

from ..api import Case, Ctx, Gen, experiment, failure, hostile, invalid, valid


def _closed_loopback_port() -> int:
    """A loopback port nothing listens on, that this process held a moment ago and has released."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def gen_valid(g: Gen) -> list[Case]:
    return [valid("own_listener_blocking", {"op": "own_listener", "how": "blocking"}),
            valid("own_listener_asyncio", {"op": "own_listener", "how": "asyncio"}),
            valid("policy_from_plain_hosts", {"op": "policy", "hosts": ["api.example.org", "10.0.0.5:8443"],
                                              "ok": True})]


def gen_invalid(g: Gen) -> list[Case]:
    bad = [("wildcard", "*.example.org", "N5"), ("scheme", "https://api.example.org", "N5"),
           ("path", "api.example.org/v1", "N5"), ("port_zero", "host.example:0", "N6"),
           ("port_huge", "host.example:99999", "N6"), ("not_a_string", 5, "N1"), ("empty", "", "N3"),
           ("loopback_entry", "127.0.0.1:8080", "N5"), ("localhost", "localhost", "N5"),
           ("space", "a b", "N5")]
    out = [invalid(f"policy_{n}", {"op": "policy", "hosts": [h], "ok": False}, t) for n, h, t in bad]
    out.append(invalid("policy_hosts_not_a_list", {"op": "policy_config", "config": {"egress": {"allowed_hosts":
                                                                                         "api.example.org"}}}, "N1"))
    return out


def gen_hostile(g: Gen) -> list[Case]:
    return [
        hostile("loopback_foreign_port_blocking", {"op": "foreign_loopback", "how": "blocking"}, "N7"),
        hostile("loopback_foreign_port_asyncio", {"op": "foreign_loopback", "how": "asyncio"}, "N7"),
        hostile("documentation_address", {"op": "connect", "host": "192.0.2.1", "port": 443}, "N7"),
        hostile("unlisted_name_lookup", {"op": "lookup", "host": "unlisted-host.invalid"}, "N7"),
        hostile("model_api_refused_in_harness", {"op": "connect", "host": "api.anthropic.com", "port": 443}, "N16"),
        hostile("proxy_variables_removed", {"op": "proxy_env"}, "N7"),
    ]


def gen_failure(g: Gen) -> list[Case]:
    return [failure("guard_cannot_be_widened", {"op": "widen"}, "N13")]


async def _connect(host: str, port: int, how: str) -> None:
    if how == "blocking":
        with socket.create_connection((host, port), timeout=2):
            pass
    else:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 2)
        writer.close()


@experiment(
    id="P0-06.egress.guard", card="P0-06",
    entries=("egress_host.*", "env_var.*PROXY", "net_module.brain/egress.py:socket",
             "gate.brain.egress.EgressPolicy.from_hosts", "gate.brain.egress.EgressPolicy.from_config"),
    hypothesis="Under the harness guard every connection or lookup outside this process is refused with "
               "EgressDenied before anything is sent; loopback works only to ports this process listens on; "
               "proxy variables are removed; the guard cannot be widened.",
    blast_radius="loopback", valid=gen_valid, invalid=gen_invalid, hostile=gen_hostile, failure=gen_failure)
async def check(case: Case, ctx: Ctx) -> Any:
    d = case.data
    op = d["op"]
    if op == "own_listener":
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            await _connect("127.0.0.1", port, d["how"])
        finally:
            server.close()
            await server.wait_closed()
        return "connected to own listener"
    if op == "policy":
        try:
            policy = EgressPolicy.from_hosts(d["hosts"])
        except ValueError:
            ctx.expect(not d["ok"], "validate_before_use", "a plain host entry was refused")
            return "refused"
        ctx.expect(d["ok"], "validate_before_use", f"accepted a bad entry: {sorted(policy.allowed)}")
        return sorted(policy.allowed)
    if op == "policy_config":
        try:
            EgressPolicy.from_config(d["config"])
        except ValueError:
            return "refused"
        ctx.expect(False, "fail_closed", "a malformed [egress] section was accepted")
    if op in ("foreign_loopback", "connect"):
        host, port = ("127.0.0.1", _closed_loopback_port()) if op == "foreign_loopback" else (d["host"], d["port"])
        try:
            await _connect(host, port, d.get("how", "blocking"))
        except EgressDenied:
            return "denied"
        except OSError as e:
            ctx.expect(False, "no_forbidden_side_effect",
                       f"the connection attempt to {host}:{port} got past the guard ({type(e).__name__})")
        ctx.expect(False, "no_forbidden_side_effect", f"connected to {host}:{port}")
    if op == "lookup":
        try:
            socket.getaddrinfo(d["host"], 443)
        except EgressDenied:
            return "denied"
        except OSError as e:
            ctx.expect(False, "no_forbidden_side_effect", f"the lookup got past the guard ({type(e).__name__})")
        ctx.expect(False, "no_forbidden_side_effect", "an unlisted name was looked up")
    if op == "proxy_env":
        planted = {"HTTP_PROXY": "http://user:pw@127.0.0.1:3128", "https_proxy": "http://127.0.0.1:3128"}
        saved = {k: os.environ.get(k) for k in planted}
        os.environ.update(planted)
        try:
            removed = egress.strip_proxy_environment()
            left = [k for k in os.environ if k.upper() in egress.PROXY_VARIABLES]
            ctx.expect(not left, "no_forbidden_side_effect", f"proxy variables left in the environment: {left}")
            ctx.expect("pw" not in repr(removed), "no_secret_in_output", "a proxy password was returned")
            return removed
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    if op == "widen":
        current = egress._policy
        try:
            egress.install_guard(EgressPolicy.from_hosts(["wider.example"]))
        except RuntimeError:
            ctx.expect(egress._policy is current, "fail_closed", "the policy changed although install refused")
            return "refused"
        ctx.expect(False, "fail_closed", "install_guard widened the policy")
    raise ValueError(f"unknown op {op!r}")
