"""P0-06 SimBody media files: `read_wav`, `wav_bytes`, `write_default_media` and `SimBody.start` (B10, B2).

The sim body reads whatever PNG, JPEG and WAV files are in its media folders, so a folder is input: an
empty, corrupt, truncated, mislabelled or oversized file must make `start()` refuse with a clear
ValueError (or an OSError that names the problem), never an unrelated library error, and a bad file must
not poison a later call (capture_image, audio_in_stream). Everything happens in a fresh sandbox folder.
"""

from __future__ import annotations

import asyncio
import struct
import wave
from pathlib import Path
from typing import Any

from brain.body import sim_assets as A
from brain.body.base import BodyError
from brain.body.sim import SimBody

from ..api import Case, Ctx, Gen, experiment, failure, hostile, invalid, valid

GOOD = A.wav_bytes(b"\x00\x00" * 160)
PNG = A.default_images()["01_gradient.png"]


def _patch(wav: bytes, at: int, fmt: str, value: int) -> bytes:
    b = bytearray(wav)
    b[at:at + struct.calcsize(fmt)] = struct.pack(fmt, value)
    return bytes(b)


# canonical 44-byte header: format tag at 20, channels at 22, rate at 24, bits at 34, data size at 40
WAVS: dict[str, bytes] = {
    "empty": b"", "one_byte": b"R", "riff_only": b"RIFF", "header_only": GOOD[:44], "truncated_header": GOOD[:30],
    "truncated_data": GOOD[:-50], "garbage": b"RIFF" + b"x" * 100, "not_wav_png": PNG,
    "eight_bit": _patch(GOOD, 34, "<H", 8), "float_format": _patch(GOOD, 20, "<H", 3),
    "rate_zero": _patch(GOOD, 24, "<I", 0), "channels_zero": _patch(GOOD, 22, "<H", 0),
    "channels_many": _patch(GOOD, 22, "<H", 9), "data_size_huge": _patch(GOOD, 40, "<I", 0xFFFFFFF0),
    "text_file": b"hello world\n" * 20, "nul_bytes": b"\x00" * 200,
}


WAV_DEFECT = ("X01B-01: read_wav leaks EOFError or wave.Error for an empty, truncated or non-WAV file and accepts a rate "
              "of 0 or 9 channels, so SimBody.start fails with a library error (not ValueError) on a bad media file")


def leaks(data: bytes, strict: bool = True) -> str | None:
    """The recorded defect (X01B-01) for this file content, found by asking read_wav itself."""
    try:
        rate, ch, pcm = A.read_wav(data)
    except ValueError:
        return None
    except Exception:  # broad-except: this IS the defect probe, any other exception type is the finding
        return WAV_DEFECT
    return WAV_DEFECT if strict and (rate <= 0 or not 1 <= ch <= 2 or len(pcm) % (2 * ch)) else None


QUICK_START_WAVS = {"empty", "garbage", "eight_bit", "rate_zero", "channels_many", "truncated_data"}  # full mode runs every file


def gen_valid(g: Gen) -> list[Case]:
    return [valid("wav_roundtrip", {"op": "wav_roundtrip", "pcm": b"\x01\x00\xff\x7f" * 100, "rate": 16000, "channels": 1}),
            valid("wav_rate_8k_stereo", {"op": "wav_roundtrip", "pcm": b"\x00\x00" * 400, "rate": 8000, "channels": 2}),
            valid("wav_empty_pcm", {"op": "wav_roundtrip", "pcm": b"", "rate": 16000, "channels": 1}),
            valid("write_default_media", {"op": "write_media", "where": "plain"}),
            valid("write_twice_overwrites", {"op": "write_media", "where": "twice"}),
            valid("start_with_default_media", {"op": "start", "dirs": "default"}),
            valid("start_with_sandbox_media", {"op": "start", "dirs": "sandbox"}),
            valid("start_ignores_other_files", {"op": "start", "dirs": "extras"})]


