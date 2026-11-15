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

Events: `speech.stt` (utterance id, ms, rtf, chars, ok, code), NEVER the words. The words go to the prompt capture
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
                      STT_CUDA_PROBE_TIMEOUT_S, STT_DECODE_TIMEOUT_S, STT_LOAD_TIMEOUT_S, STT_LOGPROB_FLOOR, STT_MAX_CHARS,
                      STT_NO_SPEECH_MAX, STT_PENDING_MAX, STT_PHANTOM_LOGPROB_FLOOR, STT_PHANTOM_NO_SPEECH, STT_REPEAT_MAX,
                      STT_SILENCE_RMS, STT_UNAVAILABLE_S, UTTERANCE_MAX_BYTES, UTTERANCE_MAX_S, UTTERANCE_MIN_MS)
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
                 ms=round(ms, 1), rtf=round(rtf, 3), chars=chars, audio_s=round(duration, 2), empty=reason or None)

    vendor = VENDOR


# ---- the local engine ----------------------------------------------------------------------------------------------

ModelFactory = Callable[[Path, str, str, int], Any]  # (folder, device, compute_type, cpu_threads) -> object with transcribe()
_CUDA_RESULT: dict[str, tuple[bool, str]] = {}  # the one-time self-test, per model folder

_CUDA_SELFTEST = (
    "import sys, numpy\n"
    "from faster_whisper import WhisperModel\n"
    "m = WhisperModel(sys.argv[1], device='cuda', compute_type=sys.argv[2], local_files_only=True)\n"
    "audio = (numpy.random.default_rng(1).standard_normal(16000) * 0.01).astype('float32')\n"
    "segs, _ = m.transcribe(audio, language='en', beam_size=1, temperature=0.0, vad_filter=False)\n"
    "list(segs)\n"
    "print('CUDA_SELFTEST_OK')\n"
)


def _default_factory(folder: Path, device: str, compute_type: str, cpu_threads: int) -> Any:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # belt and braces: the hub library must never try the network
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from faster_whisper import WhisperModel  # lazy: importing ctranslate2 takes about a second

    return WhisperModel(str(folder), device=device, compute_type=compute_type, cpu_threads=cpu_threads, num_workers=1,
                        local_files_only=True)


