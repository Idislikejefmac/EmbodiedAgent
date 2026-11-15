"""The local speech model store: what a model folder may hold and how it is checked against its lock.

    data/models/stt/<name>/            config.json, model.bin, tokenizer.json, vocabulary.txt (+ preprocessor_config.json)
    data/models/stt/lock.json          {"schema": 1, "models": {name: {repo, revision, fetched, files: {file: {...}}}}}

Two users: tools/fetch_stt_model.py (the one-time download, which checks a NEW folder before moving it into
place) and the engine (brain/voice/stt.py), which verifies a model against lock.json before it loads one.

The lock is an integrity check against damage and accident (a half-copied file, an edited file, a model
swapped by hand), and it fails closed: an unlisted or changed model is refused with a message that says why.
It is not a defence against someone who can already write to this folder (they could edit the lock too).

Never executes or unpickles anything: this module reads sizes, bytes and JSON only. Raises ModelStoreError
(a ValueError) with ONE message. Hash work is cached by (size, modification time) in `.verified.json` next
to the lock, so a start does not re-hash 500 MB each time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..limits import (STT_JSON_FILE_MAX_BYTES, STT_LOCK_MAX_BYTES, STT_MODEL_FILE_MAX_BYTES, STT_MODEL_FILES_MAX)

MODELS: dict[str, str] = {"base.en": "Systran/faster-whisper-base.en", "small.en": "Systran/faster-whisper-small.en"}
REQUIRED_FILES = ("config.json", "model.bin", "tokenizer.json")
VOCABULARY_FILES = ("vocabulary.txt", "vocabulary.json")  # one of them
OPTIONAL_FILES = ("preprocessor_config.json",)
ALLOWED_FILES = frozenset(REQUIRED_FILES + VOCABULARY_FILES + OPTIONAL_FILES)
JSON_FILES = ("config.json", "tokenizer.json", "preprocessor_config.json", "vocabulary.json")
LOCK_NAME = "lock.json"
CACHE_NAME = ".verified.json"
LOCK_SCHEMA = 1
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: a Windows link or junction
_BAD_EXTENSIONS = (".pt", ".pth", ".pkl", ".pickle", ".ckpt", ".npy", ".npz", ".joblib", ".dll", ".exe", ".py", ".so")


class ModelStoreError(ValueError):
    """A model folder or lock cannot be used. `str(e)` is the one message to show."""


def _plain(name: str) -> str:
    return name if name.isprintable() and len(name) <= 60 else repr(name[:60])


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def git_blob_sha1_of(path: Path, size: int) -> str:
    """The hub's recorded id of a small (non-LFS) file: SHA-1 of 'blob <size>\\0' plus the bytes (git's blob id)."""
    h = hashlib.sha1(f"blob {size}\0".encode("ascii"), usedforsecurity=False)  # an id the hub publishes, not a secret
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def is_link(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _looks_like_pickle_or_archive(head: bytes) -> bool:
    """A pickle (protocol 2 to 5 starts with 0x80 and the protocol), a zip (a torch .pt file) or a gzip/tar head.
    A CTranslate2 model.bin starts with its small binary version number, never with these bytes."""
    if head[:1] == b"\x80" and len(head) > 1 and head[1] in (2, 3, 4, 5):
        return True
    return head[:4] in (b"PK\x03\x04", b"PK\x05\x06") or head[:2] == b"\x1f\x8b"


def check_folder(folder: Path) -> dict[str, int]:
    """Refuse a model folder that holds anything but the expected plain files; return {file name: size}.

    Refused: a folder that is a link or not a folder; any entry that is a directory, link, junction or not a
    regular file; any name outside ALLOWED_FILES (this also covers .pt, .pkl, .pickle and a second .bin); an empty
    or over-cap file; a model.bin that starts like a pickle or an archive; a .json file that is not a JSON object;
    a missing required file; no vocabulary file."""
    try:
        st = os.lstat(folder)
    except OSError:
        raise ModelStoreError(f"model folder {_plain(str(folder))} does not exist or cannot be read") from None
    if is_link(st) or not stat.S_ISDIR(st.st_mode):
        raise ModelStoreError(f"model folder {_plain(str(folder))} is not a plain folder (a link or a file)")
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        raise ModelStoreError(f"model folder {_plain(str(folder))} cannot be listed") from None
    if len(names) > STT_MODEL_FILES_MAX:
        raise ModelStoreError(f"model folder holds {len(names)} entries; at most {STT_MODEL_FILES_MAX} are expected")
    sizes: dict[str, int] = {}
    for name in names:
        low = name.lower()
        if name not in ALLOWED_FILES:
            why = "a pickle or other code-carrying file type" if low.endswith(_BAD_EXTENSIONS) else "not part of a model"
            raise ModelStoreError(f"unexpected entry {_plain(name)!s} in the model folder ({why}); refusing")
        try:
            fst = os.lstat(folder / name)
        except OSError:
            raise ModelStoreError(f"{name} cannot be read") from None
        if is_link(fst) or not stat.S_ISREG(fst.st_mode):
            raise ModelStoreError(f"{name} is a link or not a regular file; refusing")
        if fst.st_size == 0:
            raise ModelStoreError(f"{name} is empty")
        if fst.st_size > STT_MODEL_FILE_MAX_BYTES:
            raise ModelStoreError(f"{name} is {fst.st_size} bytes; more than {STT_MODEL_FILE_MAX_BYTES} is refused")
        sizes[name] = fst.st_size
    for name in REQUIRED_FILES:
        if name not in sizes:
            raise ModelStoreError(f"required file {name} is missing from the model folder")
    if not any(v in sizes for v in VOCABULARY_FILES):
        raise ModelStoreError("the model folder has no vocabulary file (vocabulary.txt or vocabulary.json)")
    try:
        with open(folder / "model.bin", "rb") as f:
            head = f.read(16)
    except OSError:
        raise ModelStoreError("model.bin cannot be read") from None
    if _looks_like_pickle_or_archive(head):
        raise ModelStoreError("model.bin starts like a pickle or an archive, not a CTranslate2 model; refusing")
    for name in JSON_FILES:
        if name in sizes:
            if sizes[name] > STT_JSON_FILE_MAX_BYTES:
                raise ModelStoreError(f"{name} is too large to be a settings file")
            try:
                value = json.loads((folder / name).read_bytes().decode("utf-8"))
            except (OSError, ValueError, RecursionError):
                raise ModelStoreError(f"{name} is not valid JSON") from None
            if not isinstance(value, (dict, list)):
                raise ModelStoreError(f"{name} is not a JSON object or list")
    return sizes


# ---- the lock -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LockedFile:
    size: int
    sha256: str
    verified_from: str  # "hub": matched the hub's recorded hash; "first_seen": computed here, nothing to compare with
    hub_id: str | None  # the hub's recorded id (LFS sha256 or git blob sha1), when there was one


@dataclass(frozen=True)
class LockedModel:
    name: str
    repo: str
    revision: str
    fetched: str
    files: dict[str, LockedFile]


class LockTable(dict[str, LockedModel]):
    """{model name: LockedModel}: what lock.json holds."""


def _read_json_capped(path: Path, limit: int, what: str) -> Any:
    try:
        with open(path, "rb") as f:
            data = f.read(limit + 1)
    except FileNotFoundError:
        raise ModelStoreError(f"{what} not found: {_plain(str(path))}") from None
    except OSError as e:
        raise ModelStoreError(f"{what} cannot be read ({e.strerror or type(e).__name__})") from None
    if len(data) > limit:
        raise ModelStoreError(f"{what} is larger than {limit} bytes")
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (ValueError, RecursionError):
        raise ModelStoreError(f"{what} is not valid JSON") from None


def _entry(name: str, raw: object) -> LockedModel:
    def bad(why: str) -> ModelStoreError:
        return ModelStoreError(f"{LOCK_NAME}: model {_plain(name)}: {why}")

    if not isinstance(raw, dict):
        raise bad("not an object")
    repo, revision, fetched, files = raw.get("repo"), raw.get("revision"), raw.get("fetched"), raw.get("files")
    if not isinstance(repo, str) or not 1 <= len(repo) <= 100:
        raise bad("repo must be text")
    if not isinstance(revision, str) or not _HEX40.match(revision):
        raise bad("revision must be a 40 character commit id")
    if not isinstance(fetched, str) or len(fetched) > 40:
        raise bad("fetched must be a date")
    if not isinstance(files, dict) or not 1 <= len(files) <= STT_MODEL_FILES_MAX:
        raise bad("files must be a table of the model's files")
    out: dict[str, LockedFile] = {}
    for fname, info in files.items():
        if fname not in ALLOWED_FILES or not isinstance(info, dict):
            raise bad(f"unexpected file entry {_plain(str(fname))}")
        size, sha, src, hub = info.get("size"), info.get("sha256"), info.get("verified_from"), info.get("hub_id")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= STT_MODEL_FILE_MAX_BYTES:
            raise bad(f"{fname}: size is not a usable number")
        if not isinstance(sha, str) or not _HEX64.match(sha):
            raise bad(f"{fname}: sha256 must be 64 hex digits")
        if src not in ("hub", "first_seen"):
            raise bad(f"{fname}: verified_from must be 'hub' or 'first_seen'")
        if hub is not None and not (isinstance(hub, str) and (_HEX64.match(hub) or _HEX40.match(hub))):
            raise bad(f"{fname}: hub_id is not a hash")
        out[fname] = LockedFile(size, sha, src, hub)
    return LockedModel(name, repo, revision, fetched, out)


def read_lock(root: Path) -> LockTable:
    """{model name: LockedModel} from <root>/lock.json. A missing lock is an empty dict; a bad one raises."""
    path = root / LOCK_NAME
    if not path.exists():
        return LockTable()
    raw = _read_json_capped(path, STT_LOCK_MAX_BYTES, LOCK_NAME)
    if not isinstance(raw, dict) or raw.get("schema") != LOCK_SCHEMA or not isinstance(raw.get("models"), dict):
        raise ModelStoreError(f"{LOCK_NAME} is not a schema {LOCK_SCHEMA} lock file")
    models = raw["models"]
    return LockTable({str(n): _entry(str(n), e) for n, e in models.items() if _NAME.match(str(n))})


def _lock_to_json(models: LockTable) -> dict[str, Any]:
    return {"schema": LOCK_SCHEMA, "models": {
        n: {"repo": m.repo, "revision": m.revision, "fetched": m.fetched,
            "files": {f: {"size": lf.size, "sha256": lf.sha256, "verified_from": lf.verified_from, "hub_id": lf.hub_id}
                      for f, lf in sorted(m.files.items())}}
        for n, m in sorted(models.items())}}


def write_lock(root: Path, models: LockTable) -> None:
    """Replace lock.json atomically (a temporary file in the same folder, then os.replace)."""
    text = json.dumps(_lock_to_json(models), indent=1) + "\n"
    tmp = root / f".{LOCK_NAME}.{os.getpid()}.tmp"
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, root / LOCK_NAME)
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # swallowed: the temporary file is already unusable; the lock error below is the news
            pass
        raise ModelStoreError(f"{LOCK_NAME} cannot be written ({e.strerror or type(e).__name__})") from None


def _load_cache(root: Path) -> dict[str, list[Any]]:
    try:
        raw = _read_json_capped(root / CACHE_NAME, STT_LOCK_MAX_BYTES, CACHE_NAME)
    except ModelStoreError:
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, list)} if isinstance(raw, dict) else {}


def verify_model(root: Path, name: str) -> LockedModel:
    """Check <root>/<name>/ against lock.json: the folder holds exactly the locked files, each a plain file of
    the locked size and SHA-256. Returns the lock entry. Raises ModelStoreError with the reason otherwise.

    An unlisted model (no entry in the lock) is refused: run tools/fetch_stt_model.py."""
    if not _NAME.match(name):
        raise ModelStoreError(f"{_plain(name)!r} is not a usable model name")
    models = read_lock(root)
    locked = models.get(name)
    if locked is None:
        raise ModelStoreError(f"model {name} is not in {LOCK_NAME}; fetch it with tools/fetch_stt_model.py "
                              "(the brain only loads models that were fetched and locked)")
    folder = root / name
    sizes = check_folder(folder)
    for fname in sizes:
        if fname not in locked.files:
            raise ModelStoreError(f"{fname} is in the model folder but not in {LOCK_NAME}; refusing")
    for fname in locked.files:
        if fname not in sizes:
            raise ModelStoreError(f"{fname} is listed in {LOCK_NAME} but missing from the model folder")
    cache = _load_cache(root)
    changed = False
    for fname, lf in sorted(locked.files.items()):
        path = folder / fname
        st = os.lstat(path)
        if st.st_size != lf.size:
            raise ModelStoreError(f"{fname} is {st.st_size} bytes but the lock says {lf.size}; the file was changed "
                                  "or is incomplete (fetch the model again with --force)")
        key = f"{name}/{fname}"
        hit = cache.get(key)
        if hit == [st.st_size, st.st_mtime_ns, lf.sha256]:
            continue
        digest = sha256_of(path)
        if digest != lf.sha256:
            raise ModelStoreError(f"{fname} does not match its recorded SHA-256; the file was changed "
                                  "(fetch the model again with --force)")
        cache[key] = [st.st_size, st.st_mtime_ns, digest]
        changed = True
    if changed:
        try:
            (root / CACHE_NAME).write_text(json.dumps(cache), encoding="utf-8", newline="\n")
        except OSError:  # swallowed: the cache only saves time; the next start hashes again
            pass
    return locked