def gen_invalid(g: Gen) -> list[Case]:
    out = [invalid(f"read_wav_{name}", {"op": "read_wav", "data": data}, "N5" if name.startswith("not_") else "N4",
                   known_defect=leaks(data)) for name, data in WAVS.items()]
    out += [invalid(f"start_wav_{name}", {"op": "start", "dirs": "bad_wav", "wav": data}, "N5",
                    known_defect=leaks(data, strict=False)) for name, data in WAVS.items() if name not in ("data_size_huge",)]
    out += [invalid("start_png_corrupt", {"op": "start_then_capture", "png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 10}, "N4"),
            invalid("start_png_empty", {"op": "start_then_capture", "png": b""}, "N3"),
            invalid("start_png_is_text", {"op": "start_then_capture", "png": b"not an image"}, "N5"),
            invalid("start_no_images", {"op": "start", "dirs": "no_images"}, "N3"),
            invalid("start_no_audio", {"op": "start", "dirs": "no_audio"}, "N3"),
            invalid("start_dir_missing", {"op": "start", "dirs": "missing"}, "N3"),
            invalid("start_dir_is_a_file", {"op": "start", "dirs": "is_file"}, "N1"),
            invalid("start_dir_is_nul_path", {"op": "start", "dirs": "nul_path"}, "N5"),
            invalid("start_dir_is_int", {"op": "start", "dirs": "int_path"}, "N1")]
    for name, args in (("rate_zero", (b"\x00\x00", 0, 1)), ("rate_negative", (b"\x00\x00", -1, 1)),
                       ("rate_over_uint32", (b"\x00\x00", 2**32, 1)), ("channels_zero", (b"\x00\x00", 16000, 0)),
                       ("channels_over_uint16", (b"\x00\x00", 16000, 65536)), ("pcm_is_str", ("ab", 16000, 1)),
                       ("pcm_is_none", (None, 16000, 1)), ("rate_is_float", (b"\x00\x00", 16000.5, 1)),
                       ("rate_is_str", (b"\x00\x00", "16000", 1)), ("channels_is_none", (b"\x00\x00", 16000, None))):
        out.append(invalid(f"wav_bytes_{name}", {"op": "wav_bytes", "args": args}, "N6" if "rate" in name or "channels" in name else "N1",
                           known_defect="X01B-03: wav_bytes accepts a float sample rate and silently truncates it (16000.5 Hz is written as 16000)"
                           if name == "rate_is_float" else None))
    out.append(invalid("write_media_to_a_file", {"op": "write_media", "where": "is_file"}, "N1"))
    return out


def gen_hostile(g: Gen) -> list[Case]:
    out = [hostile("start_dir_with_png_named_dir", {"op": "start", "dirs": "dir_named_png"}, "N7"),
           hostile("start_wav_in_png_name", {"op": "start", "dirs": "wav_named_png"}, "N7"),
           hostile("start_names_with_traversal_text", {"op": "start", "dirs": "odd_names"}, "N7"),
           hostile("write_media_reserved_name", {"op": "write_media", "where": "reserved"}, "N7"),
           hostile("write_media_unicode_name", {"op": "write_media", "where": "unicode"}, "N7")]
    for i in range(g.n(6, 100)):  # random bytes as a WAV file
        data = g.rng.randbytes(g.rng.choice([0, 12, 44, 45, 200]))
        out.append(hostile(f"read_wav_random_{i}", {"op": "read_wav", "data": data}, "N7", known_defect=leaks(data)))
    for i in range(g.n(6, 100)):  # a valid WAV with random bytes flipped
        b = bytearray(GOOD)
        for _ in range(g.rng.randint(1, 4)):
            b[g.rng.randrange(44)] = g.rng.randrange(256)
        out.append(hostile(f"read_wav_mutated_{i}", {"op": "read_wav", "data": bytes(b)}, "N7", known_defect=leaks(bytes(b))))
    return out


def gen_failure(g: Gen) -> list[Case]:
    return [failure("start_file_vanishes_between_listing_and_read", {"op": "start", "dirs": "vanishing"}, "N10"),
            failure("start_twice", {"op": "start_twice"}, "N10"),
            failure("start_many_files", {"op": "start", "dirs": "many_files"}, "N11"),
            failure("capture_after_bad_start", {"op": "start_then_capture", "png": PNG}, "N13")]


def _build_dirs(ctx: Ctx, d: dict[str, Any]) -> tuple[Any, Any]:
    kind = d["dirs"]
    root = ctx.sandbox.fresh_dir("media")
    images, audio = root / "images", root / "audio"
    if kind == "default":
        return None, None
    if kind == "sandbox":
        return ctx.sandbox.root / "sim" / "images", ctx.sandbox.root / "sim" / "audio"
    A.write_default_media(images, audio)
    if kind == "extras":
        (images / "notes.txt").write_text("hi", encoding="utf-8")
        (audio / "readme.md").write_text("hi", encoding="utf-8")
    elif kind == "bad_wav":
        (audio / "99_bad.wav").write_bytes(d["wav"])
    elif kind == "no_images":
        for f in images.iterdir():
            f.unlink()
    elif kind == "no_audio":
        for f in audio.iterdir():
            f.unlink()
    elif kind == "missing":
        return root / "nothing-here", audio
    elif kind == "is_file":
        (root / "afile").write_text("x", encoding="utf-8")
        return root / "afile", audio
    elif kind == "nul_path":
        return str(root) + "\x00evil", audio
    elif kind == "int_path":
        return 7, audio
    elif kind == "dir_named_png":
        (images / "folder.png").mkdir()
    elif kind == "wav_named_png":
        (images / "99_actually_audio.png").write_bytes(GOOD)
    elif kind == "odd_names":
        for n in ("..%5c..%5cx.png", "a b.png", "ünï.png", "x.PNG", "x.png.exe"):
            (images / n).write_bytes(PNG)
    elif kind == "vanishing":
        pass
    elif kind == "many_files":
        for i in range(300):
            (images / f"{i:04}.png").write_bytes(PNG[:60])
    return images, audio


async def _start(ctx: Ctx, d: dict[str, Any], images: Any, audio: Any) -> str:
    body = SimBody()
    try:
        body = SimBody(image_dir=images, audio_dir=audio)
        await body.start()
        started = True
        if d["dirs"] == "bad_wav":  # a file read_wav accepted must also stream (rate 0 or 9 channels end the producer)
            s = body.audio_in_stream()
            try:
                await asyncio.wait_for(anext(s), 1.0)
            except (TimeoutError, StopAsyncIteration):
                pass
            finally:
                await s.aclose()
    except (ValueError, TypeError, OSError, BodyError) as e:
        text = str(e)
        ctx.expect(0 < len(text) <= 300, "fail_closed", f"the refusal says {len(text)} characters")
        started = False
    finally:
        await body.close()
    if d["dirs"] in ("default", "sandbox", "extras", "many_files", "odd_names", "dir_named_png", "wav_named_png"):
        ctx.expect(started or d["dirs"] in ("dir_named_png", "wav_named_png", "odd_names", "many_files"), "validate_before_use",
                   "a good media folder was refused")
    return "started" if started else "refused"


def _wav_args_refused(ctx: Ctx, args: tuple[Any, ...]) -> str:
    try:
        out = A.wav_bytes(*args)
    except (ValueError, TypeError, wave.Error, struct.error):  # clear library refusals of a dev helper's argument
        return "refused"
    # accepted: the file it made must read back with the same parameters (no garbage in, garbage out)
    rate, ch, pcm = A.read_wav(out)
    ctx.expect(rate == args[1] and ch == args[2] and pcm == args[0], "validate_before_use",
               f"wav_bytes{args[1:]} made a file that reads back as {rate} Hz, {ch} ch")
    return "accepted"


@experiment(
    id="P0-06.sim.media", card="P0-06",
    entries=("file_site.brain/body/sim.py:SimBody.start", "file_site.brain/body/sim_assets.py:read_wav",
             "file_site.brain/body/sim_assets.py:wav_bytes", "file_site.brain/body/sim_assets.py:write_default_media"),
    hypothesis="A media folder or file that is missing, empty, corrupt, truncated, mislabelled or odd is refused by "
               "SimBody.start (or read_wav, wav_bytes, write_default_media) with a ValueError or OSError of at most "
               "300 characters, never a library error, a hang or a write outside the sandbox.",
    blast_radius="sandbox", valid=gen_valid, invalid=gen_invalid, hostile=gen_hostile, failure=gen_failure)
async def check(case: Case, ctx: Ctx) -> Any:
    d = case.data
    op = d["op"]
    if op == "read_wav":
        try:
            rate, ch, pcm = A.read_wav(d["data"])
        except ValueError:
            return "refused"
        ctx.expect(rate > 0 and 1 <= ch <= 2 and len(pcm) % (2 * ch) == 0, "validate_before_use",
                   f"read_wav accepted a file with {rate} Hz, {ch} channels, {len(pcm)} bytes of data")
        return "accepted"
    if op == "wav_roundtrip":
        rate, ch, pcm = A.read_wav(A.wav_bytes(d["pcm"], d["rate"], d["channels"]))
        ctx.expect((rate, ch, pcm) == (d["rate"], d["channels"], d["pcm"]), "validate_before_use", "a WAV did not round-trip")
        return "ok"
    if op == "wav_bytes":
        return _wav_args_refused(ctx, d["args"])
    if op == "write_media":
        root = ctx.sandbox.fresh_dir("write")
        where = d["where"]
        if where == "is_file":
            (root / "f").write_text("x", encoding="utf-8")
            target = root / "f"
        else:
            name = {"plain": "m", "twice": "m", "reserved": "CON", "unicode": "ünï ✓"}[where]
            target = root / name
        try:
            for _ in range(2 if where == "twice" else 1):
                A.write_default_media(target / "images", target / "audio")
        except (ValueError, OSError):
            ctx.expect(where in ("is_file", "reserved"), "validate_before_use", "a good target was refused")
            return "refused"
        ctx.expect(len(list((target / "images").iterdir())) == 3, "validate_before_use", "the default images were not written")
        return "written"
    if op == "start_twice":
        body = SimBody()
        await body.start()
        await body.start()
        await body.close()
        return "ok"
    if op == "start":
        images, audio = _build_dirs(ctx, d)
        if d["dirs"] == "vanishing":
            return await _start_vanishing(ctx, images, audio)
        return await _start(ctx, d, images, audio)
    if op == "start_then_capture":
        root = ctx.sandbox.fresh_dir("capture")
        A.write_default_media(root / "images", root / "audio")
        (root / "images" / "99_bad.png").write_bytes(d["png"])
        body = SimBody(image_dir=root / "images", audio_dir=root / "audio")
        try:
            await body.start()
            for _ in range(5):  # the bad file comes up in the round-robin
                try:
                    await asyncio.wait_for(body.capture_image(max_edge_px=64), 3)
                except (ValueError, BodyError, OSError):
                    pass
            ctx.expect((await body.status()).connected, "no_crash", "status() lost the link after a bad image")
        except (ValueError, OSError, BodyError):
            return "refused at start"
        finally:
            await body.close()
        return "ok"
    raise ValueError(f"unknown op {op!r}")


async def _start_vanishing(ctx: Ctx, images: Path, audio: Path) -> str:
    victim = sorted(images.iterdir())[0]
    body = SimBody(image_dir=images, audio_dir=audio)
    victim.unlink()  # gone before start reads it (the listing in start() happens after this, so it is just missing)
    try:
        await body.start()
    except (ValueError, OSError):
        return "refused"
    finally:
        await body.close()
    return "started"
