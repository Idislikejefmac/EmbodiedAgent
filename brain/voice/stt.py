"""Speech to text, local (card P2-04, STT half; docs/stt.md): the `SttEngine` interface, its audio gate and transcript
gate, and the faster-whisper engine.

    engine = LocalWhisperEngine(config.stt)           # or a FakeSttEngine in tests (tests/fakes/stt.py)
    engine.start("utt-1")                            # one utterance at a time; the utterance is the unit
    for chunk in chunks:  engine.feed(chunk)          # AudioChunk: 16 kHz mono s16le, at most 15 s in all
    result = await engine.finish()                    # SttResult: cleaned text and timings
    engine.cancel()                                   # drop the open utterance and any decode still waiting
    engine.close()

Inputs and limits (brain/limits.py A.6): a chunk must be an AudioChunk of 16 kHz, one channel, pcm_s16le, at most
AUDIO_IN_MAX_CHUNK_BYTES; at most STT_CHUNKS_MAX chunks and UTTERANCE_MAX_BYTES (15 s) per utterance; anything else
raises SttBadAudio and changes nothing. An utterance shorter than UTTERANCE_MIN_MS, or digital silence, gives a clean
empty result without touching the model. Transcripts are cleaned (NFKC, no control or invisible characters, one
line, at most STT_MAX_CHARS) and Whisper's silence hallucinations are dropped (see `filter_transcript`).

The decode runs in ONE daemon worker thread, never on the event loop. At most STT_PENDING_MAX decodes are admitted (one
running, one waiting); the next raises SttBusy. A decode that outlives STT_DECODE_TIMEOUT_S, or is cancelled, makes
`finish()` return at once with SttTimeout or SttCancelled; the native decode cannot be interrupted, so the worker is
told to stop at the next segment and, until it does, new requests see SttBusy (honest, bounded; never a pile-up).

The model is loaded on first use, after `verify_model` (brain/voice/modelstore.py) has checked every file against
data/models/stt/lock.json; an unlisted or changed model raises SttUnavailable. The engine never uses the network:
the model is opened from a folder, HF_HUB_OFFLINE is set, and the egress guard would refuse a socket anyway.

The GPU (docs/stt.md): `device = "auto"` uses CUDA only if a one-time self-test in a CHILD process passes (CUDA DLLs from the
pip wheels are registered first: `register_cuda_dlls`), else the CPU with a health reason; `"cuda"` insists and reports
SttUnavailable when the GPU cannot be used. On the GPU the first use runs a warm-up decode (`warm()`) so the first real
utterance does not pay the CUDA start-up. A GPU out-of-memory or CUDA error during a decode raises SttGpuOutOfMemory or
SttGpuError for that decode only and switches the engine to the CPU, once, with an event and a health reason.

Events: `speech.stt` (utterance id, ms, rtf, chars, ok, code, device), NEVER the words; with a `phase` field (device, warming,
warm, fallback) it reports the GPU life cycle. The words go to the prompt capture
only when [logging] full_prompts is on (the caller does that).
"""

from __future__ import annotations

import abc
import array
import asyncio
import concurrent.futures
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NewType

from .. import obs
from ..body.types import IDENTIFIER, AudioChunk
from ..config import SttConfig
from ..limits import (AUDIO_IN_MAX_CHUNK_BYTES, AUDIO_IN_RATE_HZ, STT_BEAM_SIZE, STT_CHUNKS_MAX, STT_COMPRESSION_MAX,
                      STT_CUDA_PROBE_TIMEOUT_S, STT_DECODE_TIMEOUT_S, STT_GPU_REASON_MAX_CHARS, STT_LOAD_TIMEOUT_S,
                      STT_LOGPROB_FLOOR, STT_MAX_CHARS, STT_NO_SPEECH_MAX, STT_PENDING_MAX, STT_PHANTOM_LOGPROB_FLOOR,
                      STT_PHANTOM_NO_SPEECH, STT_REPEAT_MAX, STT_SILENCE_RMS, STT_UNAVAILABLE_S, STT_WARM_AUDIO_S,
                      STT_WARM_LEVEL, STT_WARM_TIMEOUT_S, UTTERANCE_MAX_BYTES, UTTERANCE_MAX_S, UTTERANCE_MIN_MS)
from .modelstore import ModelStoreError, verify_model

Pcm16 = NewType("Pcm16", bytes)  # little-endian signed 16-bit mono samples, 16 kHz (the engine's own buffer)
VENDOR = "whisper-local"  # the `vendor` field of speech.stt events


# ---- errors --------------------------------------------------------------------------------------------------------


class SttError(RuntimeError):
    """Base of every error this module raises on purpose. `str(e)` is short and safe to show or log."""


class SttBadAudio(SttError, ValueError):
    """Audio (or an utterance id) the gate refuses. Nothing was changed."""


class SttBusy(SttError):
    """An utterance is already open, or the decode queue is full. Try again after the running decode ends."""


class SttUnavailable(SttError):
    """The engine cannot work (model missing, changed or not loadable, engine closed). The message says why."""


class SttTimeout(SttError):
    """A decode took longer than its hard limit; its result is discarded."""


class SttCancelled(SttError):
    """The utterance was cancelled before its transcript was ready."""


class SttSequenceError(SttError):
    """feed or finish without start, or start while another utterance is open."""


class SttGpuError(SttError):
    """A CUDA error or driver problem while decoding on the GPU. That decode failed; the engine switched to the CPU."""


