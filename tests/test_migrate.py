import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "migrate", Path(__file__).resolve().parent.parent / "tools" / "migrate.py"
)
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)


def make_tree(root: Path):
    (root / "data" / "identity").mkdir(parents=True)
    (root / "data" / "identity" / "registry.db").write_bytes(b"people and places")
    (root / ".env").write_text("ANTHROPIC_API_KEY=abc\n")
    (root / "config.toml").write_text("[network]\nport = 8765\n")


def test_round_trip(tmp_path):
    src, dest, out = tmp_path / "src", tmp_path / "dest", tmp_path / "out.eamig"
    src.mkdir()
    make_tree(src)
    included = migrate.export_data(src, out, "correct horse battery staple")
    assert set(included) == {"data", ".env", "config.toml"}
    names = migrate.restore_data(out, dest, "correct horse battery staple")
    assert set(names) == {"data", ".env", "config.toml"}
    assert (dest / "data" / "identity" / "registry.db").read_bytes() == b"people and places"
    assert (dest / ".env").read_text() == "ANTHROPIC_API_KEY=abc\n"


def test_archive_is_not_readable_in_plain(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out.eamig"
    src.mkdir()
    make_tree(src)
    migrate.export_data(src, out, "a-long-test-passphrase")
    blob = out.read_bytes()
    assert b"ANTHROPIC_API_KEY" not in blob and b"people and places" not in blob


def test_wrong_passphrase_fails(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out.eamig"
    src.mkdir()
    make_tree(src)
    migrate.export_data(src, out, "the-right-passphrase")
    with pytest.raises(ValueError):
        migrate.restore_data(out, tmp_path / "dest", "the-wrong-passphrase")


def test_empty_passphrase_refused(tmp_path):
    with pytest.raises(ValueError):
        migrate.export_data(tmp_path, tmp_path / "o", "short")


def test_unsafe_archive_paths_rejected(tmp_path):
    import io
    import os
    import tarfile

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))
    salt, nonce = os.urandom(16), os.urandom(12)
    header = migrate.MAGIC + bytes([migrate.SCRYPT_LOG_N]) + salt + nonce
    key = migrate._key("a-long-test-passphrase", salt, migrate.SCRYPT_LOG_N)
    blob = header + AESGCM(key).encrypt(nonce, buf.getvalue(), header)
    evil = tmp_path / "evil.eamig"
    evil.write_bytes(blob)
    with pytest.raises(ValueError):
        migrate.restore_data(evil, tmp_path / "dest", "a-long-test-passphrase")
    assert not (tmp_path / "escape.txt").exists()


def test_existing_output_needs_force_and_is_replaced_atomically(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out.eamig"
    src.mkdir()
    make_tree(src)
    migrate.export_data(src, out, "a-long-test-passphrase")
    with pytest.raises(ValueError):
        migrate.export_data(src, out, "a-long-test-passphrase")
    migrate.export_data(src, out, "a-long-test-passphrase", force=True)
    assert not list(tmp_path.glob("*.tmp"))


def test_output_inside_data_is_rejected(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_tree(src)
    with pytest.raises(ValueError):
        migrate.export_data(src, src / "data" / "old.eamig", "a-long-test-passphrase")


def test_symlink_in_data_is_refused_at_export(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_tree(src)
    try:
        (src / "data" / "link").symlink_to(src / ".env")
    except OSError:
        pytest.skip("symlinks not permitted on this machine")
    with pytest.raises(ValueError):
        migrate.export_data(src, tmp_path / "o.eamig", "a-long-test-passphrase")


def test_cli_reports_wrong_passphrase_without_traceback(tmp_path, monkeypatch, capsys):
    src, out = tmp_path / "src", tmp_path / "out.eamig"
    src.mkdir()
    make_tree(src)
    migrate.export_data(src, out, "the-right-passphrase")
    monkeypatch.setattr(migrate.getpass, "getpass", lambda _="": "the-wrong-passphrase")
    code = migrate.main(["restore", str(out), "--dest", str(tmp_path / "dest")])
    assert code == 1
    assert "Wrong passphrase" in capsys.readouterr().err
