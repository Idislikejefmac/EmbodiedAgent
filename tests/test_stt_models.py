"""Card P2-04: the model store (lock verification), the one-time fetch tool's checks, and the WAV reader.

Everything here uses synthetic folders and a fake hub: no network, no real model. The fetch tool runs the real code with an
injected resolver and downloader, so the checks that protect the laptop from a bad download are the ones under test.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import wave
from pathlib import Path
from typing import Any

import pytest
from stt_helpers import GOOD_FILES, make_model, sha, symlink_or_skip

from brain.limits import STT_JSON_FILE_MAX_BYTES, STT_LOCK_MAX_BYTES, STT_MODEL_FILE_MAX_BYTES, STT_MODEL_FILES_MAX, STT_WAV_MAX_BYTES, UTTERANCE_MAX_S
from brain.voice import modelstore as MS
from brain.voice.modelstore import ModelStoreError, check_folder, read_lock, verify_model
from brain.voice.wavcheck import WavError, read_wav_pcm16k
from tools import fetch_stt_model as F

pytestmark = pytest.mark.trace("T-P2-04-009", "T-P2-04-017")


# ---- check_folder and verify_model -------------------------------------------------------------------------------------


def test_a_good_folder_passes_and_reports_sizes(tmp_path: Path) -> None:
    folder = make_model(tmp_path)
    assert check_folder(folder) == {k: len(v) for k, v in GOOD_FILES.items()}
    assert verify_model(tmp_path, "base.en").revision == "a" * 40


def test_verification_is_cached_by_size_and_time_so_a_start_does_not_rehash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_model(tmp_path)
    verify_model(tmp_path, "base.en")  # fills the cache
    calls: list[Path] = []
    real = MS.sha256_of
    monkeypatch.setattr(MS, "sha256_of", lambda p: calls.append(p) or real(p))
    verify_model(tmp_path, "base.en")
    assert calls == []
    (tmp_path / "base.en" / "model.bin").write_bytes(GOOD_FILES["model.bin"][:-1] + b"\x02")  # same size, new time
    with pytest.raises(ModelStoreError, match="SHA-256"):
        verify_model(tmp_path, "base.en")
    assert calls  # a changed file is hashed again


def test_a_modified_file_is_refused(tmp_path: Path) -> None:
    folder = make_model(tmp_path)
    (folder / "tokenizer.json").write_bytes(b'{"version": "2.0"}')  # same size, other bytes
    with pytest.raises(ModelStoreError, match="SHA-256"):
        verify_model(tmp_path, "base.en")


def test_a_file_of_another_size_is_refused_with_its_name(tmp_path: Path) -> None:
    folder = make_model(tmp_path)
    (folder / "vocabulary.txt").write_bytes(b"hello\n")
    with pytest.raises(ModelStoreError, match=r"vocabulary.txt is 6 bytes"):
        verify_model(tmp_path, "base.en")


def test_a_truncated_model_bin_is_refused(tmp_path: Path) -> None:
    folder = make_model(tmp_path)
    data = (folder / "model.bin").read_bytes()
    (folder / "model.bin").write_bytes(data[: len(data) // 2])
    with pytest.raises(ModelStoreError, match="incomplete|changed"):
        verify_model(tmp_path, "base.en")


def test_an_extra_file_is_refused(tmp_path: Path) -> None:
    folder = make_model(tmp_path)
    (folder / "extra.txt").write_text("x")
    with pytest.raises(ModelStoreError, match="unexpected entry"):
        verify_model(tmp_path, "base.en")


@pytest.mark.parametrize("name", ["evil.pt", "weights.pkl", "x.pickle", "second.bin", "run.py", "a.dll", "model.bin.bak", "README.md", ".hidden"])
def test_extra_or_code_carrying_file_names_are_refused(tmp_path: Path, name: str) -> None:
    folder = make_model(tmp_path)
    (folder / name).write_bytes(b"\x80\x04.")
    with pytest.raises(ModelStoreError):
        check_folder(folder)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    folder = make_model(tmp_path)
    (folder / "model.bin").unlink()
    with pytest.raises(ModelStoreError, match="missing"):
        verify_model(tmp_path, "base.en")


def test_a_missing_vocabulary_is_refused(tmp_path: Path) -> None:
    files = {k: v for k, v in GOOD_FILES.items() if k != "vocabulary.txt"}
    folder = make_model(tmp_path, files=files, lock=False)
    with pytest.raises(ModelStoreError, match="vocabulary"):
        check_folder(folder)


def test_a_subfolder_is_refused(tmp_path: Path) -> None:
    folder = make_model(tmp_path)
    (folder / "sub").mkdir()
    with pytest.raises(ModelStoreError):
        check_folder(folder)


def test_a_symlinked_file_is_refused(tmp_path: Path) -> None:
    folder = make_model(tmp_path)
    real = tmp_path / "elsewhere.bin"
    real.write_bytes(GOOD_FILES["model.bin"])
    (folder / "model.bin").unlink()
    symlink_or_skip(folder / "model.bin", real)
    with pytest.raises(ModelStoreError, match="link"):
        verify_model(tmp_path, "base.en")


def test_a_symlinked_model_folder_is_refused(tmp_path: Path) -> None:
    make_model(tmp_path / "real")
    link = tmp_path / "models"
    link.mkdir()
    symlink_or_skip(link / "base.en", tmp_path / "real" / "base.en")
    (link / MS.LOCK_NAME).write_bytes((tmp_path / "real" / MS.LOCK_NAME).read_bytes())
    with pytest.raises(ModelStoreError, match="not a plain folder"):
        verify_model(link, "base.en")


@pytest.mark.skipif(os.name != "nt", reason="junctions exist on Windows only")
def test_a_junction_is_refused_as_a_link(tmp_path: Path) -> None:
    import subprocess

    make_model(tmp_path / "real")
    link = tmp_path / "models"
    link.mkdir()
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link / "base.en"), str(tmp_path / "real" / "base.en")],
                          capture_output=True, text=True)
    if done.returncode != 0:
        pytest.skip("cannot create a junction here")
    (link / MS.LOCK_NAME).write_bytes((tmp_path / "real" / MS.LOCK_NAME).read_bytes())
    with pytest.raises(ModelStoreError, match="not a plain folder"):
        verify_model(link, "base.en")


@pytest.mark.parametrize("payload", [b"\x80\x04\x95" + b"\0" * 64, b"\x80\x02}q\x00.", b"PK\x03\x04" + b"\0" * 64, b"\x1f\x8b\x08" + b"\0" * 64])
def test_a_planted_pickle_or_archive_named_model_bin_is_refused_even_with_a_matching_lock(tmp_path: Path, payload: bytes) -> None:
    files = dict(GOOD_FILES, **{"model.bin": payload})
    folder = make_model(tmp_path, files=files)  # the lock was made from the planted file: only the content check can stop it
    with pytest.raises(ModelStoreError, match="pickle or an archive"):
        check_folder(folder)
    with pytest.raises(ModelStoreError, match="pickle or an archive"):
        verify_model(tmp_path, "base.en")


@pytest.mark.parametrize("name", ["config.json", "tokenizer.json"])
@pytest.mark.parametrize("data", [b"not json", b"\xff\xfe", b"123", b'"text"', b"{" * 100_000, b""])
def test_a_json_file_that_is_not_json_is_refused(tmp_path: Path, name: str, data: bytes) -> None:
    folder = make_model(tmp_path, files=dict(GOOD_FILES, **{name: data or b" "}), lock=False)
    with pytest.raises(ModelStoreError):
        check_folder(folder)


def test_a_json_settings_file_at_the_cap_is_read_and_one_byte_over_is_refused(tmp_path: Path) -> None:
    body = b'{"version": "1.0"}'
    folder = make_model(tmp_path, lock=False)
    (folder / "tokenizer.json").write_bytes(body + b" " * (STT_JSON_FILE_MAX_BYTES - len(body)))
    assert check_folder(folder)["tokenizer.json"] == STT_JSON_FILE_MAX_BYTES
    (folder / "tokenizer.json").write_bytes(body + b" " * (STT_JSON_FILE_MAX_BYTES - len(body) + 1))
    with pytest.raises(ModelStoreError, match="too large"):
        check_folder(folder)


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    folder = make_model(tmp_path, lock=False)
    (folder / "vocabulary.txt").write_bytes(b"")
    with pytest.raises(ModelStoreError, match="empty"):
        check_folder(folder)


def test_too_many_entries_is_refused(tmp_path: Path) -> None:
    folder = make_model(tmp_path, lock=False)
    for i in range(STT_MODEL_FILES_MAX):
        (folder / f"x{i}").write_bytes(b"1")
    with pytest.raises(ModelStoreError, match="entries"):
        check_folder(folder)


def test_the_file_size_cap_is_named_and_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert STT_MODEL_FILE_MAX_BYTES >= 484_000_000  # small.en model.bin fits
    monkeypatch.setattr(MS, "STT_MODEL_FILE_MAX_BYTES", 4000)
    folder = make_model(tmp_path, lock=False)
    with pytest.raises(ModelStoreError, match="refused"):
        check_folder(folder)


def test_a_model_that_is_not_in_the_lock_is_refused(tmp_path: Path) -> None:
    make_model(tmp_path, "base.en")
    make_model(tmp_path, "small.en", lock=False)
    with pytest.raises(ModelStoreError, match="not in lock.json"):
        verify_model(tmp_path, "small.en")


@pytest.mark.parametrize("name", ["", "../base.en", "base.en/..", "BASE", "a b", "x" * 40, "..", "C:\\x", "a\x00b"])
def test_a_bad_model_name_is_refused(tmp_path: Path, name: str) -> None:
    make_model(tmp_path)
    with pytest.raises(ModelStoreError):
        verify_model(tmp_path, name)


def test_a_missing_folder_or_missing_lock_is_refused_with_a_message(tmp_path: Path) -> None:
    with pytest.raises(ModelStoreError, match="not in lock.json"):
        verify_model(tmp_path / "nothing", "base.en")
    make_model(tmp_path)
    for p in (tmp_path / "base.en").iterdir():
        p.unlink()
    (tmp_path / "base.en").rmdir()
    with pytest.raises(ModelStoreError, match="does not exist"):
        verify_model(tmp_path, "base.en")


# ---- the lock file --------------------------------------------------------------------------------------------------------


def _write_lock(root: Path, text: str | bytes) -> None:
    (root / MS.LOCK_NAME).write_bytes(text if isinstance(text, bytes) else text.encode())


@pytest.mark.parametrize("text", [b"", b"not json", b"[]", b'{"schema": 2, "models": {}}', b'{"schema": 1}', b'{"schema": 1, "models": []}',
                                  b'{"schema": 1, "models": {"base.en": 5}}', b"\xff\xfe\x00", b"{" * 5000, b'{"schema": true, "models": {}}'])
def test_a_damaged_lock_is_refused_with_one_message(tmp_path: Path, text: bytes) -> None:
    make_model(tmp_path)
    _write_lock(tmp_path, text)
    with pytest.raises(ModelStoreError):
        verify_model(tmp_path, "base.en")


def _entry(**changes: Any) -> dict[str, Any]:
    e: dict[str, Any] = {"repo": "Systran/faster-whisper-base.en", "revision": "a" * 40, "fetched": "2026-01-01",
                         "files": {"config.json": {"size": 5, "sha256": "b" * 64, "verified_from": "hub", "hub_id": "c" * 40}}}
    e.update(changes)
    return e


@pytest.mark.parametrize("changes", [
    {"revision": "main"}, {"revision": "A" * 40}, {"repo": 5}, {"fetched": None}, {"files": {}}, {"files": []},
    {"files": {"evil.pt": {"size": 5, "sha256": "b" * 64, "verified_from": "hub"}}},
    {"files": {"config.json": {"size": -1, "sha256": "b" * 64, "verified_from": "hub"}}},
    {"files": {"config.json": {"size": True, "sha256": "b" * 64, "verified_from": "hub"}}},
    {"files": {"config.json": {"size": 10**12, "sha256": "b" * 64, "verified_from": "hub"}}},
    {"files": {"config.json": {"size": 5, "sha256": "xyz", "verified_from": "hub"}}},
    {"files": {"config.json": {"size": 5, "sha256": "b" * 64, "verified_from": "magic"}}},
    {"files": {"config.json": {"size": 5, "sha256": "b" * 64, "verified_from": "hub", "hub_id": 7}}}])
def test_a_lock_entry_with_a_bad_field_is_refused(tmp_path: Path, changes: dict[str, Any]) -> None:
    _write_lock(tmp_path, json.dumps({"schema": 1, "models": {"base.en": _entry(**changes)}}))
    with pytest.raises(ModelStoreError):
        read_lock(tmp_path)


def test_a_huge_lock_is_refused_before_parsing(tmp_path: Path) -> None:
    _write_lock(tmp_path, b" " * (STT_LOCK_MAX_BYTES + 1))
    with pytest.raises(ModelStoreError, match="larger than"):
        read_lock(tmp_path)


def test_a_lock_at_the_limit_is_read(tmp_path: Path) -> None:
    body = json.dumps({"schema": 1, "models": {}})
    _write_lock(tmp_path, body + " " * (STT_LOCK_MAX_BYTES - len(body)))
    assert read_lock(tmp_path) == {}


def test_a_missing_lock_means_no_models(tmp_path: Path) -> None:
    assert read_lock(tmp_path) == {}


def test_lock_writes_are_atomic_and_keep_other_models(tmp_path: Path) -> None:
    make_model(tmp_path, "base.en")
    make_model(tmp_path, "small.en")
    assert set(read_lock(tmp_path)) == {"base.en", "small.en"}
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


# ---- the fetch tool: its checks, run on a fake hub --------------------------------------------------------------------


def _hub(files: dict[str, bytes], revision: str = "d" * 40, *, lie_hash: bool = False, lie_size: bool = False) -> tuple[Any, Any, list[tuple[str, str]]]:
    """(resolver, downloader, log) for a fake hub serving `files`."""
    calls: list[tuple[str, str]] = []

    def blob_id(data: bytes) -> str:
        return hashlib.sha1(f"blob {len(data)}\0".encode() + data, usedforsecurity=False).hexdigest()

    def resolve(repo: str) -> F.HubInfo:
        out: dict[str, F.HubFile] = {}
        for name, data in files.items():
            lfs = sha(data) if name == "model.bin" else None
            out[name] = F.HubFile(len(data) + (1 if lie_size else 0), blob_id(data), ("0" * 64) if (lie_hash and lfs) else lfs)
        return F.HubInfo(revision, out)

    def download(repo: str, filename: str, rev: str, folder: Path) -> None:
        calls.append((filename, rev))
        (folder / filename).write_bytes(files[filename])

    return resolve, download, calls


def _fetch(root: Path, files: dict[str, bytes] | None = None, *, force: bool = False, **hub: Any) -> tuple[str, list[str], list[tuple[str, str]]]:
    resolve, download, calls = _hub(GOOD_FILES if files is None else files, **hub)
    said: list[str] = []
    result = F.fetch_model("base.en", root, force=force, resolve=resolve, download=download, out=said.append)
    return result, said, calls


def _leftovers(root: Path) -> list[str]:
    return [p.name for p in root.iterdir() if p.name.startswith((".incoming", ".old"))]


def test_a_good_fetch_installs_pins_the_revision_and_writes_the_lock(tmp_path: Path) -> None:
    result, said, calls = _fetch(tmp_path)
    assert result == "installed" and {rev for _, rev in calls} == {"d" * 40}
    lock = read_lock(tmp_path)["base.en"]
    assert lock.revision == "d" * 40 and lock.repo == "Systran/faster-whisper-base.en"
    assert all(lf.verified_from == "hub" for lf in lock.files.values()) and lock.files["model.bin"].hub_id == sha(GOOD_FILES["model.bin"])
    assert lock.files["config.json"].sha256 == sha(GOOD_FILES["config.json"])
    assert verify_model(tmp_path, "base.en") and _leftovers(tmp_path) == [] and any("pinned revision" in s for s in said)


def test_the_download_goes_into_a_new_temporary_folder_not_the_target(tmp_path: Path) -> None:
    seen: list[Path] = []
    resolve, download, _ = _hub(GOOD_FILES)

    def spy(repo: str, filename: str, rev: str, folder: Path) -> None:
        seen.append(folder)
        assert folder != tmp_path / "base.en" and folder.parent == tmp_path and not (tmp_path / "base.en").exists()
        download(repo, filename, rev, folder)

    F.fetch_model("base.en", tmp_path, force=False, resolve=resolve, download=spy, out=lambda s: None)
    assert len(set(seen)) == 1 and not seen[0].exists()


def test_a_second_fetch_does_not_overwrite_a_verified_model(tmp_path: Path) -> None:
    _fetch(tmp_path)
    before = (tmp_path / "base.en" / "model.bin").stat().st_mtime_ns
    result, _, calls = _fetch(tmp_path)
    assert result == "already installed" and calls == [] and (tmp_path / "base.en" / "model.bin").stat().st_mtime_ns == before


def test_force_replaces_it(tmp_path: Path) -> None:
    _fetch(tmp_path)
    newer = dict(GOOD_FILES, **{"vocabulary.txt": b"new\nwords\n"})
    result, _, _ = _fetch(tmp_path, newer, force=True, revision="e" * 40)
    assert result == "installed" and read_lock(tmp_path)["base.en"].revision == "e" * 40
    assert (tmp_path / "base.en" / "vocabulary.txt").read_bytes() == b"new\nwords\n" and _leftovers(tmp_path) == []


def test_a_damaged_installed_model_is_not_silently_replaced(tmp_path: Path) -> None:
    _fetch(tmp_path)
    (tmp_path / "base.en" / "config.json").write_bytes(b'{"x": 1}')
    with pytest.raises(ModelStoreError, match="--force"):
        _fetch(tmp_path)
    assert (tmp_path / "base.en" / "config.json").read_bytes() == b'{"x": 1}'


def test_a_hash_mismatch_with_the_hub_installs_nothing(tmp_path: Path) -> None:
    with pytest.raises(ModelStoreError, match="LFS hash"):
        _fetch(tmp_path, lie_hash=True)
    assert not (tmp_path / "base.en").exists() and read_lock(tmp_path) == {} and _leftovers(tmp_path) == []


def test_a_size_mismatch_truncated_download_installs_nothing(tmp_path: Path) -> None:
    with pytest.raises(ModelStoreError, match="truncated or replaced"):
        _fetch(tmp_path, lie_size=True)
    assert not (tmp_path / "base.en").exists() and _leftovers(tmp_path) == []


def test_a_planted_pickle_named_model_bin_is_refused_by_the_fetch_even_if_the_hub_hash_matches(tmp_path: Path) -> None:
    bad = dict(GOOD_FILES, **{"model.bin": b"\x80\x04\x95" + b"\0" * 200})
    with pytest.raises(ModelStoreError, match="pickle or an archive"):
        _fetch(tmp_path, bad)
    assert not (tmp_path / "base.en").exists() and read_lock(tmp_path) == {} and _leftovers(tmp_path) == []


def test_an_unexpected_extra_file_in_the_download_is_refused(tmp_path: Path) -> None:
    resolve, download, _ = _hub(GOOD_FILES)

    def evil(repo: str, filename: str, rev: str, folder: Path) -> None:
        download(repo, filename, rev, folder)
        (folder / "payload.pt").write_bytes(b"\x80\x04")

    with pytest.raises(ModelStoreError, match="unexpected entry"):
        F.fetch_model("base.en", tmp_path, force=False, resolve=resolve, download=evil, out=lambda s: None)
    assert not (tmp_path / "base.en").exists() and _leftovers(tmp_path) == []


def test_a_symlink_in_the_download_is_refused(tmp_path: Path) -> None:
    resolve, download, _ = _hub(GOOD_FILES)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    def evil(repo: str, filename: str, rev: str, folder: Path) -> None:
        if filename == "vocabulary.txt":
            symlink_or_skip(folder / filename, outside)
        else:
            download(repo, filename, rev, folder)

    with pytest.raises(ModelStoreError, match="link"):
        F.fetch_model("base.en", tmp_path / "root", force=False, resolve=resolve, download=evil, out=lambda s: None)
    assert not (tmp_path / "root" / "base.en").exists()


def test_a_repo_without_a_required_file_is_refused_before_any_download(tmp_path: Path) -> None:
    files = {k: v for k, v in GOOD_FILES.items() if k != "tokenizer.json"}
    resolve, download, calls = _hub(files)
    with pytest.raises(ModelStoreError, match="no tokenizer.json"):
        F.fetch_model("base.en", tmp_path, force=False, resolve=resolve, download=download, out=lambda s: None)
    assert calls == [] and _leftovers(tmp_path) == []


def test_a_hub_file_over_the_size_cap_is_refused_before_download(tmp_path: Path) -> None:
    info = F.HubInfo("d" * 40, {n: F.HubFile(10, "e" * 40, None) for n in GOOD_FILES})
    info.files["model.bin"] = F.HubFile(STT_MODEL_FILE_MAX_BYTES + 1, "e" * 40, "f" * 64)
    with pytest.raises(ModelStoreError, match="cap"):
        F.fetch_model("base.en", tmp_path, force=False, resolve=lambda r: info, download=lambda *a: None, out=lambda s: None)


def test_a_failed_move_keeps_the_old_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fetch(tmp_path)
    resolve, download, _ = _hub(dict(GOOD_FILES, **{"vocabulary.txt": b"changed\n"}), revision="e" * 40)
    real_replace = os.replace
    n = {"calls": 0}

    def flaky(src: Any, dst: Any) -> None:
        n["calls"] += 1
        if n["calls"] == 2:  # the move of the new folder into place
            raise PermissionError("locked")
        real_replace(src, dst)

    monkeypatch.setattr(F.os, "replace", flaky)
    with pytest.raises(ModelStoreError):
        F.fetch_model("base.en", tmp_path, force=True, resolve=resolve, download=download, out=lambda s: None)
    monkeypatch.undo()
    assert verify_model(tmp_path, "base.en").revision == "d" * 40 and _leftovers(tmp_path) == []


def test_the_fetch_tool_only_asks_for_the_known_files(tmp_path: Path) -> None:
    files = dict(GOOD_FILES, **{"preprocessor_config.json": b"{}", "README.md": b"hi", ".gitattributes": b"*"})
    resolve, download, calls = _hub(files)
    real = resolve

    def with_extras(repo: str) -> F.HubInfo:  # the real resolver already drops files outside ALLOWED_FILES; so does _wanted
        info = real(repo)
        return F.HubInfo(info.revision, {k: v for k, v in info.files.items() if k in MS.ALLOWED_FILES})

    F.fetch_model("base.en", tmp_path, force=False, resolve=with_extras, download=download, out=lambda s: None)
    assert sorted(n for n, _ in calls) == sorted(["config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "preprocessor_config.json"])


def test_the_tool_cli_refuses_an_unknown_model_with_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert F.main(["../etc/passwd"]) == 2
    assert F.main(["huge.en"]) == 2
    assert "unknown model" in capsys.readouterr().err


def test_the_tool_list_shows_state_without_the_network(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fetch(tmp_path)
    assert F.main(["--list", "--dest", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "base.en: verified" in out and "small.en: not installed" in out
    (tmp_path / "base.en" / "config.json").write_bytes(b"{}")
    assert F.main(["--list", "--dest", str(tmp_path)]) == 1


# ---- the WAV reader ---------------------------------------------------------------------------------------------------------


def wav(path: Path, pcm: bytes, *, rate: int = 16000, channels: int = 1, width: int = 2) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return path


def test_a_good_wav_is_read(tmp_path: Path) -> None:
    pcm = struct.pack("<8000h", *range(8000))
    assert read_wav_pcm16k(wav(tmp_path / "a.wav", pcm)) == pcm


def test_exactly_15_seconds_is_read_and_one_sample_more_is_refused(tmp_path: Path) -> None:
    ok = bytes(UTTERANCE_MAX_S * 16000 * 2)
    assert len(read_wav_pcm16k(wav(tmp_path / "a.wav", ok))) == len(ok)
    with pytest.raises(WavError, match="longer than 15 s"):
        read_wav_pcm16k(wav(tmp_path / "b.wav", ok + b"\0\0"))


@pytest.mark.parametrize("kw,match", [({"rate": 44100}, "44100 Hz"), ({"rate": 8000}, "8000 Hz"), ({"channels": 2}, "2 channels"),
                                      ({"width": 1}, "8-bit"), ({"width": 4}, "32-bit")])
def test_a_wav_in_the_wrong_format_is_refused_with_a_clear_message(tmp_path: Path, kw: dict[str, int], match: str) -> None:
    width = kw.get("width", 2)
    with pytest.raises(WavError, match=match):
        read_wav_pcm16k(wav(tmp_path / "a.wav", bytes(3200 * width * kw.get("channels", 1)), **kw))


def test_a_wav_with_no_audio_is_refused(tmp_path: Path) -> None:
    with pytest.raises(WavError, match="no audio"):
        read_wav_pcm16k(wav(tmp_path / "a.wav", b""))


def test_a_missing_file_a_folder_and_a_huge_file_are_refused(tmp_path: Path) -> None:
    with pytest.raises(WavError, match="does not exist"):
        read_wav_pcm16k(tmp_path / "none.wav")
    with pytest.raises(WavError, match="not a regular file"):
        read_wav_pcm16k(tmp_path)
    big = tmp_path / "big.wav"
    big.write_bytes(b"RIFF" + b"\0" * (STT_WAV_MAX_BYTES + 1))
    with pytest.raises(WavError, match="refused"):
        read_wav_pcm16k(big)


@pytest.mark.parametrize("data", [b"", b"RIFF", b"RIFF" + b"\0" * 100, b"x" * 100, b"\0" * 100, b"RIFF\xff\xff\xff\xffWAVEfmt " + b"\xff" * 80,
                                  b"\x89PNG\r\n\x1a\n" + b"\0" * 100])
def test_a_file_that_is_not_a_wav_is_refused_with_one_message(tmp_path: Path, data: bytes) -> None:
    p = tmp_path / "a.wav"
    p.write_bytes(data)
    with pytest.raises(WavError):
        read_wav_pcm16k(p)


def test_a_truncated_wav_is_refused(tmp_path: Path) -> None:
    good = wav(tmp_path / "a.wav", bytes(32000)).read_bytes()
    p = tmp_path / "t.wav"
    p.write_bytes(good[:-5000])
    with pytest.raises(WavError):
        read_wav_pcm16k(p)


def test_a_header_that_promises_gigabytes_does_not_allocate_them(tmp_path: Path) -> None:
    good = bytearray(wav(tmp_path / "a.wav", bytes(3200)).read_bytes())
    good[40:44] = struct.pack("<I", 0xFFFFFFF0)  # the data chunk size
    p = tmp_path / "h.wav"
    p.write_bytes(bytes(good))
    with pytest.raises(WavError):
        read_wav_pcm16k(p)


def test_a_path_with_odd_text_never_raises_anything_but_waverror() -> None:
    for p in ["", ".", "\x00", "a" * 5000, "CON", "NUL", "//", "C:\\Windows\\System32\\drivers\\etc", "\u202e.wav"]:
        with pytest.raises(WavError):
            read_wav_pcm16k(p)