class SttGpuOutOfMemory(SttGpuError):
    """The GPU ran out of memory while decoding. That decode failed; the engine switched to the CPU."""


# ---- result --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SttResult:
    """The final transcript of one utterance. `text` is already cleaned and may be empty (silence, a click, or
    something filtered as a hallucination: `empty_reason` says which). Times are wall-clock milliseconds."""

    utterance_id: str
    text: str
    language: str
    duration_s: float  # audio length
    decode_ms: float  # time inside the model (0 when it was not run)
    queue_ms: float  # time between finish() and the decode starting (waiting behind another decode, model load)
    total_ms: float  # finish() called to result returned
    rtf: float  # decode_ms / audio ms: below 1 is faster than real time
    avg_logprob: float | None
    no_speech_prob: float | None
    segments: int
    truncated: bool = False  # the transcript was cut at STT_MAX_CHARS
    empty_reason: str = ""  # "", "too_short", "silence", "filtered"

    @property
    def chars(self) -> int:
        return len(self.text)


# ---- the transcript gate -------------------------------------------------------------------------------------------

_BAD_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_SPACE_RUN = re.compile(r"\s+")
_TAGS = re.compile(r"\[[^\]]{0,40}\]|\([^)]{0,40}\)|\*[^*]{0,40}\*|[\u266a\u266b\u266c]+")  # [MUSIC] (laughs) *clap* notes
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
# Whisper's phantom phrases on silence or noise (learned from subtitles). Always dropped:
_ALWAYS_PHANTOM = frozenset({"thanks for watching", "thank you for watching", "thank you so much for watching",
                             "please subscribe", "like and subscribe", "subtitles by the amaraorg community",
                             "subtitles by the amara org community", "see you in the next video",
                             "ill see you in the next video", "see you next time", "transcribed by"})
# Dropped when alone AND the model was not confident (a clearly spoken "thank you" passes):
_WEAK_PHANTOM = frozenset({"thank you", "thanks", "you", "bye", "bye bye", "thank you very much", "okay", "so",
                           "oh", "uh", "um", "hmm", "mm", "the end", "yeah"})


def clean_transcript(raw: str) -> tuple[str, bool]:
    """Make model text safe to hand on: NFKC, no control, format (zero-width, bidi), surrogate or unassigned
    characters, line breaks and tabs become single spaces, ends trimmed, at most STT_MAX_CHARS. Returns (text,
    truncated). Anything that is not a string gives ("", False)."""
    if not isinstance(raw, str):
        return "", False
    text = unicodedata.normalize("NFKC", raw[: STT_MAX_CHARS * 8])  # bound the work before the real cut
    text = "".join(" " if c in "\u2028\u2029" or c.isspace() else ("" if unicodedata.category(c) in _BAD_CATEGORIES else c)
                   for c in text)
    text = _SPACE_RUN.sub(" ", text).strip()
    if len(text) > STT_MAX_CHARS:
        return text[:STT_MAX_CHARS].rstrip(), True
    return text, False


def _normal(text: str) -> str:
    return _SPACE_RUN.sub(" ", _PUNCT.sub("", text.lower())).strip()


def collapse_repeats(text: str, limit: int = STT_REPEAT_MAX) -> str:
    """Cut a loop (the same 1 to 4 word phrase `limit` or more times in a row) down to two copies."""
    words = text.split(" ")
    if len(words) < limit:
        return text
    keys = [_normal(w) for w in words]
    for n in (1, 2, 3, 4):
        i, out_w, out_k = 0, [], []
        while i < len(words):
            run = 1
            while i + (run + 1) * n <= len(words) and keys[i + run * n:i + (run + 1) * n] == keys[i:i + n]:
                run += 1
            if run >= limit and any(keys[i:i + n]):
                out_w += words[i:i + 2 * n]
                out_k += keys[i:i + 2 * n]
                i += run * n
            else:
                out_w.append(words[i])
                out_k.append(keys[i])
                i += 1
        words, keys = out_w, out_k
    return " ".join(words)


def filter_transcript(raw: str, *, avg_logprob: float | None = None, no_speech_prob: float | None = None) -> tuple[str, bool, str]:
    """The whole transcript gate: (clean text, truncated, empty_reason). Removes sound tags and music notes, cuts
    repetition loops, and drops a phantom phrase (see _ALWAYS_PHANTOM and _WEAK_PHANTOM). A non-finite confidence
    counts as 'not confident'."""
    text, _ = clean_transcript(raw)
    text = clean_transcript(_TAGS.sub(" ", text))[0]
    text = collapse_repeats(text)
    key = _normal(text)
    if not key:
        return "", False, "filtered" if text or raw else ""
    weak = (avg_logprob is None or not math.isfinite(avg_logprob) or avg_logprob < -STT_PHANTOM_LOGPROB_FLOOR
            or no_speech_prob is None or not math.isfinite(no_speech_prob) or no_speech_prob > STT_PHANTOM_NO_SPEECH)
    if key in _ALWAYS_PHANTOM or (weak and key in _WEAK_PHANTOM):
        return "", False, "filtered"
    text, truncated = clean_transcript(text)
    return text, truncated, ""


def log_form(text: str | None, limit: int = 80) -> str:
    """A transcript as it may appear in a log or an error: quoted with repr (escapes anything odd), cut to `limit`."""
    s = text if isinstance(text, str) else type(text).__name__
    return repr(s[:limit]) + ("..." if len(s) > limit else "")


