"""Move the project's private data to another machine, encrypted.

    python tools/migrate.py export OUT.eamig
    python tools/migrate.py verify OUT.eamig                    # decrypt and check, extract nothing
    python tools/migrate.py restore OUT.eamig --dest NEWFOLDER  # refuses a non-empty NEWFOLDER
    python tools/migrate.py restore OUT.eamig --dest NEWFOLDER --force   # lists what it replaces

Bundles data/, .env and config.toml into one archive encrypted with a passphrase
(scrypt key derivation, AES-256-GCM). See docs/migration.md for the full
procedure, including the secure-delete checklist for the old machine.

Restore and verify refuse, before spending memory or time on it (H-01): an archive larger than
ARCHIVE_MAX_BYTES (checked before it is read), a header asking for any key-derivation cost other than
the SCRYPT_LOG_N that export writes (checked before scrypt runs), more than ARCHIVE_MEMBERS_MAX entries,
more than ARCHIVE_UNPACKED_MAX_BYTES unpacked, paths that leave the destination, and links, devices or
other special entries. Restore never silently overwrites (H-33): a non-empty destination needs
--force, and then every existing path the archive replaces is listed. Deletion records: entries listed
in the destination's data/deletions.log are not re-created (see DELETION_LOG below).

The whole archive is built in memory, which is fine at trial scale. If
recordings grow to many gigabytes, switch to a streaming format.
"""

from __future__ import annotations

import argparse
import getpass
import io
import os
import sys
import tarfile
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

if str(Path(__file__).resolve().parent.parent) not in sys.path:  # run as `python tools/migrate.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain.limits import (ARCHIVE_MAX_BYTES, ARCHIVE_MEMBERS_MAX, ARCHIVE_UNPACKED_MAX_BYTES,  # noqa: E402
                          PASSPHRASE_MIN_CHARS, SCRYPT_LOG_N)

MAGIC = b"EAMIG2"
ITEMS = ("data", ".env", "config.toml")
MIN_PASSPHRASE = PASSPHRASE_MIN_CHARS  # older name, kept for callers
HEADER_LEN = len(MAGIC) + 1 + 16 + 12  # magic, log2(N), salt, nonce
TAG_LEN = 16  # AES-GCM tag

# Deletion records (H-33, doc 00 section 16). TODO(P3-01, P4-08): the identity and memory stores will keep
# their own deletion log outside the restorable set and re-apply it before the brain starts; until then
# this is the hook: one path per line, relative to the destination (for example `data/people/ann`), '#'
# for comments. Restore skips every archive entry at or under a listed path, so data deleted after the
# archive was made is not brought back, and the destination's log is kept (the archive's copy is merged).
DELETION_LOG = "data/deletions.log"
DELETION_LOG_MAX_BYTES = 1024 * 1024  # a log larger than this is refused rather than half-read


def _key(passphrase: str, salt: bytes, log_n: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**log_n, r=8, p=1).derive(
        passphrase.encode("utf-8")
    )


def _refuse_links(info: tarfile.TarInfo) -> tarfile.TarInfo:
    if info.issym() or info.islnk():
        raise ValueError(
            f"Refusing to export a link ({info.name}); replace it with the real file"
        )
    return info


def build_archive(root: Path, items: tuple[str, ...] = ITEMS) -> tuple[bytes, list[str]]:
    """Tar+gzip the items that exist under root. Returns (bytes, names included)."""
    buf = io.BytesIO()
    included: list[str] = []
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in items:
            path = root / name
            if path.exists():
                tar.add(path, arcname=name, filter=_refuse_links)
                included.append(name)
    return buf.getvalue(), included


def export_data(
    root: Path,
    out_file: Path,
    passphrase: str,
    *,
    force: bool = False,
    min_passphrase: int = PASSPHRASE_MIN_CHARS,
) -> list[str]:
    if len(passphrase) < min_passphrase:
        raise ValueError(f"Passphrase must be at least {min_passphrase} characters")
    out_file = out_file.resolve()
    data_dir = (root / "data").resolve()
    if out_file == data_dir or data_dir in out_file.parents:
        raise ValueError("Output must not be inside data/ (it would bundle old archives)")
    if out_file.exists() and not force:
        raise ValueError(f"{out_file} already exists; use --force to replace it")
    payload, included = build_archive(root)
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + bytes([SCRYPT_LOG_N]) + salt + nonce
    ciphertext = AESGCM(_key(passphrase, salt, SCRYPT_LOG_N)).encrypt(nonce, payload, header)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_file.with_name(out_file.name + ".tmp")
    tmp.write_bytes(header + ciphertext)
    os.replace(tmp, out_file)  # atomic: a crash never leaves a truncated backup
    return included


