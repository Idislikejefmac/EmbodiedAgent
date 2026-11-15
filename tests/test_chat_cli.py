"""Card P0-07: `python -m brain chat` (brain/chat.py, brain/__main__.py), the new config key and the LinkBody
speak_local path. Child-process runs of `chat` with hostile argv and stdin are in tests/test_cli_end_to_end.py.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import pytest

from brain import __main__ as main_mod
from brain.agent.budget import load_prices
from brain.body.link import LinkBody
from brain.body.sim import SimBody
from brain.body.types import Capability
from brain.chat import ChatOptions, StartRefused, live_model, printable, run_chat
from brain.config import ConfigError, parse_config
from brain.transport.client import TestBodyClient
from fakes import TOKEN, run

pytestmark = pytest.mark.trace("T-P0-07-004", "T-P0-07-008", "T-P0-07-028")


def parse(*argv: str) -> argparse.Namespace:
    return main_mod.build_parser(None).parse_args(["chat", *argv])


# ---- CLI flags: the boundary matrix (valid, invalid, repeated, conflicting) ---------------------------------------


@pytest.mark.parametrize("argv, want", [
    ((), dict(as_app=False, live=False, fake_model=False, quiet=False, body=None)),
    (("--as-app",), dict(as_app=True)), (("--live",), dict(live=True)), (("--fake-model",), dict(fake_model=True)),
    (("--quiet",), dict(quiet=True)), (("--body", "sim"), dict(body="sim")), (("--body", "phone"), dict(body="phone")),
    (("--as-app", "--as-app"), dict(as_app=True)), (("--body", "phone", "--body", "sim"), dict(body="sim")),
])
def test_chat_flags_that_are_accepted(argv, want):
    ns = parse(*argv)
    for k, v in want.items():
        assert getattr(ns, k) == v, k


@pytest.mark.parametrize("argv", [("--live", "--fake-model"), ("--body",), ("--body", ""), ("--body", "robot"),
                                  ("--body", "SIM"), ("--quiet=1",), ("--as-app=yes",), ("--shell",), ("extra",),
                                  ("--body", "sim\x00"), ("--live=true",)])
def test_chat_flags_that_are_refused(argv, capsys):
    with pytest.raises(SystemExit) as e:
        parse(*argv)
    assert e.value.code == 2 and "usage" in capsys.readouterr().err.lower()


# ---- the config key [models] allow_live_calls --------------------------------------------------------------------


@pytest.mark.parametrize("value, ok", [(True, True), (False, True), (1, False), (0, False), ("true", False),
                                       ("", False), (None, False), ([], False), ({}, False), (1.0, False)])
def test_allow_live_calls_must_be_true_or_false(tmp_path, value, ok):
    raw: dict[str, Any] = {"models": {"allow_live_calls": value}}
    if ok:
        assert parse_config(raw, base=tmp_path).models.allow_live_calls is value
    else:
        with pytest.raises(ConfigError, match="allow_live_calls"):
            parse_config(raw, base=tmp_path)


def test_live_calls_are_off_unless_consented(tmp_path):
    assert parse_config({}, base=tmp_path).models.allow_live_calls is False
    with pytest.raises(ConfigError, match="did you mean 'allow_live_calls'"):
        parse_config({"models": {"allow_live_call": True}}, base=tmp_path)


# ---- the live start checks (no key here, and no network) -----------------------------------------------------------


def test_live_refuses_without_consent_then_without_a_key(tmp_path, monkeypatch):
    prices = load_prices()
    off = parse_config({}, base=tmp_path)
    with pytest.raises(StartRefused, match="allow_live_calls"):
        run(live_model(off, prices))
    on = parse_config({"models": {"allow_live_calls": True}}, base=tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("brain.egress.anthropic_api_key", lambda *a, **k: None)
    with pytest.raises(StartRefused, match="no ANTHROPIC_API_KEY"):
        run(live_model(on, prices))


def test_live_refuses_an_unpriced_model_and_a_missing_api_host(tmp_path, monkeypatch):
    prices = load_prices()
    monkeypatch.setattr("brain.egress.anthropic_api_key", lambda *a, **k: "sk-ant-test-key-0000000000")
    odd = parse_config({"models": {"allow_live_calls": True, "live_conversation": "claude-nonesuch-1"}}, base=tmp_path)
    with pytest.raises(StartRefused, match="no price"):
        run(live_model(odd, prices))
    on = parse_config({"models": {"allow_live_calls": True}}, base=tmp_path)
    with pytest.raises(StartRefused, match="egress"):  # the suite's guard allows no outside host
        run(live_model(on, prices))


def test_live_refuses_when_the_configured_model_is_not_listed(tmp_path, monkeypatch):
    from brain.agent import model as M
    from fakes.anthropic_fake import FakeAnthropic
    from brain.egress import EgressPolicy, build_anthropic_client

    fake = FakeAnthropic(models=("claude-haiku-5-5",))  # the careful-looking model is missing
    monkeypatch.setattr("brain.egress.anthropic_api_key", lambda *a, **k: "sk-ant-test-key-0000000000")
    monkeypatch.setattr("brain.egress.build_anthropic_client", lambda key: build_anthropic_client(
        key, policy=EgressPolicy.from_hosts(["api.anthropic.com"]), transport=fake.transport))
    on = parse_config({"models": {"allow_live_calls": True}}, base=tmp_path)
    with pytest.raises(StartRefused, match="not offered"):
        run(live_model(on, load_prices()))
    fake.models = ("claude-haiku-5-5", "claude-sonnet-5-5")
    model, key = run(live_model(on, load_prices()))
    assert isinstance(model, M.AnthropicModel) and key.startswith("sk-ant-test")
    run(model.aclose())


# ---- the console session in process ----------------------------------------------------------------------------


def test_run_chat_with_the_scripted_model_in_a_sandbox(tmp_path, capsys):
    async def lines():
        for line in ("hello", "what is your battery level?", "/poke", "/look what is here", "/help", "/quit", "ignored"):
            yield line
            await asyncio.sleep(0.05)

    config = parse_config({"logging": {"full_prompts": True}}, base=tmp_path)
    out: list[str] = []
    code = run(run_chat(config, SimBody(), options=ChatOptions(trace=False), lines=lines(), out=out.append))
    text = "\n".join(out) + capsys.readouterr().out
    assert code == 0 and "My battery is at" in text and "You poked me" in text and "commands:" in text
    assert (tmp_path / "data" / "capture").is_dir() and (tmp_path / "data" / "state.json").exists()
    state = __import__("json").loads((tmp_path / "data" / "state.json").read_text(encoding="utf-8"))
    assert state["spend_usd_total"] == 0.0  # the scripted model's pretend spend is never recorded as real


def test_run_chat_live_refuses_cleanly_in_process(tmp_path):
    out: list[str] = []
    code = run(run_chat(parse_config({}, base=tmp_path), SimBody(), options=ChatOptions(live=True), out=out.append))
    assert code == 2 and "allow_live_calls" in out[-1]


def test_terminal_output_is_cleaned():
    assert printable("ok\x1b[2J\x07 ‮done\n") == "ok[2J done\n"


# ---- LinkBody speak_local (the brain side of plan steps 5 and 6) ------------------------------------------------------


def test_speak_reaches_the_body_as_speak_local_when_the_body_offers_it():
    async def go():
        body = LinkBody(token=TOKEN, port=0)
        await body.start()
        try:
            async with TestBodyClient(f"ws://127.0.0.1:{body.port}/", token=TOKEN,
                                      capabilities=("event", "echo", "speak_local")) as client:
                assert await body.wait_connected(5) and Capability.SPEAK_TEXT in body.capabilities

                async def answer() -> dict:
                    r = await client.next_message(5)
                    await client.link.send("ack", {"status": "ok"}, reply_to=r.msg["id"])  # type: ignore[union-attr]
                    return r.msg

                waiter = asyncio.create_task(answer())
                await body.speak_text("Hello, I can hear you.", utterance_id="u-1")
                msg = await waiter
                assert msg["type"] == "speak_local" and msg["payload"] == {
                    "utterance_id": "u-1", "text": "Hello, I can hear you.", "interrupt": False}
                with pytest.raises(ValueError):
                    await body.speak_text("", utterance_id="u-2")
        finally:
            await body.close()

    run(go())


def test_speak_is_not_built_when_the_body_does_not_offer_speak_local():
    from brain.body.base import NotYetImplemented

    async def go():
        body = LinkBody(token=TOKEN, port=0)
        await body.start()
        try:
            async with TestBodyClient(f"ws://127.0.0.1:{body.port}/", token=TOKEN) as client:
                assert await body.wait_connected(5) and body.capabilities == frozenset()
                with pytest.raises(NotYetImplemented, match="P0-07"):
                    await body.speak_text("hi", utterance_id="u-1")
                assert client.inbox.empty()
        finally:
            await body.close()

    run(go())
