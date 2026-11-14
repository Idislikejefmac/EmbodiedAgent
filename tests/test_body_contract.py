"""Runs the Body contract (tests/body_contract.py) against every body the brain has.

- sim: SimBody with everything, reading its media from folders of generated files. Passes in full.
- sim-carried: SimBody narrowed to what a body carried by a person has (no navigate, no body speech).
- sim-bare: SimBody with no optional capability at all.
- phone: PhoneBody over the real P0-05 server on loopback, with the protocol's test body client
  playing the phone (the real phone is not needed). Passes the implemented subset: status, events,
  stop; everything else must refuse with NotSupported.
"""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

import pytest

from body_contract import CHECKS, Harness
from brain.body import Capability
from brain.body.phone import PhoneBody
from brain.body.sim import SimBody
from brain.body.sim_assets import write_default_media
from brain.transport.client import TestBodyClient
from brain.transport.protocol import now_us

TOKEN = bytes(range(32))


@asynccontextmanager
async def sim_harness(tmp_path, capabilities=None):
    write_default_media(tmp_path / "images", tmp_path / "audio")
    body = SimBody(image_dir=tmp_path / "images", audio_dir=tmp_path / "audio", capabilities=capabilities)
    started = now_us()
    await body.start()

    async def emit(name, data):
        body.inject_event(name, data)

    async def drop():
        body.set_link(False)

    async def restore():
        body.set_link(True)

    try:
        yield Harness(body, emit, drop, restore, started)
    finally:
        await body.close()


@asynccontextmanager
async def phone_harness(tmp_path):
    # The slow-consumer check sends a burst of 400 events: the transport's per-type rate limit (H-09) is
    # raised here so the check exercises the body stream's own drop policy, not the flood limit.
    body = PhoneBody(token=TOKEN, port=0, phone_link=False, heartbeat_s=0.1, liveness_s=3.0,
                     peer_msg_max_per_s=10_000, peer_msg_burst=10_000)
    started = now_us()
    await body.start()
    clients = AsyncExitStack()
    current = []

    async def connect():
        client = TestBodyClient(f"ws://127.0.0.1:{body.port}/", token=TOKEN, heartbeat_s=0.1, liveness_s=3.0)
        current[:] = [await clients.enter_async_context(client)]
        assert await body.wait_connected(5.0)

    async def emit(name, data):
        await current[0].link.send("event", {"name": name, "data": data})

    async def drop():
        await clients.aclose()  # the test body hangs up; the adapter keeps listening

    try:
        await connect()
        yield Harness(body, emit, drop, connect, started)
    finally:
        await clients.aclose()
        await body.close()


HARNESSES = {
    "sim": lambda tmp: sim_harness(tmp),
    "sim-carried": lambda tmp: sim_harness(tmp, {Capability.POSE, Capability.IMAGE, Capability.AUDIO_IN,
                                                 Capability.AUDIO_OUT}),
    "sim-bare": lambda tmp: sim_harness(tmp, ()),
    "phone": lambda tmp: phone_harness(tmp),
}


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
@pytest.mark.parametrize("kind", HARNESSES)
def test_body_contract(kind, check, tmp_path):
    async def go():
        async with HARNESSES[kind](tmp_path) as harness:
            await check(harness)

    asyncio.run(asyncio.wait_for(go(), 60))


def test_sim_passes_the_contract_in_full(tmp_path):
    """SimBody must exercise every gated method, not skip any by lacking the capability."""
    async def go():
        async with sim_harness(tmp_path) as h:
            assert h.body.capabilities == frozenset(Capability)

    asyncio.run(go())


def test_phone_body_implements_exactly_the_core_today(tmp_path):
    """Card P0-06: status and events work; every gated method refuses. This test changes when a card
    builds one of them over the link."""
    async def go():
        async with phone_harness(tmp_path) as h:
            assert h.body.capabilities == frozenset()
            assert (await h.body.status()).battery_pct == 100.0

    asyncio.run(go())