# ---- reading an archive ---------------------------------------------------------------------------------


def _read_archive(archive: Path) -> bytes:
    """The archive's bytes, refusing one over ARCHIVE_MAX_BYTES before reading it (H-01)."""
    size = archive.stat().st_size
    if size > ARCHIVE_MAX_BYTES:
        raise ValueError(f"Archive is {size} bytes; larger than the {ARCHIVE_MAX_BYTES} this tool accepts")
    with open(archive, "rb") as f:
        blob = f.read(ARCHIVE_MAX_BYTES + 1)
    if len(blob) > ARCHIVE_MAX_BYTES:  # it grew while we looked
        raise ValueError(f"Archive is larger than the {ARCHIVE_MAX_BYTES} bytes this tool accepts")
    return blob


def decrypt_archive(archive: Path, passphrase: str) -> bytes:
    """The authenticated payload (a gzip tar). Every header field is checked BEFORE the key is derived:
    only the SCRYPT_LOG_N that export writes is accepted, so a crafted header cannot ask scrypt for
    gigabytes (H-01)."""
    blob = _read_archive(archive)
    if not blob.startswith(MAGIC) or len(blob) < HEADER_LEN + TAG_LEN:
        raise ValueError("Not an Embodied Agent migration archive (or it is truncated)")
    log_n = blob[len(MAGIC)]
    if log_n != SCRYPT_LOG_N:
        raise ValueError(f"Unsupported archive header (key cost {log_n}; this tool writes and accepts only "
                         f"{SCRYPT_LOG_N}): corrupted or not made by this tool")
    salt = blob[len(MAGIC) + 1 : len(MAGIC) + 17]
    header = blob[:HEADER_LEN]
    nonce = blob[len(MAGIC) + 17 : HEADER_LEN]
    try:
        return AESGCM(_key(passphrase, salt, log_n)).decrypt(nonce, blob[HEADER_LEN:], header)
    except InvalidTag as exc:
        raise ValueError("Wrong passphrase or corrupted archive") from exc


def _member_problem(member: tarfile.TarInfo, dest: Path) -> str | None:
    name = member.name
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if not name or name.startswith(("/", "\\")) or ".." in parts or (len(name) > 1 and name[1] == ":"):
        return f"Unsafe path in archive: {name[:200]}"
    target = (dest / name).resolve()
    if dest not in target.parents and target != dest:
        return f"Unsafe path in archive: {name[:200]}"
    if not (member.isreg() or member.isdir()):  # links, devices, fifos and the rest
        return f"Unsupported entry in archive: {name[:200]}"
    return None


def _scan(tar: tarfile.TarFile, dest: Path) -> Iterator[tarfile.TarInfo]:
    """Each member, checked, while bounding the member count and the unpacked size (H-01)."""
    count = total = 0
    for member in tar:  # reads one header at a time: a bomb is stopped at the bound, not after it
        count += 1
        if count > ARCHIVE_MEMBERS_MAX:
            raise ValueError(f"Archive has more than {ARCHIVE_MEMBERS_MAX} entries")
        total += max(member.size, 0)
        if total > ARCHIVE_UNPACKED_MAX_BYTES:
            raise ValueError(f"Archive unpacks to more than {ARCHIVE_UNPACKED_MAX_BYTES} bytes")
        problem = _member_problem(member, dest)
        if problem:
            raise ValueError(problem)
        yield member


def _open_payload(payload: bytes) -> tarfile.TarFile:
    return tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz")


_TAR_ERRORS = (tarfile.TarError, EOFError, zlib.error, OSError)  # authentic, but not a gzip tar (X01-F03)


def verify_archive(archive: Path, passphrase: str) -> list[tuple[str, int, bool]]:
    """Decrypt and check the whole structure and size without extracting anything.
    Returns [(name, size in bytes, is_folder)]. Raises ValueError on any problem."""
    payload = decrypt_archive(archive, passphrase)
    probe = Path.cwd() / "__migrate_verify__"  # only for the path checks; nothing is written there
    try:
        with _open_payload(payload) as tar:
            return [(m.name, m.size, m.isdir()) for m in _scan(tar, probe)]
    except _TAR_ERRORS as exc:
        raise ValueError(f"Archive is not a valid compressed tar ({type(exc).__name__})") from exc


# ---- restoring ----------------------------------------------------------------------------------------------


