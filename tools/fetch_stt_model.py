"""One-time, safe fetch of the local speech-to-text models (docs/stt.md). Run it OUTSIDE the brain:

    python tools/fetch_stt_model.py                 # base.en and small.en (use the project's .venv python)
    python tools/fetch_stt_model.py base.en         # one model
    python tools/fetch_stt_model.py --force         # replace a model that is already installed
    python tools/fetch_stt_model.py --list          # what is installed and verified

The brain itself never touches the network for speech to text; this is the only program that talks to
huggingface.co. Downloaded files are untrusted data, so for each model:

 1. the current commit of the repository is resolved and PINNED (every file is fetched at that exact revision);
 2. the files go into a NEW, EMPTY temporary folder next to the destination (same disk, so the final move is atomic);
 3. the folder is checked: only the expected plain files (config.json, model.bin, tokenizer.json, vocabulary.txt,
    preprocessor_config.json when present); no links, no sub-folders, no extra files, no .pt/.pkl/.pickle or a second
    .bin, no model.bin that starts like a pickle or archive, sizes under STT_MODEL_FILE_MAX_BYTES, JSON that parses;
 4. each file is hashed (SHA-256) and compared with what the hub recorded: the LFS SHA-256 for model.bin, git's blob
    id (SHA-1) for the small files. A file the hub has no hash for is recorded as 'first_seen';
 5. the folder is moved into place atomically and data/models/stt/lock.json is updated (name, repo, revision, per
    file sha256, size, how it was verified, date). An installed, verified model is never replaced without --force.

Nothing downloaded is ever executed, imported or unpickled. Exit code 0: done; 1: a model could not be fetched or
failed a check (nothing is installed for it); 2: bad arguments.
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from brain.limits import STT_MODEL_FILE_MAX_BYTES  # noqa: E402
from brain.voice.modelstore import (ALLOWED_FILES, MODELS, OPTIONAL_FILES, REQUIRED_FILES, VOCABULARY_FILES,  # noqa: E402
                                    LockedFile, LockedModel, ModelStoreError, check_folder, git_blob_sha1_of, read_lock,
                                    sha256_of, verify_model, write_lock)

DEFAULT_ROOT = REPO / "data" / "models" / "stt"


@dataclass(frozen=True)
class HubFile:
    size: int
    blob_id: str  # git blob id (SHA-1) of the file
    lfs_sha256: str | None  # for large files, the hub's SHA-256


@dataclass(frozen=True)
class HubInfo:
    revision: str
    files: dict[str, HubFile]


Resolver = Callable[[str], HubInfo]
Downloader = Callable[[str, str, str, Path], None]  # repo, file name, revision, destination folder


def hub_resolve(repo: str) -> HubInfo:
    from huggingface_hub import HfApi  # lazy: only this tool needs it

    info = HfApi().model_info(repo, files_metadata=True, token=False)
    files: dict[str, HubFile] = {}
    for s in info.siblings or []:
        if s.rfilename in ALLOWED_FILES and s.size is not None and s.blob_id:
            files[s.rfilename] = HubFile(int(s.size), str(s.blob_id), s.lfs.sha256 if s.lfs else None)
    if not info.sha:
        raise ModelStoreError(f"{repo}: the hub did not report a commit id")
    return HubInfo(str(info.sha), files)


def hub_download(repo: str, filename: str, revision: str, folder: Path) -> None:
    from huggingface_hub import hf_hub_download

    hf_hub_download(repo, filename, revision=revision, local_dir=str(folder), token=False)


def _wanted(info: HubInfo, repo: str) -> list[str]:
    """The files to fetch, from what the hub lists; refuses a repo that lacks a required file."""
    for need in REQUIRED_FILES:
        if need not in info.files:
            raise ModelStoreError(f"{repo} has no {need} at revision {info.revision[:12]}")
    vocab = [v for v in VOCABULARY_FILES if v in info.files]
    if not vocab:
        raise ModelStoreError(f"{repo} has no vocabulary file")
    names = list(REQUIRED_FILES) + vocab[:1] + [o for o in OPTIONAL_FILES if o in info.files]
    for n in names:
        if info.files[n].size > STT_MODEL_FILE_MAX_BYTES:
            raise ModelStoreError(f"{repo}: {n} is {info.files[n].size} bytes, over the {STT_MODEL_FILE_MAX_BYTES} cap")
    return names


def lock_entry(name: str, repo: str, info: HubInfo, folder: Path, now: str) -> LockedModel:
    """Hash every file in the checked folder and compare with the hub's record. Raises ModelStoreError on a mismatch."""
    sizes = check_folder(folder)
    files: dict[str, LockedFile] = {}
    for fname, size in sizes.items():
        hub = info.files.get(fname)
        digest = sha256_of(folder / fname)
        if hub is None:
            raise ModelStoreError(f"{fname} was downloaded but is not in the hub's file list; refusing")
        if size != hub.size:
            raise ModelStoreError(f"{fname} is {size} bytes but the hub lists {hub.size}: truncated or replaced; refusing")
        if hub.lfs_sha256 is not None:
            if digest != hub.lfs_sha256:
                raise ModelStoreError(f"{fname}: SHA-256 differs from the hub's recorded LFS hash; refusing")
            files[fname] = LockedFile(size, digest, "hub", hub.lfs_sha256)
        else:
            blob = git_blob_sha1_of(folder / fname, size)
            if blob != hub.blob_id:
                raise ModelStoreError(f"{fname}: git blob id differs from the hub's record; refusing")
            files[fname] = LockedFile(size, digest, "hub", hub.blob_id)
    return LockedModel(name, repo, info.revision, now, files)


