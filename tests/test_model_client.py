"""Card P0-07: the model API client built in brain/egress.py, the real SDK over an in-memory transport, and the
streamed-reply gate (brain/agent/model.py). docs/body_tools_egress_v0.md section 3 checklist, doc 10 T-P0-07-003,
-010, -012, -020, -025, -026.

Nothing here opens a socket to anything but loopback ports this process or a child of it holds. The checklist item
"one real API call succeeds and the only destination recorded is api.anthropic.com:443" needs the key and the real
API: it is the real_vendor test (tests/test_real_vendor.py), not run here.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from typing import Any

import httpx2
import pytest

from brain import egress
from brain.agent import model as M
from brain.agent.budget import SpendGuard, load_prices
from brain.agent.body_tools import register_body_tools
from brain.agent.capture import PromptCapture
from brain.agent.loop import Agent, AgentSettings, TurnInput, load_system_prompt
from brain.agent.model import AnthropicModel, ModelFailure, ModelRequest
from brain.agent.tools import InputSource, ToolRegistry
from brain.body.sim import SimBody
from brain.config import CapsConfig
from brain.egress import EgressDenied, EgressPolicy, VendorError, build_anthropic_client
from brain.limits import (MAX_ARGS_BYTES, MAX_MODEL_BLOCKS, MAX_MODEL_TEXT_CHARS, MODEL_BLOCK_MAX_BYTES,
                          MODEL_CONNECT_TIMEOUT_S, MODEL_STREAM_EVENTS_MAX, MODEL_STREAM_IDLE_S, MODELS_LISTED_MAX)
from fakes import run
from fakes.anthropic_fake import FakeAnthropic, error, raw, sse_events, sse_reply, stall, text, tool
from fakes.egress_policy import testing_guard
from fakes.models import FakeClock

pytestmark = pytest.mark.trace("T-P0-07-003", "T-P0-07-010", "T-P0-07-012", "T-P0-07-020", "T-P0-07-025",
                               "T-P0-07-026", risks=("R-FND-40", "R-FND-42", "R-FND-43"))

API = EgressPolicy.from_hosts(["api.anthropic.com"])
CANARY_KEY = "sk-ant-canary-" + "k3y" * 12


def client(fake: FakeAnthropic, key: str = CANARY_KEY) -> AnthropicModel:
    return AnthropicModel(build_anthropic_client(key, policy=API, transport=fake.transport))


def request(**kw: Any) -> ModelRequest:
    base = dict(model="claude-haiku-5-5", max_tokens=64, system_blocks=({"type": "text", "text": "be brief"},),
                tools=(), messages=({"role": "user", "content": [{"type": "text", "text": "hi"}]},), effort="low")
    base.update(kw)
    return ModelRequest(**base)  # type: ignore[arg-type]


# ---- the real SDK over FakeAnthropic -------------------------------------------------------------------------------


def test_a_streamed_text_reply_arrives_in_order_with_its_usage():
    async def go():
        fake = FakeAnthropic([sse_reply(text("Hello there, Jeff."), usage={"output_tokens": 9, "cache_read_input_tokens": 40,
                                                                           "cache_creation_input_tokens": 5})])
        m = client(fake)
        pieces: list[str] = []
        reply = await m.complete(request(), pieces.append)
        await m.aclose()
        assert "".join(pieces) == reply.text == "Hello there, Jeff." and len(pieces) > 1
        assert reply.stop_reason == "end_turn"
        assert (reply.usage.input_tok, reply.usage.output_tok, reply.usage.cache_read_tok, reply.usage.cache_write_tok) == (
            120, 9, 40, 5)
        sent = fake.message_requests[0]
        assert sent["host"] == "api.anthropic.com" and sent["json"]["stream"] is True
        assert sent["json"]["output_config"] == {"effort": "low"} and sent["json"]["max_tokens"] == 64

    run(go())


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 8, 13, 1000])
def test_tool_json_split_at_any_boundary_gives_the_same_call(chunk):
    """T-P0-07-003: a tool call assembled from partial JSON deltas equals the call, wherever the JSON is cut."""
    args = {"text": "Hello, I can hear you.", "interrupt": True}

    async def go():
        fake = FakeAnthropic([sse_reply(text("One moment."), tool("speak", args, "toolu_abc"), stop="tool_use", chunk=chunk)])
        reply = await client(fake).complete(request())
        assert reply.tool_calls == (M.ToolCall("toolu_abc", "speak", args),)
        assert reply.blocks[1] == {"type": "tool_use", "id": "toolu_abc", "name": "speak", "input": args}

    run(go())


def test_cache_control_reaches_the_wire_and_cache_use_shows_in_the_cost_events():
    async def go():
        fake = FakeAnthropic([sse_reply(text("Hi."), usage={"cache_read_input_tokens": 3000})])
        body = SimBody()
        registry = ToolRegistry()
        register_body_tools(registry, body)
        clock = FakeClock()
        guard = SpendGuard(CapsConfig(), load_prices(), None, now=clock.now, today=clock.today)
        agent = Agent(registry, client(fake), guard, settings=AgentSettings(), prompt=load_system_prompt())
        events: list[dict[str, Any]] = []
        from brain import obs

        obs.LOG.reset_limits()
        obs.LOG.add_sink(events.append)
        try:
            async with body:
                r = await agent.run_turn(TurnInput("hi", InputSource.LOCAL_CONSOLE))
        finally:
            obs.LOG.remove_sink(events.append)
        body_json = fake.message_requests[0]["json"]
        assert r.outcome == "ok"
        assert body_json["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        assert body_json["system"][-1]["cache_control"] == {"type": "ephemeral"}
        cost = [e for e in events if e["type"] == "cost.update"][0]["f"]
        assert cost["cache_read_tok"] == 3000 and cost["usd"] > 0

    run(go())


@pytest.mark.parametrize("step, code, retryable", [
    (error(429, "rate_limit_error", {"retry-after": "2"}), "rate_limited", True),
    (error(529, "overloaded_error"), "overloaded", True), (error(500, "api_error"), "server_error", True),
    (error(503, "api_error"), "server_error", True), (error(401, "authentication_error"), "auth", False),
    (error(403, "permission_error"), "auth", False), (error(404, "not_found_error"), "not_found", False),
    (error(400, "invalid_request_error"), "bad_request", False), (error(413, "request_too_large"), "too_large", False),
])
def test_http_errors_become_one_word_failures(step, code, retryable):
    async def go():
        with pytest.raises(ModelFailure) as e:
            await client(FakeAnthropic([step])).complete(request())
        assert e.value.code == code and e.value.retryable is retryable and "scripted failure" not in str(e.value)
        if code == "rate_limited":
            assert e.value.retry_after_s == 2.0

    run(go())


@pytest.mark.parametrize("body, code", [
    (b"event: message_start\ndata: {not json\n\n", "malformed"),
    (sse_events(text("cut"), stop_event=False), "truncated"),
    (sse_events(tool("speak", None, raw_json='{"text": "hel'), stop="tool_use")[:-120], "truncated"),
    (b"event: error\ndata: {\"type\": \"error\", \"error\": {\"type\": \"overloaded_error\", \"message\": \"x\"}}\n\n", "overloaded"),
    (b"", "truncated"),
    (b"event: message_start\ndata: {\"type\": \"message_start\"}\n\n", "malformed"),
])
def test_malformed_or_cut_streams_are_refused(body, code):
    async def go():
        with pytest.raises(ModelFailure) as e:
            await client(FakeAnthropic([raw(body)])).complete(request())
        assert e.value.code in (code, "server_error") and e.value.usage is None or e.value.code == code

    run(go())


def test_malformed_tool_json_reaches_the_registry_as_a_string_and_is_refused():
    async def go():
        fake = FakeAnthropic([sse_reply(tool("speak", None, "toolu_x", raw_json='{"text": "hi",,}'), stop="tool_use")])
        reply = await client(fake).complete(request())
        call = reply.tool_calls[0]
        assert call.arguments == '{"text": "hi",,}'
        r = await ToolRegistry().call(call.name, call.arguments, __import__("brain.agent.tools").agent.tools.APP)
        assert r.status == "refused" and r.code == "invalid_arguments"

    run(go())


def test_a_stalled_stream_is_abandoned():
    async def go():
        events = sse_events(text("slow"))
        fake = FakeAnthropic([stall(5.0, events[: len(events) // 2])])
        m = client(fake)
        it = m._client.stream_events(AnthropicModel.params(request()))
        with pytest.raises(ModelFailure) as e:
            await M._read_stream(it, idle_s=0.3)
        assert e.value.code == "stalled" and e.value.retryable

    run(go())


def test_the_model_list_is_read_for_the_start_check():
    async def go():
        ids = await client(FakeAnthropic(models=("claude-haiku-5-5", "claude-sonnet-5-5", "x"))).list_models()
        assert ids == ["claude-haiku-5-5", "claude-sonnet-5-5", "x"] and len(ids) <= MODELS_LISTED_MAX

    run(go())


def test_the_key_is_sent_only_in_its_header_and_never_captured(tmp_path):
    """T-P0-07-012 with a canary key: it goes to the API in x-api-key and nowhere else (capture, events, repr)."""
    async def go():
        fake = FakeAnthropic([sse_reply(text("ok"))])
        m = client(fake)
        cap = PromptCapture(tmp_path, enabled=True, max_mb=1, max_days=1, secrets=[CANARY_KEY])
        req = request(messages=({"role": "user", "content": [{"type": "text", "text": f"my key is {CANARY_KEY}"}]},))
        cap.request("m-1", req)
        reply = await m.complete(req)
        cap.reply("m-1", reply)
        sent = fake.message_requests[0]
        carrying = [k for k, v in sent["headers"].items() if CANARY_KEY in v]
        assert carrying in (["x-api-key"], ["authorization"])
        assert CANARY_KEY not in repr(m._client)
        text_on_disk = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "capture").glob("*.jsonl"))
        assert CANARY_KEY not in text_on_disk and "<redacted:secret>" in text_on_disk

    run(go())


# ---- the factory and the egress checklist (docs/body_tools_egress_v0.md section 3) -------------------------------------


def test_the_factory_builds_a_client_that_ignores_the_environment_and_does_not_retry():
    c = build_anthropic_client(CANARY_KEY, policy=API)
    sdk = c._sdk
    assert sdk.max_retries == 0 and str(sdk.base_url).startswith("https://api.anthropic.com")
    http = sdk._client
    assert isinstance(http, httpx2.AsyncClient) and http.trust_env is False and http.follow_redirects is False
    assert sdk.timeout.connect == MODEL_CONNECT_TIMEOUT_S and sdk.timeout.read == MODEL_STREAM_IDLE_S
    run(c.aclose())


@pytest.mark.parametrize("base_url", ["https://192.0.2.1/", "http://api.anthropic.com/", "https://api.anthropic.com:80/",
                                      "https://evil.example.org/", "https://user:pw@api.anthropic.com/",
                                      "https://api.anthropic.com.evil.example/", "ftp://api.anthropic.com/", "", "::::"])
def test_the_factory_refuses_any_other_destination(base_url):
    with pytest.raises(EgressDenied):
        build_anthropic_client(CANARY_KEY, base_url=base_url, policy=API)


def test_the_factory_refuses_with_an_empty_allowlist_and_by_default_under_the_suite_guard():
    with pytest.raises(EgressDenied):
        build_anthropic_client(CANARY_KEY, policy=EgressPolicy())
    with pytest.raises(EgressDenied):  # no policy given: the installed guard's (loopback only in the suite) decides
        build_anthropic_client(CANARY_KEY)


@pytest.mark.parametrize("key", ["", "a\nb", "x" * 5000, None, 7])
def test_the_factory_refuses_an_unusable_key(key):
    with pytest.raises(ValueError):
        build_anthropic_client(key, policy=API)  # type: ignore[arg-type]


def test_the_process_guard_stops_the_sdk_even_if_the_factory_were_bypassed():
    """A client built by hand (not through the factory) with a documentation address is refused by the guard
    before anything is sent: EgressDenied is in the cause chain and becomes VendorError("egress_denied")."""
    import anthropic

    async def go():
        sdk = anthropic.AsyncAnthropic(api_key=CANARY_KEY, base_url="https://192.0.2.1/", max_retries=0,
                                       http_client=httpx2.AsyncClient(trust_env=False))
        with pytest.raises(anthropic.APIConnectionError) as e:
            await sdk.messages.create(model="m", max_tokens=1, messages=[{"role": "user", "content": "x"}])
        chain, cur = [], e.value
        while cur is not None:
            chain.append(cur)
            cur = cur.__cause__ or cur.__context__
        assert any(isinstance(c, EgressDenied) for c in chain)
        assert egress._vendor_error(e.value).code == "egress_denied"
        await sdk.close()

    run(go())


class _Listener:
    """A loopback TCP listener in ANOTHER process (a stand-in for a local proxy): the guard allows loopback only to
    ports this process listens on, so a client that tried to use it would be refused. Reports whether anything
    connected."""

    SCRIPT = ("import socket,sys\ns=socket.socket();s.bind(('127.0.0.1',0));s.listen(5);s.settimeout(8)\n"
              "print(s.getsockname()[1],flush=True)\n"
              "try:\n c,_=s.accept();print('CONNECTED',flush=True)\nexcept OSError:\n print('NOTHING',flush=True)\n")

    def __enter__(self) -> _Listener:
        self.p = subprocess.Popen([sys.executable, "-I", "-c", self.SCRIPT], stdout=subprocess.PIPE, text=True)
        self.port = int(self.p.stdout.readline())  # type: ignore[union-attr]
        return self

    def __exit__(self, *exc: object) -> None:
        self.p.kill()
        self.p.communicate(timeout=10)

    def got_connection(self) -> bool:
        self.p.kill()
        out, _ = self.p.communicate(timeout=10)
        return "CONNECTED" in out


def test_proxy_settings_cannot_carry_the_model_call(monkeypatch):
    """Checklist: HTTPS_PROXY and ALL_PROXY point at a loopback listener in another process. The factory's client
    ignores them (trust_env=False) and goes direct, which the suite's loopback-only guard refuses at the name lookup of
    the API host; the listener receives nothing. An explicit proxy is refused at its loopback connection."""
    from brain import obs

    seen: list[str] = []  # what the guard refused (its own audit hook runs first, so it is read from its events)

    def sink(e: dict[str, Any]) -> None:
        if e["type"] == "egress.denied":
            seen.append(str(e["f"].get("what")))

    obs.LOG.reset_limits()
    obs.LOG.add_sink(sink)
    with _Listener() as lis:
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{lis.port}")
        monkeypatch.setenv("ALL_PROXY", f"http://127.0.0.1:{lis.port}")

        async def go() -> str:
            m = AnthropicModel(build_anthropic_client(CANARY_KEY, policy=API))  # the real transport
            try:
                await m.complete(request())
            except ModelFailure as e:
                return e.code
            finally:
                await m.aclose()
            return "sent"

        try:
            code = run(go())
        finally:
            obs.LOG.remove_sink(sink)
        assert code == "egress_denied"
        assert any("api.anthropic.com" in s for s in seen) and not any(str(lis.port) in s for s in seen)

        async def explicit() -> None:
            async with httpx2.AsyncClient(proxy=f"http://127.0.0.1:{lis.port}", trust_env=False) as h:
                await h.get("https://api.anthropic.com/v1/models")

        with pytest.raises(Exception) as e:
            run(explicit())
        chain, cur = [], e.value
        while cur is not None:
            chain.append(cur)
            cur = cur.__cause__ or cur.__context__
        assert any(isinstance(c, EgressDenied) for c in chain) or "EgressDenied" in repr(e.value)
        assert not lis.got_connection()


def test_install_guard_strips_the_proxy_variables():
    removed = egress.strip_proxy_environment.__doc__
    assert removed and "never their values" in removed
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
    try:
        assert "HTTPS_PROXY" in egress.strip_proxy_environment() and "HTTPS_PROXY" not in os.environ
    finally:
        os.environ.pop("HTTPS_PROXY", None)


def test_vendor_errors_carry_no_vendor_text():
    e = VendorError("rate_limited", status=429, retryable=True)
    assert str(e) == "model API call failed: rate_limited (HTTP 429)" and VendorError("nonsense").code == "other"


def test_the_key_is_read_from_the_environment_or_dotenv(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=from-file-123\n", encoding="utf-8")
    assert egress.anthropic_api_key(env, environ={}) == "from-file-123"
    assert egress.anthropic_api_key(env, environ={"ANTHROPIC_API_KEY": "from-env"}) == "from-env"
    assert egress.anthropic_api_key(tmp_path / "none", environ={}) is None


# ---- the streamed-reply gate on hand-made event dicts (T-P0-07-020) --------------------------------------------------


def _events(*evs: Any) -> Any:
    async def gen() -> Any:
        for e in evs:
            yield e
    return gen()


START = {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}}
STOP = [{"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"}]


def _text_block(i: int, t: str) -> list[dict[str, Any]]:
    return [{"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": t}},
            {"type": "content_block_stop", "index": i}]


def _tool_block(i: int, cid: Any, name: Any = "speak", js: str = "{}") -> list[dict[str, Any]]:
    return [{"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": cid, "name": name}},
            {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": js}},
            {"type": "content_block_stop", "index": i}]


def gate(*evs: Any, **kw: Any) -> Any:
    return run(M._read_stream(_events(*evs), **kw))


def test_the_gate_accepts_a_plain_reply():
    r = gate(START, *_text_block(0, "hi"), *STOP)
    assert r.text == "hi" and r.usage.output_tok == 3 and r.stop_reason == "end_turn"


@pytest.mark.parametrize("evs", [
    [START, *_tool_block(0, "t1"), *_tool_block(1, "t1"), *STOP],  # duplicate tool ids
    [START, *_tool_block(0, None), *STOP],  # no id
    [START, *_tool_block(0, "bad id!"), *STOP],  # id of the wrong shape
    [START, *_tool_block(0, "t1", name=7), *STOP],  # name not text
    [START, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}}, *STOP],
    [START, *_text_block(MAX_MODEL_BLOCKS, "x"), *STOP],  # index out of range
    [START, *_text_block(0, "a"), *_text_block(0, "b"), *STOP],  # a block started twice
    [{"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}],  # before message_start
    [START, START, *STOP],
    [START, "not a dict", *STOP],
    [{"type": "message_start", "message": {"usage": {"input_tokens": -5}}}, *STOP],
    [{"type": "message_start", "message": {"usage": {"input_tokens": True}}}, *STOP],
])
def test_the_gate_refuses_malformed_streams(evs):
    with pytest.raises(ModelFailure) as e:
        gate(*evs)
    assert e.value.code == "malformed"


def test_the_gate_drops_unknown_block_and_event_types():
    r = gate(START, {"type": "content_block_start", "index": 0, "content_block": {"type": "server_magic"}},
             {"type": "content_block_delta", "index": 0, "delta": {"type": "x"}}, {"type": "content_block_stop", "index": 0},
             {"type": "ping"}, {"type": "brand_new_event"}, *_text_block(1, "ok"), *STOP)
    assert r.text == "ok" and r.dropped >= 3 and [b["type"] for b in r.blocks] == ["text"]


def test_the_gate_cuts_text_and_bounds_events_and_blocks():
    r = gate(START, *_text_block(0, "x" * (MAX_MODEL_TEXT_CHARS + 10)), *STOP)
    assert len(r.text) == MAX_MODEL_TEXT_CHARS and r.text_cut
    flood = [{"type": "ping"}] * (MODEL_STREAM_EVENTS_MAX + 1)
    with pytest.raises(ModelFailure) as e:
        gate(START, *flood, *STOP)
    assert e.value.code == "too_large"
    big_thinking = [{"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
                    {"type": "content_block_delta", "index": 0,
                     "delta": {"type": "thinking_delta", "thinking": "t" * (MODEL_BLOCK_MAX_BYTES + 1)}},
                    {"type": "content_block_stop", "index": 0}]
    with pytest.raises(ModelFailure):
        gate(START, *big_thinking, *STOP)


def test_tool_json_over_the_argument_limit_is_not_parsed():
    r = gate(START, *_tool_block(0, "t1", js='{"text": "' + "x" * MAX_ARGS_BYTES + '"}'),
             {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}, {"type": "message_stop"})
    assert isinstance(r.tool_calls[0].arguments, str) and r.blocks[0]["input"] == {}


def test_thinking_blocks_are_kept_whole_for_the_next_request():
    r = gate(START, {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
             {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}},
             {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
             {"type": "content_block_stop", "index": 0}, *_text_block(1, "ok"), *STOP)
    assert r.blocks[0] == {"type": "thinking", "thinking": "hmm", "signature": "sig"}


def test_a_stream_without_message_stop_or_with_an_open_block_is_truncated():
    with pytest.raises(ModelFailure) as e:
        gate(START, *_text_block(0, "x"))
    assert e.value.code == "truncated" and e.value.usage is not None and e.value.usage.input_tok == 10
    with pytest.raises(ModelFailure) as e:
        gate(START, *_text_block(0, "x")[:2], *STOP)
    assert e.value.code == "truncated"


def test_a_display_callback_that_fails_does_not_break_the_reply():
    def boom(_: str) -> None:
        raise RuntimeError("display broke")

    assert gate(START, *_text_block(0, "hi"), *STOP, on_text=boom).text == "hi"


def test_suite_policy_is_unchanged_after_these_tests():
    assert egress._policy is not None and egress._policy.allowed == frozenset()
    with testing_guard(API):
        assert egress._policy.allows("api.anthropic.com", 443)
    assert egress._policy.allowed == frozenset()
    with pytest.raises(EgressDenied):
        socket.getaddrinfo("api.anthropic.com", 443)
    assert asyncio.iscoroutinefunction(AnthropicModel.complete) and json