def cuda_usable(folder: Path, compute_type: str) -> tuple[bool, str]:
    """`device = "auto"`: is CUDA usable? (True, why) or (False, why). One-time per folder per process. A cheap
    check first (no CUDA device visible to CTranslate2: CPU). Only if a device shows up, a real load and a one-second
    transcribe run in a CHILD process with a timeout, so a driver that is too old can never take the brain down."""
    key = str(folder)
    if key in _CUDA_RESULT:
        return _CUDA_RESULT[key]
    try:
        import ctranslate2

        count = int(ctranslate2.get_cuda_device_count())
    except Exception as e:  # broad-except: a driver problem inside the native library must mean "use the CPU", never a crash
        return _CUDA_RESULT.setdefault(key, (False, f"CUDA could not be queried ({type(e).__name__})"))
    if count <= 0:
        return _CUDA_RESULT.setdefault(key, (False, "no CUDA device is usable by CTranslate2 (driver or libraries too old)"))
    ct = "int8_float16" if compute_type == "int8" else "float32"
    try:
        done = subprocess.run([sys.executable, "-I", "-c", _CUDA_SELFTEST, str(folder), ct], capture_output=True,
                              text=True, timeout=STT_CUDA_PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL,
                              env={**os.environ, "HF_HUB_OFFLINE": "1"})
        ok = done.returncode == 0 and "CUDA_SELFTEST_OK" in done.stdout
        result = (ok, "CUDA self-test passed" if ok else "the CUDA self-test failed (the GPU stack is not usable)")
    except (OSError, subprocess.SubprocessError) as e:
        result = (False, f"the CUDA self-test could not run ({type(e).__name__})")
    return _CUDA_RESULT.setdefault(key, result)


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
                 pending_max: int = STT_PENDING_MAX, vad_filter: bool = True) -> None:
        super().__init__()
        self.config = config
        self._factory = factory or _default_factory
        self._decode_timeout_s, self._load_timeout_s = decode_timeout_s, load_timeout_s
        self._vad = vad_filter
        self._q: queue.Queue[_Job | None] = queue.Queue()
        self._pending = 0
        self._pending_max = pending_max
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._model: Any = None
        self._device_used = ""
        self._failed: tuple[float, str] | None = None  # (monotonic time of the failure, reason)
        self._jobs: set[_Job] = set()
        self.load_ms: float | None = None

    @property
    def device_used(self) -> str:
        return self._device_used or "not loaded yet"

    @property
    def loaded(self) -> bool:
        return self._model is not None

    # -- the async side

    async def _transcribe(self, utterance_id: str, pcm: bytes, generation: int) -> SttResult:
        if self._failed is not None:
            if time.monotonic() - self._failed[0] < STT_UNAVAILABLE_S:
                raise SttUnavailable(self._failed[1])
            self._failed = None
        job = self._admit(utterance_id, pcm)
        limit = self._decode_timeout_s + (0.0 if self._model is not None else self._load_timeout_s)
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
        started = time.perf_counter()
        model = self._ensure_model()
        decode_start = time.perf_counter()
        queue_ms = (decode_start - job.submitted) * 1000
        text_parts: list[str] = []
        logprobs: list[float] = []
        no_speech: list[float] = []
        seen = 0
        audio = self._to_float(job.pcm)
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
        decode_ms = (time.perf_counter() - decode_start) * 1000
        avg_lp = sum(logprobs) / len(logprobs) if logprobs else None
        max_nsp = max(no_speech) if no_speech else None
        text, truncated, reason = filter_transcript(" ".join(text_parts), avg_logprob=avg_lp, no_speech_prob=max_nsp)
        if not text and not reason:
            reason = "silence" if seen == 0 else "filtered"
        duration = len(job.pcm) / 2 / AUDIO_IN_RATE_HZ
        total = (time.perf_counter() - job.submitted) * 1000
        _ = started
        return SttResult(job.uid, text, str(getattr(info, "language", "en") or "en")[:8], duration, decode_ms, queue_ms,
                         total, decode_ms / (duration * 1000), avg_lp, max_nsp, seen, truncated, reason)

    @staticmethod
    def _to_float(pcm: bytes) -> Any:
        import numpy  # lazy: a dependency of faster-whisper

        return numpy.frombuffer(pcm, dtype="<i2").astype(numpy.float32) / 32768.0

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        t0 = time.perf_counter()
        try:
            verify_model(self.config.model_dir, self.config.model)
        except ModelStoreError as e:
            self._fail(f"speech model {self.config.model} cannot be used: {e}")
        folder = self.config.model_dir / self.config.model
        device, why = "cpu", "device is cpu"
        if self.config.device == "auto":
            ok, why = cuda_usable(folder, self.config.compute_type)
            device = "cuda" if ok else "cpu"
        compute = self.config.compute_type if device == "cpu" else ("int8_float16" if self.config.compute_type == "int8"
                                                                    else "float32")
        try:
            self._model = self._factory(folder, device, compute, self.config.cpu_threads)
        except Exception as e:  # broad-except: a native library failure while loading must become SttUnavailable
            if device == "cuda":  # the self-test passed but the real load failed: fall back, once
                try:
                    self._model = self._factory(folder, "cpu", self.config.compute_type, self.config.cpu_threads)
                    device, why = "cpu", f"CUDA load failed ({type(e).__name__}); using the CPU"
                except Exception as e2:  # broad-except: same native-library failure mode as the load above
                    self._fail(f"speech model {self.config.model} could not be loaded ({type(e2).__name__})")
            else:
                self._fail(f"speech model {self.config.model} could not be loaded ({type(e).__name__})")
        self._device_used = f"{device} ({why})"
        self.load_ms = (time.perf_counter() - t0) * 1000
        return self._model

    def _fail(self, reason: str) -> Any:
        self._failed = (time.monotonic(), reason[:300])
        raise SttUnavailable(reason[:300])


def _finite(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None
