"""Card P0-06 acceptance: the brain runs end to end against SimBody with the phone switched off, no brain
module imports the phone body directly (tests/test_boundary_lint.py), and the one-page guide names
every method of the interface and its units."""

import asyncio
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

from brain.body import Body, Capability, NavGoal, NotSupported
from brain.body.types import AudioChunk
from brain.body.link import LinkBody
from brain.body.replay import ReplayBody
from brain.body.sim import SimBody

REPO = Path(__file__).resolve().parent.parent

MANDATORY = {"name", "capabilities", "connected", "start", "close", "wait_connected", "clock_sync", "status",
             "events", "stop"}
GATED = {"pose_stream": Capability.POSE, "capture_image": Capability.IMAGE, "audio_in_stream": Capability.AUDIO_IN,
         "play_audio": Capability.AUDIO_OUT, "speak_text": Capability.SPEAK_TEXT, "navigate": Capability.NAVIGATE}


def test_brain_runs_end_to_end_against_sim_body_without_the_phone():
    out = subprocess.run([sys.executable, "-m", "brain", "body-check", "--body", "sim"], capture_output=True,
                         text=True, cwd=REPO, timeout=120)
    text = out.stdout + out.stderr
    assert out.returncode == 0, text
    for tool in ("get_body_status", "get_pose", "capture_image", "speak", "stop"):
        assert f"tool {tool}: ok/ok" in text, text
    for tool in ("listen", "who_is_present", "navigate_to"):
        assert f"tool {tool}: unavailable/not_built" in text, text
    assert text.count("refused/unknown_tool") == 8 and text.count("REFUSED tool=") == 8, text
    assert "phone body module loaded: False" in text  # the phone body's code was never even imported
    assert "body-check: ok" in text


def test_the_interface_surface_is_exactly_what_is_documented():
    """A change to the Body surface must be made on purpose: here, in the contract and in the guide."""
    assert Body.__abstractmethods__ == frozenset(MANDATORY)  # the gated methods have defaults that refuse
    assert set(GATED.values()) == set(Capability)
    assert all(callable(getattr(Body, name)) for name in GATED)
    for cls in (SimBody, LinkBody, ReplayBody):
        assert not cls.__abstractmethods__, cls
    for name in ("status", "stop", "capture_image", "play_audio", "speak_text", "navigate", "start", "close",
                 "wait_connected"):
        assert inspect.iscoroutinefunction(getattr(Body, name)), name
    for name in ("events", "pose_stream", "audio_in_stream", "clock_sync"):
        assert not inspect.iscoroutinefunction(getattr(Body, name)), name  # streams raise at the call


def test_a_body_that_implements_only_the_core_refuses_everything_else_by_default():
    """Gated methods are not abstract: a new body overrides what it has, and a capability added to the
    interface later costs existing bodies nothing."""
    class CoreOnly(ReplayBody):
        pose_stream = Body.pose_stream
        capture_image = Body.capture_image
        audio_in_stream = Body.audio_in_stream
        play_audio = Body.play_audio
        speak_text = Body.speak_text
        navigate = Body.navigate

    class Liar(CoreOnly):  # lists a capability it never implemented
        capabilities = frozenset({Capability.IMAGE})

    async def go():
        body = CoreOnly("s-1")
        chunk = AudioChunk(0, 0, 16000, 1, bytes(320))
        calls = {Capability.POSE: lambda: body.pose_stream(), Capability.IMAGE: lambda: body.capture_image(),
                 Capability.AUDIO_IN: lambda: body.audio_in_stream(),
                 Capability.AUDIO_OUT: lambda: body.play_audio(chunk, utterance_id="u-1"),
                 Capability.SPEAK_TEXT: lambda: body.speak_text("hi", utterance_id="u-1"),
                 Capability.NAVIGATE: lambda: body.navigate(NavGoal("f", (0, 0, 0)))}
        assert set(calls) == set(Capability)
        for capability, call in calls.items():
            with pytest.raises(NotSupported) as e:
                result = call()
                if asyncio.iscoroutine(result):
                    await result
            assert e.value.capability is capability and e.value.code == "not_supported"
        with pytest.raises(NotSupported, match="does not implement"):
            await Liar("s-1").capture_image()

    asyncio.run(go())


def test_the_guide_names_every_method_and_its_units():
    guide = (REPO / "docs" / "adding_a_body.md").read_text(encoding="utf-8")
    assert len(guide.splitlines()) <= 75  # one page
    for member in MANDATORY | set(GATED):
        assert re.search(rf"\| `{member}[`(]", guide), f"{member} is missing from the methods table"
    for capability in Capability:
        assert f"`{capability.name}`" in guide
    for unit in ("_m", "_rad", "_deg", "_us", "_ms", "_s", "_hz", "_pct", "_c", "_px"):
        assert f"`{unit}`" in guide, unit
    for field in ("position_m", "orientation_xyzw", "heading_rad", "max_edge_px", "rate_hz", "timeout_s", "battery_pct",
                  "temperature_c", "link_rtt_ms", "sample_rate_hz", "offset_us", "rtt_us", "drift_ppm", "t_us",
                  "t_body_us", "frame_id", "camera_pose", "stale", "dropped"):
        assert field in guide, field
    for word in ("metres", "microseconds", "hertz", "pixels", "Celsius", "radians", "16000 Hz", "80 ms"):
        assert word in guide, word
