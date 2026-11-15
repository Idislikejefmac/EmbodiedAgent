"""Read a WAV file for `stt-check`: 16 kHz, mono, 16-bit PCM only, size-capped and header-validated.

    pcm = read_wav_pcm16k(path)        # the raw s16le bytes, at most UTTERANCE_MAX_S long

Raises WavError (a ValueError) with ONE short message for anything else: not a file, too large (STT_WAV_MAX_BYTES,
checked before reading), not RIFF/WAVE, wrong rate, channels, width or compression, a header that promises more
frames than the file holds, no audio, or longer than UTTERANCE_MAX_S. Uses the standard `wave` module only; nothing in
the file is executed or interpreted beyond the audio header.
"""

from __future__ import annotations

import io
import os
import stat
import struct
import wave
from pathlib import Path

from ..limits import AUDIO_IN_RATE_HZ, STT_WAV_MAX_BYTES, UTTERANCE_MAX_BYTES, UTTERANCE_MAX_S


class WavError(ValueError):
    """The WAV file cannot be used. `str(e)` is the one message to show."""


def read_wav_pcm16k(path: str | os.PathLike[str]) -> bytes:
    try:
        p = Path(path)
        st = p.stat()
    except (OSError, ValueError):
        raise WavError("the file does not exist or cannot be read") from None
    if not stat.S_ISREG(st.st_mode):
        raise WavError("not a regular file")
    if st.st_size > STT_WAV_MAX_BYTES:
        raise WavError(f"the file is {st.st_size} bytes; more than {STT_WAV_MAX_BYTES} is refused")
    if st.st_size < 44:
        raise WavError("the file is too small to be a WAV file")
    try:
        with open(p, "rb") as f:
            raw = f.read(STT_WAV_MAX_BYTES + 1)
    except OSError:
        raise WavError("the file cannot be read") from None
    if len(raw) > STT_WAV_MAX_BYTES:
        raise WavError("the file grew while it was read")
    try:
        with wave.open(io.BytesIO(raw), "rb") as w:
            channels, width, rate, frames, comp = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes(), w.getcomptype()
            if comp != "NONE":
                raise WavError("compressed WAV files are not supported (16-bit PCM only)")
            if channels != 1:
                raise WavError(f"the WAV file has {channels} channels; it must be mono")
            if width != 2:
                raise WavError(f"the WAV file has {width * 8}-bit samples; it must be 16-bit")
            if rate != AUDIO_IN_RATE_HZ:
                raise WavError(f"the WAV file is {rate} Hz; it must be {AUDIO_IN_RATE_HZ} Hz")
            if frames <= 0:
                raise WavError("the WAV file holds no audio")
            if frames * 2 > UTTERANCE_MAX_BYTES:
                raise WavError(f"the WAV file is longer than {UTTERANCE_MAX_S} s")
            data = w.readframes(frames)
    except WavError:
        raise
    except (wave.Error, EOFError, struct.error, ValueError, OverflowError, MemoryError):
        raise WavError("not a valid WAV file (the header is damaged or not RIFF/WAVE)") from None
    if len(data) != frames * 2:
        raise WavError("the WAV header promises more audio than the file holds (truncated)")
    return data
