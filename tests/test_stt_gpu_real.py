"""Card P2-04, GPU half: the REAL GPU with the real models (base.en and small.en). Deselected in every normal run.

    .\\.venv\\Scripts\\python -m pytest tests\\test_stt_gpu_real.py --run-real-model

Needs the models (python tools/fetch_stt_model.py), the CUDA wheels (pip install -e ".[stt-gpu]") and an NVIDIA GPU with a
CUDA 12 driver (525 or newer); without a CUDA device every test here skips cleanly. Test speech is made by Windows' SAPI
synthesiser in pytest's temporary folder (never the repository). Local only, no network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import statistics
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any

import pytest
from stt_helpers import chunks
from wer import wer

from brain.config import load_config
from brain.voice.stt import LocalWhisperEngine, register_cuda_dlls

pytestmark = [pytest.mark.real_model, pytest.mark.trace("T-P2-04-007", "T-P2-04-013")]

SENTENCES = [
    "Please turn left and go to the kitchen.",
    "What time is it, Jeff?",
    "Take a picture of the front door and tell me what you see.",
    "There are twenty three apples and forty seven oranges.",
    "Stop right now.",
    "Hello Syd, this is Jeff speaking.",
]
SAPI = """\
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$i = 0
foreach ($line in $args[1..($args.Count - 1)]) { $i++; $s.SetOutputToWaveFile((Join-Path $args[0] ("u$i.wav")), $fmt); $s.Speak($line); $s.SetOutputToNull() }
$s.Dispose()
"""
SECOND_DECODE_MAX_MS = 600  # generous: measured 70 ms (base.en) and 140 ms (small.en)


def _cuda_devices() -> int:
    register_cuda_dlls()
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:  # noqa: BLE001  # broad-except: any failure here simply means there is no GPU to test
        return 0


@pytest.fixture(scope="module")
def speech(tmp_path_factory: pytest.TempPathFactory) -> list[bytes]:
    if os.name != "nt":
        pytest.skip("the SAPI synthesiser is Windows only")
    if _cuda_devices() <= 0:
        pytest.skip("no CUDA device is usable here")
    folder = tmp_path_factory.mktemp("sapi_gpu")
    script = folder / "gen.ps1"
    script.write_text(SAPI, encoding="utf-8")
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), str(folder), *SENTENCES],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("PowerShell is not available")
    if done.returncode != 0:
        pytest.skip("the SAPI synthesiser is not available")
    out = []
    for i in range(1, len(SENTENCES) + 1):
        with wave.open(str(folder / f"u{i}.wav")) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
            out.append(w.readframes(w.getnframes()))
    return out


async def _decode(e: LocalWhisperEngine, pcm: bytes, uid: str) -> Any:
    e.start(uid)
    for c in chunks(pcm):
        e.feed(c)
    return await e.finish()


@pytest.fixture(scope="module", params=["base.en", "small.en"])
def gpu_run(request: pytest.FixtureRequest, speech: list[bytes]) -> dict[str, Any]:
    """One real GPU engine per model: warm() first, then every sentence three times. Explicit device = cuda, so a broken
    GPU stack fails the test instead of quietly using the CPU."""
    cfg = dataclasses.replace(load_config().stt, model=request.param, device="cuda", compute_type="int8")
    e = LocalWhisperEngine(cfg)

    async def go() -> dict[str, Any]:
        warm_ms = await e.warm_async()
        rounds = [[await _decode(e, pcm, f"r{r}u{i}") for i, pcm in enumerate(speech)] for r in range(3)]
        return {"model": request.param, "engine": e, "warm_ms": warm_ms, "rounds": rounds}

    result = asyncio.run(go())
    yield result
    e.close()


def test_the_gpu_is_used_with_int8_float16_and_the_text_is_right(gpu_run: dict[str, Any]) -> None:
    e: LocalWhisperEngine = gpu_run["engine"]
    assert e.device == "cuda" and e.compute_used == "int8_float16" and "self-test passed" in e.device_used
    assert gpu_run["warm_ms"] is not None and e.warm_state == "up"
    errors = [wer(ref, r.text) for ref, r in zip(SENTENCES, gpu_run["rounds"][0], strict=True)]
    assert sum(errors) / len(errors) <= 0.15, [(r.text, w) for r, w in zip(gpu_run["rounds"][0], errors, strict=True)]
    print(f"\n{gpu_run['model']}: load {e.load_ms:.0f} ms, self-test {e.probe_ms:.0f} ms, warm-up {gpu_run['warm_ms']:.0f} ms, "
          f"decode ms {[round(r.decode_ms) for r in gpu_run['rounds'][1]]}, texts {[r.text for r in gpu_run['rounds'][1]]}")


def test_the_second_decode_is_fast(gpu_run: dict[str, Any]) -> None:
    for rnd in gpu_run["rounds"][1:]:
        assert all(0 < r.decode_ms < SECOND_DECODE_MAX_MS for r in rnd), [round(r.decode_ms) for r in rnd]


def test_the_warm_up_removes_the_first_decode_penalty(gpu_run: dict[str, Any]) -> None:
    first = gpu_run["rounds"][0][0].decode_ms
    median = statistics.median(r.decode_ms for rnd in gpu_run["rounds"][1:] for r in rnd)
    assert first <= 2 * median, (round(first), round(median))  # without the warm-up the first decode took 1 to 7 s


def test_a_decode_on_the_gpu_makes_no_network_connection(speech: list[bytes]) -> None:
    import socket

    seen: list[Any] = []
    real = socket.socket.connect

    def spy(self: Any, addr: Any) -> Any:
        seen.append(addr)
        return real(self, addr)

    socket.socket.connect = spy  # type: ignore[method-assign]
    try:
        e = LocalWhisperEngine(dataclasses.replace(load_config().stt, model="base.en", device="cuda"))
        r = asyncio.run(_decode(e, speech[1], "n"))
        e.close()
    finally:
        socket.socket.connect = real  # type: ignore[method-assign]
    assert r.text and [a for a in seen if a[0] not in ("127.0.0.1", "::1")] == []


def test_stt_check_in_a_child_process_prints_the_device_compute_type_and_timings(tmp_path: Path, speech: list[bytes]) -> None:
    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(speech[1])
    from brain.config import REPO_ROOT

    done = subprocess.run([sys.executable, "-m", "brain", "stt-check", str(p), "--model", "base.en", "--device", "cuda"],
                          capture_output=True, text=True, timeout=300, cwd=REPO_ROOT, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stdout + done.stderr
    out = done.stdout
    assert "device cuda int8_float16" in out and "model load" in out and "warm-up" in out and "decode" in out
    assert "time is it" in out.lower()
