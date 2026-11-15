"""Card P2-04: the REAL local models (base.en and small.en on the CPU). Deselected in every normal run.

    .\\.venv\\Scripts\\python -m pytest tests\\test_stt_real_model.py --run-real-model

They need the models fetched once (python tools/fetch_stt_model.py) and take about a minute. They use only local files
and make no network call (the suite's egress guard is loopback-only). Test speech is made by Windows' built-in SAPI
synthesiser in a temporary folder (no real person, nothing committed); without SAPI the speech tests skip.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import random
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from stt_helpers import chunks, tone
from wer import wer

from brain.config import REPO_ROOT, load_config
from brain.limits import UTTERANCE_MAX_BYTES
from brain.voice.modelstore import verify_model
from brain.voice.stt import LocalWhisperEngine, SttBadAudio

pytestmark = [pytest.mark.real_model, pytest.mark.trace("T-P2-04-006", "T-P2-04-007", "T-P2-04-013")]

SENTENCES = [
    "Please turn left and go to the kitchen.",
    "What time is it, Jeff?",
    "Take a picture of the front door and tell me what you see.",
    "There are twenty three apples and forty seven oranges.",
    "Stop right now.",
]
MODELS = ["base.en", "small.en"]
SAPI = """\
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$i = 0
foreach ($line in $args[1..($args.Count - 1)]) { $i++; $s.SetOutputToWaveFile((Join-Path $args[0] ("u$i.wav")), $fmt); $s.Speak($line); $s.SetOutputToNull() }
$s.Dispose()
"""


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def speech(tmp_path_factory: pytest.TempPathFactory) -> list[bytes]:
    if os.name != "nt":
        pytest.skip("the SAPI synthesiser is Windows only")
    folder = tmp_path_factory.mktemp("sapi")
    script = folder / "gen.ps1"
    script.write_text(SAPI, encoding="utf-8")
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), str(folder), *SENTENCES],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("PowerShell is not available")
    if done.returncode != 0:
        pytest.skip("the SAPI synthesiser is not available")
    import wave

    out = []
    for i in range(1, len(SENTENCES) + 1):
        with wave.open(str(folder / f"u{i}.wav")) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
            out.append(w.readframes(w.getnframes()))
    return out


def engine_for(model: str) -> LocalWhisperEngine:
    cfg = dataclasses.replace(load_config().stt, model=model)
    return LocalWhisperEngine(cfg)


async def transcribe(e: LocalWhisperEngine, pcm: bytes, uid: str = "u") -> Any:
    e.start(uid)
    for c in chunks(pcm):
        e.feed(c)
    return await e.finish()


@pytest.mark.parametrize("model", MODELS)
def test_the_installed_model_matches_its_lock(model: str) -> None:
    locked = verify_model(load_config().stt.model_dir, model)
    assert locked.revision and all(f.verified_from in ("hub", "first_seen") for f in locked.files.values())


@pytest.mark.parametrize("model", MODELS)
def test_speech_is_transcribed_with_a_low_word_error_rate_and_the_timings_are_recorded(model: str, speech: list[bytes]) -> None:
    e = engine_for(model)

    async def go() -> list[Any]:
        return [await transcribe(e, pcm, f"u{i}") for i, pcm in enumerate(speech)]

    first = run(go())
    errors = [wer(ref, r.text) for ref, r in zip(SENTENCES, first, strict=True)]
    assert sum(errors) / len(errors) <= 0.15, [(r.text, w) for r, w in zip(first, errors, strict=True)]
    assert all(r.decode_ms > 0 and r.rtf > 0 for r in first) and e.load_ms and e.device_used.startswith("cpu")
    warm = run(go())
    print(f"\n{model}: load {e.load_ms:.0f} ms; warm decode ms {[round(r.decode_ms) for r in warm]}; "
          f"rtf {[round(r.rtf, 2) for r in warm]}")
    assert all(r.total_ms < 20_000 for r in warm)
    e.close()


@pytest.mark.parametrize("model", MODELS)
def test_silence_and_noise_give_no_transcript(model: str) -> None:
    rng = random.Random(3)
    e = engine_for(model)
    noises = {"silence": bytes(64000),
              "white noise": struct.pack("<32000h", *[max(-32768, min(32767, int(rng.gauss(0, 3000)))) for _ in range(32000)]),
              "quiet hiss": struct.pack("<32000h", *[int(rng.gauss(0, 150)) for _ in range(32000)]),
              "a steady tone": tone(2.0, 220.0, 6000)}
    for name, pcm in noises.items():
        r = run(transcribe(e, pcm))
        assert r.text == "", (name, r.text)
    e.close()


@pytest.mark.parametrize("model", MODELS)
def test_15_seconds_is_decoded_and_one_sample_more_is_refused(model: str, speech: list[bytes]) -> None:
    e = engine_for(model)
    pcm = (b"".join(speech) * 4)[:UTTERANCE_MAX_BYTES]
    r = run(transcribe(e, pcm))
    assert r.duration_s == pytest.approx(15.0) and r.text and r.total_ms < 20_000
    e.start("over")
    for c in chunks(pcm):
        e.feed(c)
    with pytest.raises(SttBadAudio):
        e.feed(chunks(b"\x01\x00")[0])
    e.close()


def test_a_cancel_and_a_close_during_a_real_decode_do_not_hang(speech: list[bytes]) -> None:
    e = engine_for("small.en")

    async def go() -> float:
        e.start("c")
        for c in chunks(speech[0]):
            e.feed(c)
        task = asyncio.create_task(e.finish())
        await asyncio.sleep(0.05)
        t0 = time.perf_counter()
        e.cancel()
        try:
            await task
        except Exception as ex:  # noqa: BLE001  # either cancelled or (if it won the race) a normal result
            assert type(ex).__name__ == "SttCancelled"
        return time.perf_counter() - t0

    assert run(go()) < 2.0
    e.close()


def test_the_real_cpu_run_makes_no_network_connection(speech: list[bytes]) -> None:
    import socket

    seen: list[Any] = []
    real = socket.socket.connect

    def spy(self: Any, addr: Any) -> Any:
        seen.append(addr)
        return real(self, addr)

    socket.socket.connect = spy  # type: ignore[method-assign]
    try:
        e = engine_for("base.en")
        run(transcribe(e, speech[1]))
        e.close()
    finally:
        socket.socket.connect = real  # type: ignore[method-assign]
    assert [a for a in seen if a[0] not in ("127.0.0.1", "::1")] == []


def test_device_auto_falls_back_to_the_cpu_on_this_machine_or_uses_a_working_gpu(speech: list[bytes]) -> None:
    cfg = dataclasses.replace(load_config().stt, device="auto")
    e = LocalWhisperEngine(cfg)
    r = run(transcribe(e, speech[1]))
    assert r.text and ("cpu" in e.device_used or "cuda" in e.device_used)
    e.close()


def test_stt_check_in_a_child_process_prints_text_time_and_rtf(tmp_path: Path, speech: list[bytes]) -> None:
    import wave

    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(speech[1])
    done = subprocess.run([sys.executable, "-m", "brain", "stt-check", str(p), "--model", "base.en"], capture_output=True, text=True,
                          timeout=300, cwd=REPO_ROOT, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "real-time factor" in done.stdout and "decode" in done.stdout and "time is it" in done.stdout.lower()


def test_the_chaos_experiment_on_the_real_engine_passes_with_the_real_models() -> None:
    """P2-04.stt.real_engine in the chaos harness, allowed to read the real data folder, with the real models loaded."""
    env = {**os.environ, "EMBODIED_CHAOS_REAL_MODEL": "1"}
    done = subprocess.run([sys.executable, "-m", "brain", "chaos", "--only", "P2-04.stt.real_engine", "--allow-real-data",
                           "--no-results"], capture_output=True, text=True, timeout=600, cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL)
    assert "RESULT: PASS" in done.stdout or done.returncode == 0, done.stdout[-1500:] + done.stderr[-500:]
