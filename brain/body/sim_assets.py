"""Synthetic test media for SimBody, made with the standard library only: small PNG images and WAV tones.

Also the few image helpers SimBody needs: reading the size of a PNG or JPEG from its header, and
shrinking a PNG (only the plain kind written here: 8 bits per channel, grey, RGB or RGBA, not
interlaced). Nothing is downloaded and no imaging library is needed.
"""

from __future__ import annotations

import io
import math
import struct
import wave
import zlib
from collections.abc import Callable
from pathlib import Path

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 6: 4}  # PNG colour type -> channels (grey, RGB, RGBA)


# ---- PNG ---------------------------------------------------------------------------------------------


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_from_rgb(width: int, height: int, rgb: bytes) -> bytes:
    """Encode `width * height * 3` RGB bytes as a PNG."""
    if len(rgb) != width * height * 3:
        raise ValueError("rgb must be width * height * 3 bytes")
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    return (PNG_MAGIC + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b""))


def make_png(width: int, height: int, pixel: Callable[[int, int], tuple[int, int, int]]) -> bytes:
    """A PNG whose pixel (x, y) is `pixel(x, y)` as (r, g, b), each 0 to 255."""
    return png_from_rgb(width, height, bytes(c for y in range(height) for x in range(width) for c in pixel(x, y)))


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """(width, height, RGB bytes) of a simple PNG. Raises ValueError for kinds this reader does not handle."""
    if not data.startswith(PNG_MAGIC):
        raise ValueError("not a PNG")
    pos, idat, header = len(PNG_MAGIC), [], None
    while pos + 8 <= len(data):
        (length,), kind = struct.unpack_from(">I", data, pos), data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + length
    if header is None:
        raise ValueError("PNG has no header")
    width, height, depth, colour, _, _, interlace = header
    if depth != 8 or colour not in _CHANNELS or interlace:
        raise ValueError("only 8-bit grey, RGB or RGBA PNGs that are not interlaced can be resized")
    n = _CHANNELS[colour]
    stride = width * n
    raw = zlib.decompress(b"".join(idat))
    if len(raw) != (stride + 1) * height:
        raise ValueError("PNG data has the wrong length")
    out = bytearray()
    prev = bytearray(stride)
    for y in range(height):
        ftype = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - n] if i >= n else 0
            b = prev[i]
            c = prev[i - n] if i >= n else 0
            if ftype == 1:
                line[i] = (line[i] + a) & 255
            elif ftype == 2:
                line[i] = (line[i] + b) & 255
            elif ftype == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
            elif ftype != 0:
                raise ValueError("PNG has an unknown filter type")
        prev = line
        if n == 3:
            out += line
        else:
            for x in range(width):
                px = line[x * n:x * n + n]
                out += bytes((px[0], px[0], px[0])) if n == 1 else px[:3]
    return width, height, bytes(out)


def shrink_png(data: bytes, max_edge_px: int) -> tuple[bytes, int, int]:
    """(png, width, height) with the longer edge at most `max_edge_px` (nearest neighbour). Returns the
    input unchanged when it already fits."""
    width, height = image_size(data)[1:]
    if max(width, height) <= max_edge_px:
        return data, width, height
    width, height, rgb = decode_png(data)
    scale = max_edge_px / max(width, height)
    w2, h2 = max(1, int(width * scale)), max(1, int(height * scale))
    out = bytearray()
    for y in range(h2):
        sy = min(height - 1, int(y / scale))
        for x in range(w2):
            sx = min(width - 1, int(x / scale))
            out += rgb[(sy * width + sx) * 3:(sy * width + sx) * 3 + 3]
    return png_from_rgb(w2, h2, bytes(out)), w2, h2


def image_size(data: bytes) -> tuple[str, int, int]:
    """(encoding, width, height) read from a PNG or JPEG header. Raises ValueError otherwise."""
    if data.startswith(PNG_MAGIC) and data[12:16] == b"IHDR":
        width, height = struct.unpack_from(">II", data, 16)
        return "png", width, height
    if data.startswith(b"\xff\xd8"):
        pos = 2
        while pos + 9 < len(data) and data[pos] == 0xFF:
            marker = data[pos + 1]
            if marker in (0xC0, 0xC1, 0xC2):  # start of frame: baseline, extended, progressive
                height, width = struct.unpack_from(">HH", data, pos + 5)
                return "jpeg", width, height
            pos += 2 + struct.unpack_from(">H", data, pos + 2)[0]
    raise ValueError("not a PNG or JPEG image")


# ---- WAV ---------------------------------------------------------------------------------------------


def wav_bytes(pcm_s16le: bytes, sample_rate_hz: int = 16000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate_hz)
        w.writeframes(pcm_s16le)
    return buf.getvalue()


def read_wav(data: bytes) -> tuple[int, int, bytes]:
    """(sample_rate_hz, channels, pcm_s16le bytes) of a 16-bit PCM WAV."""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getsampwidth() != 2 or w.getcomptype() != "NONE":
            raise ValueError("only 16-bit PCM WAV files are supported")
        return w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())


def tone(freq_hz: float, seconds: float, sample_rate_hz: int = 16000, amplitude: float = 0.3) -> bytes:
    """A sine tone as mono pcm_s16le."""
    n = int(seconds * sample_rate_hz)
    return struct.pack(f"<{n}h", *(int(32767 * amplitude * math.sin(2 * math.pi * freq_hz * i / sample_rate_hz))
                                    for i in range(n)))


# ---- the default test set ----------------------------------------------------------------------------


def default_images() -> dict[str, bytes]:
    """Three small PNGs (160 x 120): a gradient, a checkerboard and colour bars."""
    bars = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0), (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]
    return {
        "01_gradient.png": make_png(160, 120, lambda x, y: (x * 255 // 159, y * 255 // 119, 128)),
        "02_checker.png": make_png(160, 120, lambda x, y: (230, 230, 230) if (x // 20 + y // 20) % 2 else (25, 25, 25)),
        "03_bars.png": make_png(160, 120, lambda x, y: bars[x * 8 // 160]),
    }


def default_audio() -> dict[str, bytes]:
    """Two short 16 kHz mono WAV files: a 440 Hz tone, and two tones in turn."""
    return {
        "01_tone_440.wav": wav_bytes(tone(440.0, 0.8)),
        "02_two_tone.wav": wav_bytes(tone(330.0, 0.4) + tone(660.0, 0.4)),
    }


def write_default_media(image_dir: Path, audio_dir: Path) -> None:
    """Write the default test set into two folders (used by tests and by anyone who wants files to edit)."""
    for folder, files in ((image_dir, default_images()), (audio_dir, default_audio())):
        folder.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (folder / name).write_bytes(data)