# ---- the audio gate ------------------------------------------------------------------------------------------------


def check_chunk(chunk: AudioChunk) -> bytes:
    """The PCM bytes of one chunk, or SttBadAudio. 16 kHz, mono, pcm_s16le, whole samples, at most
    AUDIO_IN_MAX_CHUNK_BYTES. (Empty chunks are allowed and carry no audio; they still count toward STT_CHUNKS_MAX.)"""
    if not isinstance(chunk, AudioChunk):
        raise SttBadAudio(f"a chunk must be an AudioChunk, not {type(chunk).__name__}")
    data = chunk.data
    if (not isinstance(chunk.sample_rate_hz, int) or isinstance(chunk.sample_rate_hz, bool)
            or chunk.sample_rate_hz != AUDIO_IN_RATE_HZ):
        raise SttBadAudio(f"audio must be {AUDIO_IN_RATE_HZ} Hz, not {chunk.sample_rate_hz!r:.20}")
    if chunk.channels != 1:
        raise SttBadAudio(f"audio must be mono, not {chunk.channels!r:.10} channels")
    if chunk.encoding != "pcm_s16le":
        raise SttBadAudio("audio must be pcm_s16le")
    if not isinstance(data, (bytes, bytearray)):
        raise SttBadAudio(f"chunk data must be bytes, not {type(data).__name__}")
    if len(data) % 2:
        raise SttBadAudio("chunk data must be whole 16-bit samples (an odd number of bytes)")
    if len(data) > AUDIO_IN_MAX_CHUNK_BYTES:
        raise SttBadAudio(f"a chunk is at most {AUDIO_IN_MAX_CHUNK_BYTES} bytes, not {len(data)}")
    return bytes(data)


def rms_s16le(pcm: Pcm16) -> float:
    """Root-mean-square level of little-endian 16-bit samples (0 for none). Pure Python, about 15 ms for 15 s."""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: n * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return math.sqrt(sum(s * s for s in samples) / n)


# ---- the interface -------------------------------------------------------------------------------------------------

PartialCallback = Callable[[str, str], None]  # (utterance_id, partial text); called from the thread that calls feed()


