"""Shared helpers for the speech-to-text tests: synthetic PCM, a synthetic model folder with a lock, a fake Whisper model.

Nothing here needs the real models or ctranslate2. Audio is generated in code (a tone is loud enough to pass the
engine's digital-silence check); no recording of a person is used anywhere in the default suite.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from brain.body.types import AudioChunk
from brain.limits import AUDIO_IN_RATE_HZ
from brain.voice import modelstore as MS

NOMINAL_CHUNK_BYTES = 2560  # 80 ms of 16 kHz mono s16le


def tone(seconds: float, freq: float = 440.0, amp: int = 8000) -> bytes:
    n = int(seconds * AUDIO_IN_RATE_HZ)
    return struct.pack(f"<{n}h", *[int(amp * math.sin(2 * math.pi * freq * i / AUDIO_IN_RATE_HZ)) for i in range(n)])


def silence(seconds: float) -> bytes:
    return bytes(int(seconds * AUDIO_IN_RATE_HZ) * 2)


def chunks(pcm: bytes, size: int = NOMINAL_CHUNK_BYTES, rate: int = AUDIO_IN_RATE_HZ, channels: int = 1) -> list[AudioChunk]:
    return [AudioChunk(0, i // size, rate, channels, pcm[i:i + size]) for i in range(0, len(pcm), size)]


def feed_all(engine: Any, uid: str, pcm: bytes, size: int = NOMINAL_CHUNK_BYTES) -> None:
    engine.start(uid)
    for c in chunks(pcm, size):
        engine.feed(c)


# ---- a synthetic model folder ------------------------------------------------------------------------------------------

GOOD_FILES: dict[str, bytes] = {
    "config.json": b'{"alignment_heads": []}',
    "model.bin": b"\x06\x00\x00\x00" + b"\x01" * 4096,  # starts like a CTranslate2 binary version number
    "tokenizer.json": b'{"version": "1.0"}',
    "vocabulary.txt": b"hello\nworld\n",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_model(root: Path, name: str = "base.en", files: dict[str, bytes] | None = None, *, lock: bool = True) -> Path:
    """root/<name>/ with the files, and root/lock.json describing them (verified_from 'first_seen')."""
    files = dict(GOOD_FILES if files is None else files)
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    for fname, data in files.items():
        (folder / fname).write_bytes(data)
    if lock:
        locked = {f: MS.LockedFile(len(d), sha(d), "first_seen", None) for f, d in files.items()}
        existing = MS.read_lock(root) if (root / MS.LOCK_NAME).exists() else {}
        existing[name] = MS.LockedModel(name, MS.MODELS.get(name, "Systran/faster-whisper-base.en"), "a" * 40,
                                        "2026-01-01T00:00:00Z", locked)
        MS.write_lock(root, existing)
    return folder


# ---- a fake faster-whisper model --------------------------------------------------------------------------------------


def seg(text: str, *, avg_logprob: float = -0.2, no_speech_prob: float = 0.01, compression_ratio: float = 1.2) -> Any:
    return SimpleNamespace(text=text, avg_logprob=avg_logprob, no_speech_prob=no_speech_prob,
                           compression_ratio=compression_ratio)


class FakeWhisperModel:
    """Stands in for faster_whisper.WhisperModel: transcribe(audio, **options) -> (segment iterator, info).
    `segments` may be a list or a callable(audio) -> list; `delay_s` makes the decode slow (it waits on `release`);
    `error` is raised from transcribe; `crash_in_iteration` raises while the segments are consumed."""

    def __init__(self, segments: Any = None, *, delay_s: float = 0.0, error: BaseException | None = None,
                 crash_in_iteration: BaseException | None = None) -> None:
        self.segments = [seg("hello world")] if segments is None else segments
        self.delay_s, self.error, self.crash = delay_s, error, crash_in_iteration
        self.calls: list[dict[str, Any]] = []
        self.release = threading.Event()
        self.started = threading.Event()
        self.audio_lengths: list[int] = []

    def transcribe(self, audio: Any, **options: Any) -> tuple[Any, Any]:
        self.calls.append(options)
        self.audio_lengths.append(len(audio))
        self.started.set()
        if self.error is not None:
            raise self.error
        segs = self.segments(audio) if callable(self.segments) else list(self.segments)

        def gen() -> Any:
            if self.delay_s:
                self.release.wait(self.delay_s)
            if self.crash is not None:
                raise self.crash
            for s in segs:
                yield s

        return gen(), SimpleNamespace(language="en")


def factory_for(model: FakeWhisperModel | None = None, *, fail: BaseException | None = None) -> Any:
    made: list[tuple[Any, ...]] = []

    def factory(folder: Path, device: str, compute_type: str, cpu_threads: int) -> Any:
        made.append((folder, device, compute_type, cpu_threads))
        if fail is not None:
            raise fail
        return model if model is not None else FakeWhisperModel()

    factory.made = made  # type: ignore[attr-defined]
    return factory


def wait_until(predicate: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def symlink_or_skip(link: Path, target: Path) -> None:
    import pytest

    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symbolic links")
