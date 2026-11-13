"""Move the project's private data to another machine, encrypted.

    python tools/migrate.py export OUT.eamig
    python tools/migrate.py restore OUT.eamig --dest NEWFOLDER

Bundles data/, .env and config.toml into one archive encrypted with a passphrase
(scrypt key derivation, AES-256-GCM). See docs/migration.md for the full
procedure, including the secure-delete checklist for the old machine.

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
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"EAMIG2"
ITEMS = ("data", ".env", "config.toml")
SCRYPT_LOG_N = 17  # 2**17: about 128 MiB per guess. Stored in the header.
MIN_PASSPHRASE = 12
HEADER_LEN = len(MAGIC) + 1 + 16 + 12  # magic, log2(N), salt, nonce


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
    min_passphrase: int = MIN_PASSPHRASE,
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


def _safe_members(tar: tarfile.TarFile, dest: Path):
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if dest not in target.parents and target != dest:
            raise ValueError(f"Unsafe path in archive: {member.name}")
        if member.issym() or member.islnk() or member.isdev():
            raise ValueError(f"Unsupported entry in archive: {member.name}")
        yield member


def restore_data(archive: Path, dest: Path, passphrase: str) -> list[str]:
    blob = archive.read_bytes()
    if not blob.startswith(MAGIC) or len(blob) < HEADER_LEN:
        raise ValueError("Not an Embodied Agent migration archive")
    log_n = blob[len(MAGIC)]
    if not 14 <= log_n <= 22:
        raise ValueError("Corrupted archive header")
    salt = blob[len(MAGIC) + 1 : len(MAGIC) + 17]
    header = blob[:HEADER_LEN]
    nonce = blob[len(MAGIC) + 17 : HEADER_LEN]
    ciphertext = blob[HEADER_LEN:]
    try:
        payload = AESGCM(_key(passphrase, salt, log_n)).decrypt(nonce, ciphertext, header)
    except InvalidTag as exc:
        raise ValueError("Wrong passphrase or corrupted archive") from exc
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        members = list(_safe_members(tar, dest))
        tar.extractall(dest, members=members, filter="data")
        return sorted({m.name.split("/")[0] for m in members})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    exp = sub.add_parser("export", help="bundle and encrypt data")
    exp.add_argument("out", type=Path)
    exp.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    exp.add_argument("--force", action="store_true", help="replace an existing archive")
    res = sub.add_parser("restore", help="decrypt and unpack an archive")
    res.add_argument("archive", type=Path)
    res.add_argument("--dest", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "export":
            pw = getpass.getpass("Passphrase: ")
            if pw != getpass.getpass("Again: "):
                print("Passphrases differ", file=sys.stderr)
                return 1
            included = export_data(args.root, args.out, pw, force=args.force)
            print(f"Wrote {args.out} with: {', '.join(included) or '(nothing found)'}")
        else:
            pw = getpass.getpass("Passphrase: ")
            names = restore_data(args.archive, args.dest, pw)
            print(f"Restored into {args.dest}: {', '.join(names)}")
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