class SttEngine(abc.ABC):
    """What the voice pipeline needs from any speech-to-text engine (local, fake, or a later vendor).

    Call order per utterance: start, feed (any number of times), then finish or cancel. One utterance is open at a
    time; a second `start` raises SttSequenceError. After `finish` returns or raises the engine is ready for the next.
    `finish` is a coroutine; the other methods are quick and never block."""

    @abc.abstractmethod
    def start(self, utterance_id: str, *, on_partial: PartialCallback | None = None) -> None: ...

    @abc.abstractmethod
    def feed(self, chunk: AudioChunk) -> None: ...

    @abc.abstractmethod
    async def finish(self) -> SttResult: ...

    @abc.abstractmethod
    def cancel(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...


class BufferedSttEngine(SttEngine):
    """The gate and the state machine shared by every engine that decodes a whole utterance at once. A subclass
    implements `_transcribe(utterance_id, pcm)`; the gate has already checked the audio."""

    def __init__(self) -> None:
        self._uid: str | None = None
        self._buf = bytearray()
        self._chunks = 0
        self._on_partial: PartialCallback | None = None
        self._closed = False
        self._generation = 0  # bumped by cancel(): a finish that started before it is cancelled

    # -- the state machine

    def start(self, utterance_id: str, *, on_partial: PartialCallback | None = None) -> None:
        self._open_check()
        if not isinstance(utterance_id, str) or not IDENTIFIER.match(utterance_id):
            raise SttBadAudio("utterance_id must be 1 to 64 letters, digits, '_', '-' or '.'")
        if self._uid is not None:
            raise SttSequenceError("an utterance is already open: finish or cancel it first")
        self._uid, self._buf, self._chunks, self._on_partial = utterance_id, bytearray(), 0, on_partial

    def feed(self, chunk: AudioChunk) -> None:
        self._open_check()
        if self._uid is None:
            raise SttSequenceError("feed without start")
        data = check_chunk(chunk)
        if self._chunks >= STT_CHUNKS_MAX:
            raise SttBadAudio(f"more than {STT_CHUNKS_MAX} chunks in one utterance")
        if len(self._buf) + len(data) > UTTERANCE_MAX_BYTES:
            raise SttBadAudio(f"the utterance is longer than {UTTERANCE_MAX_S} s")
        self._chunks += 1
        self._buf += data
        self._after_feed(data)

    def _after_feed(self, data: bytes) -> None:
        """Hook for engines that make partial text while audio arrives (none yet)."""

    def cancel(self) -> None:
        self._generation += 1
        self._uid, self._buf, self._chunks, self._on_partial = None, bytearray(), 0, None
        self._cancel_running()

    def _cancel_running(self) -> None:
        """Hook: tell a decode already admitted to stop."""

    def close(self) -> None:
        self._closed = True
        self.cancel()

    def _open_check(self) -> None:
        if self._closed:
            raise SttUnavailable("the engine is closed")

    async def finish(self) -> SttResult:
        t0 = time.perf_counter()
        self._open_check()
        uid = self._uid
        if uid is None:
            raise SttSequenceError("finish without start")
        pcm, generation = bytes(self._buf), self._generation
        self._uid, self._buf, self._chunks, self._on_partial = None, bytearray(), 0, None  # ready for the next start
        duration = len(pcm) / 2 / AUDIO_IN_RATE_HZ
        if duration * 1000 < UTTERANCE_MIN_MS:
            return self._empty(uid, duration, "too_short", t0)
        if rms_s16le(Pcm16(pcm)) < STT_SILENCE_RMS:
            return self._empty(uid, duration, "silence", t0)
        try:
            result = await self._transcribe(uid, pcm, generation)
        except SttError as e:
            self._emit(uid, ok=False, code=type(e).__name__, duration=duration, ms=(time.perf_counter() - t0) * 1000)
            raise
        self._emit(uid, ok=True, code="ok", duration=duration, ms=result.total_ms, rtf=result.rtf, chars=result.chars,
                   reason=result.empty_reason)
        return result

    @abc.abstractmethod
    async def _transcribe(self, utterance_id: str, pcm: bytes, generation: int) -> SttResult: ...

    # -- helpers

    def _empty(self, uid: str, duration: float, reason: str, t0: float) -> SttResult:
        total = (time.perf_counter() - t0) * 1000
        self._emit(uid, ok=True, code="ok", duration=duration, ms=total, rtf=0.0, chars=0, reason=reason)
        return SttResult(uid, "", "en", duration, 0.0, 0.0, total, 0.0, None, None, 0, False, reason)

    def _emit(self, uid: str, *, ok: bool, code: str, duration: float, ms: float, rtf: float = 0.0, chars: int = 0,
              reason: str = "") -> None:
        obs.emit("speech.stt", "an utterance was transcribed" if ok else "an utterance could not be transcribed",
                 level=None if ok else "WARNING", vendor=self.vendor, utterance_id=uid, ok=ok, code=code,
                 ms=round(ms, 1), rtf=round(rtf, 3), chars=chars, audio_s=round(duration, 2), empty=reason or None,
                 **self._event_extras())

    def _event_extras(self) -> dict[str, Any]:
        """Hook: extra event fields (the local engine adds the device it runs on)."""
        return {}

    vendor = VENDOR


# ---- the local engine ----------------------------------------------------------------------------------------------

ModelFactory = Callable[[Path, str, str, int], Any]  # (folder, device, compute_type, cpu_threads) -> object with transcribe()
_CUDA_RESULT: dict[tuple[str, str], tuple[bool, str]] = {}  # the one-time self-test, per (model folder, compute type)
_CUDA_LOCK = threading.Lock()
_DLL_LOCK = threading.Lock()
_DLL_DIRS: list[str] | None = None  # the CUDA DLL folders registered by register_cuda_dlls (None: not done yet)

_CUDA_SELFTEST = (
    "import os, sys\n"
    "dirs = [d for d in sys.argv[3].split(os.pathsep) if d and os.path.isdir(d)]\n"
    "for d in dirs:\n"
    "    if hasattr(os, 'add_dll_directory'):\n"
    "        os.add_dll_directory(d)\n"
    "os.environ['PATH'] = os.pathsep.join(dirs + [os.environ.get('PATH', '')])\n"
    "try:\n"
    "    import numpy\n"
    "    from faster_whisper import WhisperModel\n"
    "    m = WhisperModel(sys.argv[1], device='cuda', compute_type=sys.argv[2], local_files_only=True)\n"
    "    audio = (numpy.random.default_rng(1).standard_normal(16000) * 0.01).astype('float32')\n"
    "    segs, _ = m.transcribe(audio, language='en', beam_size=1, temperature=0.0, vad_filter=False)\n"
    "    list(segs)\n"
    "except Exception as e:\n"
    "    print('CUDA_SELFTEST_FAIL ' + type(e).__name__ + ': ' + str(e)[:200])\n"
    "    sys.exit(1)\n"
    "print('CUDA_SELFTEST_OK')\n"
)


def _site_packages() -> list[Path]:
    """This interpreter's own site-packages folders (never a user-supplied path)."""
    import sysconfig

    seen: list[Path] = []
    for key in ("purelib", "platlib"):
        p = sysconfig.get_paths().get(key)
        if p and Path(p) not in seen:
            seen.append(Path(p))
    return seen


def cuda_dll_dirs(site_dirs: list[Path] | None = None) -> list[str]:
    """The folders `<site-packages>/nvidia/*/bin` that exist (where the pip wheels nvidia-cublas-cu12, nvidia-cudnn-cu12
    and nvidia-cuda-nvrtc-cu12 put their DLLs), each really inside its site-packages folder (no link leads out)."""
    out: list[str] = []
    for site in (_site_packages() if site_dirs is None else site_dirs):
        try:
            if not (site / "nvidia").is_dir() or (site / "nvidia").is_symlink():
                continue
            root = (site / "nvidia").resolve()
            for sub in sorted(root.iterdir()):
                b = (sub / "bin").resolve()
                if sub.is_dir() and not sub.is_symlink() and b.is_dir() and root in b.parents and str(b) not in out:
                    out.append(str(b))
        except OSError:  # swallowed: an unreadable folder just means there are no CUDA wheels there
            continue
    return out


def register_cuda_dlls() -> list[str]:
    """Windows only, once per process: make the pip-installed CUDA 12 DLLs findable BEFORE ctranslate2 loads CUDA, with
    `os.add_dll_directory` for each folder and by putting them at the front of PATH. Returns the folders registered
    (empty on other systems or when the wheels are not installed). Idempotent; never raises."""
    global _DLL_DIRS
    with _DLL_LOCK:
        if _DLL_DIRS is not None:
            return list(_DLL_DIRS)
        added: list[str] = []
        if sys.platform == "win32":
            try:
                for d in cuda_dll_dirs():
                    try:
                        os.add_dll_directory(d)
                    except OSError:  # swallowed: this folder cannot be added; CUDA will report what is missing
                        continue
                    added.append(d)
                if added:
                    rest = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p not in added]
                    os.environ["PATH"] = os.pathsep.join(added + rest)
            except Exception:  # broad-except: a path or OS oddity must mean "no GPU libraries", never a crash
                added = []
        _DLL_DIRS = added
        return list(added)