def _read_deletions(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    if path.stat().st_size > DELETION_LOG_MAX_BYTES:
        raise ValueError(f"{path} is larger than {DELETION_LOG_MAX_BYTES} bytes; refusing to restore")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return {ln.strip().replace("\\", "/").strip("/") for ln in lines if ln.strip() and not ln.lstrip().startswith("#")}


def deleted_by_record(name: str, deletions: set[str]) -> bool:
    """Is archive entry `name` at or under a path the destination's deletion log lists?"""
    clean = name.replace("\\", "/").strip("/")
    return any(clean == d or clean.startswith(d + "/") for d in deletions)


def restore_data(archive: Path, dest: Path, passphrase: str, *, force: bool = False,
                 on_replace: Callable[[str], object] | None = None) -> list[str]:
    """Decrypt, check and unpack into `dest`. Returns the top-level names restored.

    A non-empty `dest` is refused unless `force`; then each existing path the archive overwrites is
    passed to `on_replace` (if given) BEFORE anything is written. Entries listed in dest's deletion log
    are skipped. Raises ValueError for every refusal."""
    if dest.exists() and not dest.is_dir():
        raise ValueError(f"Destination {dest} exists and is not a folder")
    existing = sorted(p.name for p in dest.iterdir()) if dest.is_dir() else []
    if existing and not force:
        shown = ", ".join(existing[:10]) + (f" and {len(existing) - 10} more" if len(existing) > 10 else "")
        raise ValueError(f"Destination {dest} is not empty ({shown}); restore does not overwrite. Restore into "
                         "an empty folder, or use --force to replace what the archive holds (it is listed first)")
    payload = decrypt_archive(archive, passphrase)
    dest_resolved = dest.resolve()
    deletions = _read_deletions(dest / DELETION_LOG)
    try:
        with _open_payload(payload) as tar:
            members = list(_scan(tar, dest_resolved))  # every check passes before anything is written
            keep = [m for m in members if not deleted_by_record(m.name, deletions)
                    and not (deletions and m.name.replace("\\", "/").strip("/") == DELETION_LOG)]
            overwritten = [m.name for m in keep if not m.isdir() and (dest_resolved / m.name).exists()]
            if on_replace is not None:
                for name in overwritten:
                    on_replace(name)
            dest.mkdir(parents=True, exist_ok=True)
            tar.extractall(dest, members=keep, filter="data")
            archived_log = next((m for m in members if m.name.replace("\\", "/").strip("/") == DELETION_LOG
                                 and m.isreg()), None)
            if deletions and archived_log is not None:  # merge, never lose a newer record
                f = tar.extractfile(archived_log)
                older = set() if f is None else {ln.strip() for ln in f.read(DELETION_LOG_MAX_BYTES).decode(
                    "utf-8", "replace").splitlines() if ln.strip() and not ln.lstrip().startswith("#")}
                extra = sorted(older - deletions)
                if extra:
                    with open(dest / DELETION_LOG, "a", encoding="utf-8") as out:
                        out.write("".join(f"{e}\n" for e in extra))
            return sorted({m.name.split("/")[0] for m in keep})
    except _TAR_ERRORS as exc:
        raise ValueError(f"Archive is not a valid compressed tar ({type(exc).__name__})") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "migrate").splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    exp = sub.add_parser("export", help="bundle and encrypt data")
    exp.add_argument("out", type=Path)
    exp.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    exp.add_argument("--force", action="store_true", help="replace an existing archive")
    res = sub.add_parser("restore", help="decrypt and unpack an archive into an empty folder")
    res.add_argument("archive", type=Path)
    res.add_argument("--dest", type=Path, required=True)
    res.add_argument("--force", action="store_true",
                     help="restore into a non-empty folder, replacing the files the archive holds (listed first)")
    ver = sub.add_parser("verify", help="decrypt and check an archive without extracting it")
    ver.add_argument("archive", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "export":
            pw = getpass.getpass("Passphrase: ")
            if pw != getpass.getpass("Again: "):
                print("Passphrases differ", file=sys.stderr)
                return 1
            included = export_data(args.root, args.out, pw, force=args.force)
            print(f"Wrote {args.out} with: {', '.join(included) or '(nothing found)'}")
        elif args.cmd == "verify":
            pw = getpass.getpass("Passphrase: ")
            entries = verify_archive(args.archive, pw)
            for name, size, is_dir in entries:
                print(f"{name}/" if is_dir else f"{name}  {size} bytes")
            print(f"OK: {len(entries)} entries, {sum(s for _, s, _ in entries)} bytes unpacked; nothing extracted")
        else:
            pw = getpass.getpass("Passphrase: ")
            names = restore_data(args.archive, args.dest, pw, force=args.force,
                                 on_replace=lambda name: print(f"Replacing: {name}"))
            print(f"Restored into {args.dest}: {', '.join(names)}")
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
