"""FakeSttEngine: the scripted speech-to-text engine for tests (docs/testing/00 section 5: `FakeStt`).

It uses the same audio gate and state machine as the real engine (`BufferedSttEngine`), so the contract tests run
against both. Script it with text (a string, or a list used in turn), a latency, an error to raise, or a hang:

    fake = FakeSttEngine(["hello there", "what time is it"], latency_s=0.05)
    fake = FakeSttEngine(error=SttUnavailable("down"))
    fake = FakeSttEngine(hang=True)       # never returns: the caller's timeout and cancel must save it

`fake.bytes_received` counts audio bytes that reached the engine (T-P2-04-010); `fake.utterances` lists
(utterance_id, pcm) for what it was asked to transcribe. `partials` are sent to the start() callback while audio is fed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

from brain.limits import AUDIO_IN_RATE_HZ
from brain.voice.stt import BufferedSttEngine, SttError, SttResult


class FakeSttEngine(BufferedSttEngine):
    def __init__(self, script: str | Sequence[str] = "hello", *, latency_s: float = 0.0, error: SttError | None = None,
                 hang: bool = False, partials: Sequence[str] = ()) -> None:
        super().__init__()
        self.script = [script] if isinstance(script, str) else list(script)
        self.latency_s, self.error, self.hang, self.partial_texts = latency_s, error, hang, list(partials)
        self.bytes_received = 0
        self.utterances: list[tuple[str, bytes]] = []
        self.cancelled_count = 0
        self._n = 0
        self._fed = 0

    def start(self, utterance_id, *, on_partial=None):  # type: ignore[no-untyped-def]
        super().start(utterance_id, on_partial=on_partial)
        self._fed = 0

    def _after_feed(self, data: bytes) -> None:
        self.bytes_received += len(data)
        if self._on_partial is not None and self._uid is not None and self._fed < len(self.partial_texts):
            self._on_partial(self._uid, self.partial_texts[self._fed])
        self._fed += 1

    def _cancel_running(self) -> None:
        self.cancelled_count += 1

    async def _transcribe(self, utterance_id: str, pcm: bytes, generation: int) -> SttResult:
        self.utterances.append((utterance_id, pcm))
        t0 = time.perf_counter()
        if self.hang:
            await asyncio.sleep(3600)
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if generation != self._generation:
            from brain.voice.stt import SttCancelled

            raise SttCancelled("the utterance was cancelled")
        if self.error is not None:
            raise self.error
        text = self.script[min(self._n, len(self.script) - 1)] if self.script else ""
        self._n += 1
        ms = (time.perf_counter() - t0) * 1000
        duration = len(pcm) / 2 / AUDIO_IN_RATE_HZ
        from brain.voice.stt import filter_transcript

        clean, truncated, reason = filter_transcript(text, avg_logprob=-0.2, no_speech_prob=0.01)
        return SttResult(utterance_id, clean, "en", duration, ms, 0.0, ms, ms / (duration * 1000), -0.2, 0.01,
                         1 if clean else 0, truncated, reason)