def resolve_compute_type(device: str, requested: str) -> tuple[str, str]:
    """(compute type to use, short note) for a RESOLVED device ("cpu" or "cuda"). On the GPU the CPU default `int8` becomes
    `int8_float16` (the measured fast setting: docs/stt.md); on the CPU a float16 type is never used (int8 instead)."""
    if device == "cuda":
        if requested in ("int8_float16", "float16", "float32"):
            return requested, ""
        return "int8_float16", "int8 became int8_float16 on the GPU"
    if requested in ("int8_float16", "float16"):
        return "int8", f"{requested} needs a GPU; using int8"
    return (requested, "") if requested in ("int8", "float32") else ("int8", "")


def _default_factory(folder: Path, device: str, compute_type: str, cpu_threads: int) -> Any:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # belt and braces: the hub library must never try the network
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    if device == "cuda":
        register_cuda_dlls()
    from faster_whisper import WhisperModel  # lazy: importing ctranslate2 takes about a second

    return WhisperModel(str(folder), device=device, compute_type=compute_type, cpu_threads=cpu_threads, num_workers=1,
                        local_files_only=True)


def _short(text: str, limit: int = STT_GPU_REASON_MAX_CHARS) -> str:
    return clean_transcript(text)[0][:limit]


def cuda_usable(folder: Path, compute_type: str) -> tuple[bool, str]:
    """`device = "auto"` or "cuda": is CUDA usable? (True, why) or (False, short reason). One-time per (folder, compute
    type) per process. A cheap check first (no CUDA device visible to CTranslate2: no). Only if a device shows up, a real
    load and a one-second transcribe run in a CHILD process with a timeout (STT_CUDA_PROBE_TIMEOUT_S), so a driver or
    library problem, even a native crash, can never take the brain down."""
    ct, _ = resolve_compute_type("cuda", compute_type)
    key = (str(folder), ct)
    with _CUDA_LOCK:
        if key in _CUDA_RESULT:
            return _CUDA_RESULT[key]
        dirs = register_cuda_dlls()
        try:
            import ctranslate2

            count = int(ctranslate2.get_cuda_device_count())
        except Exception as e:  # broad-except: a driver problem inside the native library must mean "use the CPU", never a crash
            return _CUDA_RESULT.setdefault(key, (False, f"CUDA could not be queried ({type(e).__name__})"))
        if count <= 0:
            return _CUDA_RESULT.setdefault(key, (False, "no CUDA device is usable by CTranslate2 (no NVIDIA GPU, or a driver "
                                                        "older than 525, or the CUDA libraries are missing)"))
        try:
            done = subprocess.run([sys.executable, "-I", "-c", _CUDA_SELFTEST, str(folder), ct, os.pathsep.join(dirs)],
                                  capture_output=True, text=True, timeout=STT_CUDA_PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL,
                                  env={**os.environ, "HF_HUB_OFFLINE": "1"})
            if done.returncode == 0 and "CUDA_SELFTEST_OK" in done.stdout:
                result = (True, "CUDA self-test passed")
            else:
                said = next((ln for ln in done.stdout.splitlines() if ln.startswith("CUDA_SELFTEST_FAIL")), "")
                detail = f": {_short(said[len('CUDA_SELFTEST_FAIL'):].strip())}" if said else f" (exit code {done.returncode})"
                result = (False, f"the CUDA self-test failed{detail}")
        except subprocess.TimeoutExpired:
            result = (False, f"the CUDA self-test timed out after {STT_CUDA_PROBE_TIMEOUT_S:g} s")
        except (OSError, subprocess.SubprocessError) as e:
            result = (False, f"the CUDA self-test could not run ({type(e).__name__})")
        return _CUDA_RESULT.setdefault(key, result)


_GPU_WORDS = ("cuda", "cublas", "cudnn", "cudart", "nvrtc", "gpu", "driver", "illegal memory", "device-side", "device lost",
              "launch timed out", "no kernel image")


def _gpu_error_class(e: BaseException) -> type[SttGpuError] | None:
    """The named GPU failure for an exception raised by a decode on the GPU, or None when it does not look like one."""
    if isinstance(e, MemoryError):
        return SttGpuOutOfMemory
    text = str(e).lower()
    if "out of memory" in text or "failed to allocate" in text or "cudamalloc" in text:
        return SttGpuOutOfMemory
    if any(w in text for w in _GPU_WORDS):
        return SttGpuError
    return None


@dataclass(eq=False)
class _Job:
    uid: str
    pcm: bytes
    submitted: float
    future: concurrent.futures.Future[SttResult]
    stop: threading.Event