def fetch_model(name: str, root: Path, *, force: bool, resolve: Resolver = hub_resolve,
                download: Downloader = hub_download, out: Callable[[str], None] = print) -> str:
    """Fetch one model into root/<name>/. Returns 'installed' or 'already installed'. Raises ModelStoreError."""
    repo = MODELS[name]
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    if target.exists() or target.is_symlink():
        try:
            verify_model(root, name)
            if not force:
                out(f"{name}: already installed and verified (use --force to fetch it again)")
                return "already installed"
        except ModelStoreError as e:
            if not force:
                raise ModelStoreError(f"{name}: an existing folder failed its check ({e}); nothing was changed. "
                                      "Use --force to replace it") from None
    out(f"{name}: resolving the current revision of {repo} ...")
    info = resolve(repo)
    names = _wanted(info, repo)
    out(f"{name}: pinned revision {info.revision}")
    tmp = Path(tempfile.mkdtemp(prefix=f".incoming-{name}-", dir=root))  # a NEW, EMPTY folder; same disk as the target
    old: Path | None = None
    try:
        for fname in names:
            out(f"{name}: downloading {fname} ({info.files[fname].size / 1e6:.1f} MB) ...")
            download(repo, fname, info.revision, tmp)
        shutil.rmtree(tmp / ".cache", ignore_errors=True)  # the hub library's own bookkeeping, not model data
        now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = lock_entry(name, repo, info, tmp, now)
        for fname, lf in sorted(entry.files.items()):
            out(f"{name}:   {fname:26s} {lf.size:>12,d} bytes  sha256 {lf.sha256[:16]}...  verified from {lf.verified_from}")
        if target.exists() or target.is_symlink():
            old = root / f".old-{name}-{os.getpid()}"
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
            os.replace(target, old)
        try:
            os.replace(tmp, target)  # atomic: same disk, target is gone
        except OSError:
            if old is not None:
                os.replace(old, target)
                old = None
            raise
        locks = read_lock(root)
        locks[name] = entry
        write_lock(root, locks)
        out(f"{name}: installed in {target}")
        return "installed"
    except OSError as e:
        raise ModelStoreError(f"{name}: a file operation failed ({e.strerror or type(e).__name__}); nothing was installed") from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)


def listing(root: Path, out: Callable[[str], None] = print) -> int:
    try:
        locks = read_lock(root)
    except ModelStoreError as e:
        out(f"lock.json: {e}")
        return 1
    bad = 0
    for name in MODELS:
        if name not in locks:
            out(f"{name}: not installed")
            continue
        try:
            m = verify_model(root, name)
            first = sorted(f for f, lf in m.files.items() if lf.verified_from != "hub")
            out(f"{name}: verified; revision {m.revision[:12]}, fetched {m.fetched}"
                + (f"; first seen (no hub hash): {', '.join(first)}" if first else "; every hash matched the hub"))
        except ModelStoreError as e:
            bad += 1
            out(f"{name}: FAILED its check: {e}")
    return 1 if bad else 0


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="One-time, verified download of the local speech-to-text models.")
    p.add_argument("models", nargs="*", help=f"model names ({', '.join(MODELS)}); default: all")
    p.add_argument("--force", action="store_true", help="replace a model that is already installed")
    p.add_argument("--list", action="store_true", help="show what is installed and verified; download nothing")
    p.add_argument("--dest", type=Path, default=DEFAULT_ROOT, help="the model folder (default: data/models/stt)")
    args = p.parse_args(argv)
    for m in args.models:
        if m not in MODELS:
            print(f"unknown model {m[:40]!r}; choose from {', '.join(MODELS)}", file=sys.stderr)
            return 2
    if args.list:
        return listing(args.dest)
    failed = 0
    for name in args.models or list(MODELS):
        try:
            fetch_model(name, args.dest, force=args.force)
        except ModelStoreError as e:
            failed += 1
            print(f"{name}: FAILED: {e}", file=sys.stderr)
        except Exception as e:  # broad-except: a network or library failure ends this model, not the run; one line, no traceback
            failed += 1
            print(f"{name}: FAILED: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
    print("summary:")
    listing(args.dest)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