class LocalWhisperEngine(BufferedSttEngine):
    """faster-whisper on this machine. See the module docstring for the guarantees."""

    def __init__(self, config: SttConfig, *, factory: ModelFactory | None = None,
                 decode_timeout_s: float = STT_DECODE_TIMEOUT_S, load_timeout_s: float = STT_LOAD_TIMEOUT_S,
                 pending_max: int = STT_PENDING_MAX, vad_filter: bool = True,
                 warm_timeout_s: float = STT_WARM_TIMEOUT_S) -> None:
        super().__init__()
        self.config = config
        self._factory = factory or _default_factory
        self._decode_timeout_s, self._load_timeout_s = decode_timeout_s, load_timeout_s
        self._warm_timeout_s = warm_timeout_s
        self._vad = vad_filter
        self._q: queue.Queue[_Job | None] = queue.Queue()
        self._pending = 0
        self._pending_max = pending_max
        self._lock = threading.Lock()
        self._model_lock = threading.RLock()  # load, warm-up and the fall back to the CPU happen one at a time
        self._worker: threading.Thread | None = None
        self._model: Any = None
        self._device_used = ""
        self._device = ""  # "cpu" or "cuda" once a model is loaded
        self._compute = ""
        self._note = ""  # a short health note: what is running and why
        self._force_cpu = False  # set once, after a GPU failure: every later load is on the CPU
        self._gpu_fallback = False
        self._warmed = False
        self._failed: tuple[float, str] | None = None  # (monotonic time of the failure, reason)
        self._jobs: set[_Job] = set()
        self.load_ms: float | None = None
        self.probe_ms: float = 0.0  # the GPU self-test (0 when cached or on the CPU)
        self.warm_ms: float | None = None
        self.warm_state = "cold"  # cold, warming, up, failed

    @property
    def device_used(self) -> str:
        return self._device_used or "not loaded yet"

    @property
    def device(self) -> str:
        return self._device or "not loaded yet"

    @property
    def compute_used(self) -> str:
        return self._compute or "not loaded yet"

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _event_extras(self) -> dict[str, Any]:
        return {"device": self._device or None, "note": self._note or None, "gpu_fallback": True if self._gpu_fallback else None}

    def _phase(self, phase: str, msg: str, *, ok: bool = True, level: str | None = None, **fields: Any) -> None:
        obs.emit("speech.stt", msg, level=level, vendor=self.vendor, phase=phase, ok=ok, **self._event_extras(), **fields)

    # -- the async side

    async def _transcribe(self, utterance_id: str, pcm: bytes, generation: int) -> SttResult:
        if self._failed is not None:
            if time.monotonic() - self._failed[0] < STT_UNAVAILABLE_S:
                raise SttUnavailable(self._failed[1])
            self._failed = None
        job = self._admit(utterance_id, pcm)
        limit = self._decode_timeout_s
        if self._model is None:  # first use: the file check and load, and (GPU) the self-test and warm-up
            limit += self._load_timeout_s
            if self.config.device != "cpu" and not self._force_cpu:
                limit += STT_CUDA_PROBE_TIMEOUT_S + self._warm_timeout_s
        try:
            return await asyncio.wait_for(asyncio.wrap_future(job.future), limit)
        except TimeoutError:
            job.stop.set()
            raise SttTimeout(f"the decode did not finish within {limit:g} s") from None
        except asyncio.CancelledError:
            job.stop.set()
            raise
        finally:
            if generation != self._generation and not job.future.done():
                job.stop.set()

    async def warm_async(self) -> float | None:
        """`warm()` in a helper thread: the event loop keeps running. Never raises."""
        return await asyncio.to_thread(self.warm)

    def _admit(self, uid: str, pcm: bytes) -> _Job:
        with self._lock:
            if self._pending >= self._pending_max:
                raise SttBusy("the speech decoder is busy; try again when the running decode ends")
            self._pending += 1
            job = _Job(uid, pcm, time.perf_counter(), concurrent.futures.Future(), threading.Event())
            self._jobs.add(job)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name="stt-worker", daemon=True)
                self._worker.start()
        self._q.put(job)
        return job

    def _cancel_running(self) -> None:
        with self._lock:
            for job in list(self._jobs):
                job.stop.set()
                if not job.future.done():
                    try:
                        job.future.set_exception(SttCancelled("the utterance was cancelled"))
                    except concurrent.futures.InvalidStateError:  # swallowed: the decode finished at that moment
                        pass

    def close(self) -> None:
        super().close()
        self._q.put(None)
        self._model = None

    # -- the worker thread

    def _work(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                return
            try:
                result = self._run(job)
                if not job.future.done():
                    job.future.set_result(result)
            except SttError as e:
                self._set_exception(job, e)
            except Exception as e:  # broad-except: the worker thread must survive any engine or library failure
                self._set_exception(job, SttUnavailable(f"the speech decoder failed ({type(e).__name__})"))
            finally:
                with self._lock:
                    self._pending -= 1
                    self._jobs.discard(job)

    @staticmethod
    def _set_exception(job: _Job, error: SttError) -> None:
        try:
            if not job.future.done():
                job.future.set_exception(error)
        except concurrent.futures.InvalidStateError:  # swallowed: the waiting side timed out or cancelled first
            pass

    def _run(self, job: _Job) -> SttResult:
        if job.stop.is_set():
            raise SttCancelled("the utterance was cancelled before it started")
        model = self._ensure_model()
        decode_start = time.perf_counter()
        queue_ms = (decode_start - job.submitted) * 1000
        text_parts: list[str] = []
        logprobs: list[float] = []
        no_speech: list[float] = []
        seen = 0
        audio = self._to_float(job.pcm)
        try:
            segments, info = model.transcribe(
                audio, language="en", task="transcribe", beam_size=STT_BEAM_SIZE, temperature=0.0,
                condition_on_previous_text=False, vad_filter=self._vad, without_timestamps=True,
                no_speech_threshold=STT_NO_SPEECH_MAX, log_prob_threshold=-STT_LOGPROB_FLOOR,
                compression_ratio_threshold=STT_COMPRESSION_MAX, word_timestamps=False)
            for seg in segments:  # the decode is lazy: it runs one 30 s window at a time as this loop asks
                if job.stop.is_set():
                    raise SttCancelled("the utterance was cancelled while it was decoded")
                seen += 1
                nsp, alp = _finite(getattr(seg, "no_speech_prob", None)), _finite(getattr(seg, "avg_logprob", None))
                cr = _finite(getattr(seg, "compression_ratio", None))
                if nsp is not None:
                    no_speech.append(nsp)
                if (nsp is not None and alp is not None and nsp > STT_NO_SPEECH_MAX and alp < -STT_LOGPROB_FLOOR) \
                        or (cr is not None and cr > STT_COMPRESSION_MAX):
                    continue  # Whisper's own "this is not speech" and "this is a loop" rules
                text_parts.append(str(getattr(seg, "text", "")))
                if alp is not None:
                    logprobs.append(alp)
        except SttError:
            raise
        except Exception as e:  # broad-except: a CUDA failure inside the native decode must become a named error, not a crash
            gpu_error = _gpu_error_class(e) if self._device == "cuda" else None
            if gpu_error is None:
                raise
            what = "out of memory" if gpu_error is SttGpuOutOfMemory else "CUDA error"
            self._fall_back_to_cpu(f"{what} during a decode ({type(e).__name__})")
            raise gpu_error(f"the GPU failed during this decode ({what}); later decodes use the CPU") from None
        decode_ms = (time.perf_counter() - decode_start) * 1000
        avg_lp = sum(logprobs) / len(logprobs) if logprobs else None
        max_nsp = max(no_speech) if no_speech else None
        text, truncated, reason = filter_transcript(" ".join(text_parts), avg_logprob=avg_lp, no_speech_prob=max_nsp)
        if not text and not reason:
            reason = "silence" if seen == 0 else "filtered"
        duration = len(job.pcm) / 2 / AUDIO_IN_RATE_HZ
        total = (time.perf_counter() - job.submitted) * 1000
        return SttResult(job.uid, text, str(getattr(info, "language", "en") or "en")[:8], duration, decode_ms, queue_ms,
                         total, decode_ms / (duration * 1000), avg_lp, max_nsp, seen, truncated, reason)

    @staticmethod
    def _to_float(pcm: bytes) -> Any:
        import numpy  # lazy: a dependency of faster-whisper

        return numpy.frombuffer(pcm, dtype="<i2").astype(numpy.float32) / 32768.0

    # -- loading, warming up, falling back

    def _ensure_model(self) -> Any:
        with self._model_lock:
            if self._model is not None:
                return self._model
            model = self._load_model()
            if self._device == "cuda" and not self._warmed:
                self._warm_model(model)  # never raises; after a GPU failure it unloads the model and sets _force_cpu
                if self._model is None:
                    return self._load_model()  # the one fall back: load on the CPU
            return self._model

    def _load_model(self) -> Any:
        cfg = self.config
        try:
            verify_model(cfg.model_dir, cfg.model)
        except ModelStoreError as e:
            self._fail(f"speech model {cfg.model} cannot be used: {e}")
        t0 = time.perf_counter()
        folder = cfg.model_dir / cfg.model
        device, why = "cpu", "device is cpu"
        self.probe_ms = 0.0
        if self._force_cpu:
            why = self._note
        elif cfg.device in ("auto", "cuda"):
            gpu_ct, _ = resolve_compute_type("cuda", cfg.compute_type)
            p0 = time.perf_counter()
            ok, reason = cuda_usable(folder, gpu_ct)
            reason = _short(reason, 160)
            self.probe_ms = (time.perf_counter() - p0) * 1000
            if ok:
                device, why = "cuda", reason
            elif cfg.device == "cuda":
                self._note = _short(f"GPU unusable: {reason}", 160)
                self._phase("device", "the GPU was asked for and cannot be used", ok=False, level="WARNING", fallback=False)
                self._fail(f"[stt] device = cuda, but the GPU is not usable: {reason}")
            else:
                device, why = "cpu", f"GPU unusable: {reason}, using CPU"
                self._note = _short(why, 160)
        compute, cnote = resolve_compute_type(device, cfg.compute_type)
        threads = cfg.cpu_threads if device == "cpu" else 0  # cpu_threads means nothing on the GPU
        try:
            self._model = self._factory(folder, device, compute, threads)
        except Exception as e:  # broad-except: a native library failure while loading must become SttUnavailable or a CPU fall back
            if device == "cuda" and cfg.device == "auto":  # the self-test passed but the real load failed: fall back, once
                device, why = "cpu", f"GPU unusable: the load failed ({type(e).__name__}), using CPU"
                self._note = _short(why, 160)
                compute, cnote = resolve_compute_type("cpu", cfg.compute_type)
                try:
                    self._model = self._factory(folder, "cpu", compute, cfg.cpu_threads)
                except Exception as e2:  # broad-except: same native-library failure mode as the load above
                    self._fail(f"speech model {cfg.model} could not be loaded ({type(e2).__name__})")
            else:
                self._fail(f"speech model {cfg.model} could not be loaded ({type(e).__name__})"
                           + (" on the GPU" if device == "cuda" else ""))
        self._device, self._compute = device, compute
        self._device_used = f"{device} {compute} ({why}{'; ' + cnote if cnote else ''})"
        if device == "cuda":
            self._note = _short(f"GPU {compute}" + (f" ({cnote})" if cnote else ""), 160)
        self.load_ms = (time.perf_counter() - t0) * 1000 - self.probe_ms
        if cfg.device != "cpu":
            self._phase("device", f"the speech model is loaded on the {device.upper()}", fallback=device == "cpu",
                        compute=compute, load_ms=round(self.load_ms, 1), probe_ms=round(self.probe_ms, 1))
        return self._model

    @staticmethod
    def _warm_audio() -> Any:
        import numpy  # lazy: a dependency of faster-whisper

        n = int(STT_WARM_AUDIO_S * AUDIO_IN_RATE_HZ)
        return (numpy.random.default_rng(1).standard_normal(n) * STT_WARM_LEVEL).astype(numpy.float32)

    def _warm_model(self, model: Any) -> None:
        """Decode about one second of very quiet synthetic noise (first with VAD off, so the model really runs; then, when the
        engine uses it, with VAD on, so its model is loaded), so that the first real utterance does not pay the CUDA
        initialisation (measured 7.3 s on base.en) or the VAD start-up. Called with the model lock held.
        Bounded by the warm-up timeout (STT_WARM_TIMEOUT_S). NEVER raises: a failure is an event and a health reason; a
        GPU failure falls back to the CPU (the model is dropped and `_force_cpu` set; the caller loads the CPU model)."""
        self._warmed = True
        self.warm_state = "warming"
        self._phase("warming", "warming up the speech model")
        box: dict[str, BaseException] = {}

        def work() -> None:
            try:
                audio = self._warm_audio()
                for vad in ((False, True) if self._vad else (False,)):  # the second pass loads the voice-activity model
                    segs, _ = model.transcribe(audio, language="en", beam_size=STT_BEAM_SIZE, temperature=0.0,
                                               condition_on_previous_text=False, vad_filter=vad, without_timestamps=True)
                    for _ in segs:
                        pass
            except BaseException as e:  # broad-except: any failure in the warm-up thread is reported, never raised
                box["error"] = e

        t0 = time.perf_counter()
        thread = threading.Thread(target=work, name="stt-warm", daemon=True)
        thread.start()
        thread.join(self._warm_timeout_s)
        ms = (time.perf_counter() - t0) * 1000
        error = box.get("error")
        if thread.is_alive():
            self.warm_state, self.warm_ms = "failed", None
            self._phase("warm", "the warm-up did not finish in time", ok=False, level="WARNING", code="SttTimeout",
                        warm_ms=round(ms, 1))
        elif error is not None:
            self.warm_state, self.warm_ms = "failed", None
            gpu_error = _gpu_error_class(error) if self._device == "cuda" else None
            if gpu_error is not None:
                what = "out of memory" if gpu_error is SttGpuOutOfMemory else "CUDA error"
                self._fall_back_to_cpu(f"{what} during the warm-up ({type(error).__name__})")
            else:
                self._phase("warm", "the warm-up failed", ok=False, level="WARNING", code=type(error).__name__,
                            warm_ms=round(ms, 1))
        else:
            self.warm_state, self.warm_ms = "up", ms
            self._phase("warm", "the speech model is warm", warm_ms=round(ms, 1))

    def warm(self) -> float | None:
        """Load the model if needed and run the warm-up decode (once). Returns the warm-up time in ms, or None when the
        model could not be loaded or the warm-up failed. Blocking (a model load and, on the GPU, a self-test and the CUDA
        start-up): call it from a thread, or use `await warm_async()`. Bounded; never raises. On the GPU the first
        utterance does this by itself; calling it at start-up moves the cost to before the first word."""
        try:
            if self._closed:
                return None
            if self._failed is not None and time.monotonic() - self._failed[0] < STT_UNAVAILABLE_S:
                return None
            with self._model_lock:
                model = self._ensure_model()
                if not self._warmed:
                    self._warm_model(model)
                return self.warm_ms
        except SttError:
            return None
        except Exception:  # broad-except: warm-up is an optimisation; nothing it does may reach the caller
            return None

    def _fall_back_to_cpu(self, reason: str) -> None:
        """After a GPU failure: drop the GPU model and make every later load a CPU load. Once only."""
        if self._gpu_fallback:
            return
        self._gpu_fallback, self._force_cpu = True, True
        self._model, self._warmed, self._device = None, False, "cpu"
        self.warm_state, self.warm_ms = "cold", None
        self._note = _short(f"GPU failed: {reason}, using CPU", 160)
        self._device_used = f"cpu ({self._note})"
        self._phase("fallback", "the GPU failed; the speech decoder now uses the CPU", ok=False, level="WARNING", reason=reason)

    def _fail(self, reason: str) -> Any:
        self._failed = (time.monotonic(), reason[:300])
        raise SttUnavailable(reason[:300])


def _finite(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None
