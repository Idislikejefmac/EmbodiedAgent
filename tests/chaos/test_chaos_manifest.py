"""The entry-point manifest and its drift check (plan card X-01 step 1; T-X01-001, T-X01-002, T-X01-020).

A new entry point in the code that the manifest lacks fails the build, and so does a manifest record
the code no longer has, in each kind of entry point that exists today.
"""

import argparse
import shutil

import pytest

from brain.body import base
from brain.transport import protocol
from tools.chaos import manifest as M
from tools.chaos import planted

pytestmark = pytest.mark.chaos


@pytest.fixture(scope="module")
def found():
    return M.discover()


def test_the_manifest_matches_the_code(found):
    d = M.drift(M.load_manifest(), found)
    assert d.ok, "\n".join(d.lines())


def test_discovery_finds_every_kind_built_today(found):
    kinds = {v["kind"] for v in found.values()}
    assert kinds == set(M.KINDS)
    ids = set(found)
    assert {"tool.speak", "reserved_tool.delete_person_data", "message.hello", "field.hello.software",
            "command.capture_image", "body_method.capture_image", "cli_command.brain.chaos",
            "cli_arg.brain.probe.--bytes", "config_key.egress.allowed_hosts", "env_var.ANTHROPIC_API_KEY",
            "store.link_token", "android_component.debug.receiver.DebugCommandReceiver",
            "android_action.debug.PAIR"} <= ids
    assert found["tool.speak"]["channels"] == ["app", "console"]
    assert found["android_component.debug.receiver.DebugCommandReceiver"]["exported"] == "true"


def _drift_lines(found):
    return M.drift(M.load_manifest(), found).lines()


def test_planted_new_tool_fails(monkeypatch):
    with planted.active("unregistered_entry_point"):
        lines = _drift_lines(M.discover())
    assert any("tool.teleport" in line and "lacks" in line for line in lines), lines


def test_planted_new_cli_subcommand_fails(monkeypatch):
    real = M.CLI_MAINS["migrate"]

    def main_with_extra(argv=None):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="cmd")
        for name in ("export", "restore", "wipe"):
            sub.add_parser(name)
        return p.parse_args(argv)

    monkeypatch.setitem(M.CLI_MAINS, "migrate", lambda: main_with_extra)
    lines = _drift_lines(M.discover())
    assert any("cli_command.migrate.wipe" in line and "lacks" in line for line in lines)
    # the planted parser also lost the real flags, so the reverse direction fires too
    assert any("cli_arg.migrate.export.--force" in line and "no longer has" in line for line in lines)
    monkeypatch.setitem(M.CLI_MAINS, "migrate", real)


def test_planted_new_message_type_fails(monkeypatch):
    monkeypatch.setattr(protocol, "MESSAGE_TYPES", protocol.MESSAGE_TYPES + ("shell",))
    lines = _drift_lines(M.discover())
    assert any("message.shell" in line for line in lines)


def test_planted_new_config_key_fails(monkeypatch, tmp_path):
    extra = tmp_path / "planted.py"
    extra.write_text("def f(config):\n    return config.get('debug', {}).get('open_port')\n", encoding="utf-8")
    monkeypatch.setattr(M, "SCAN_EXTRA_FILES", M.SCAN_EXTRA_FILES + [extra])
    lines = _drift_lines(M.discover())
    assert any("config_key.debug.open_port" in line for line in lines)


def test_planted_new_body_method_fails(monkeypatch):
    monkeypatch.setattr(base.Body, "teleport", lambda self: None, raising=False)
    lines = _drift_lines(M.discover())
    assert any("body_method.teleport" in line for line in lines)


def test_planted_android_receiver_fails(monkeypatch, tmp_path):
    copy = tmp_path / "AndroidManifest.xml"
    shutil.copy(M.ANDROID_MANIFESTS["main"], copy)
    text = copy.read_text(encoding="utf-8-sig").replace(
        "</application>", '<receiver android:name=".Backdoor" android:exported="true" /></application>')
    copy.write_text(text, encoding="utf-8")
    monkeypatch.setitem(M.ANDROID_MANIFESTS, "main", copy)
    lines = _drift_lines(M.discover())
    assert any("android_component.main.receiver.Backdoor" in line for line in lines)


def test_a_security_attribute_change_fails(monkeypatch, found):
    changed = dict(found)
    changed["tool.speak"] = dict(found["tool.speak"], channels=["app"])
    changed["android_component.main.service.LinkService"] = dict(
        found["android_component.main.service.LinkService"], exported="true")
    lines = _drift_lines(changed)
    assert sum("attribute changed" in line for line in lines) == 2


def test_removing_a_manifest_record_fails(found):
    text = M.MANIFEST_PATH.read_text(encoding="utf-8")
    line = next(x for x in text.splitlines() if '"tool.speak"' in x)
    m = M.load_manifest(text=text.replace(line + "\n", ""))
    assert any("tool.speak" in x and "lacks" in x for x in M.drift(m, found).lines())


@pytest.mark.parametrize("text, why", [
    ("schema = 1\nentry = [\n", "not valid TOML"),
    ('schema = 2\nentry = []\n', "schema must be"),
    ('schema = 1\nentry = [{ id = "tool.a", kind = "tool" }]\n', "lacks"),
    ('schema = 1\nentry = [{ id = "tool.a", kind = "nope", card = "P0-06", boundary = "B6" }]\n', "kind"),
    ('schema = 1\nentry = [{ id = "env_var.A", kind = "env_var", card = "P0-04", boundary = "B11" },'
     '{ id = "env_var.A", kind = "env_var", card = "P0-04", boundary = "B11" }]\n', "listed twice"),
    ('schema = 1\nentry = [{ id = "env_var.A", kind = "env_var", card = "P0-04", boundary = "X9" }]\n', "B-id"),
    ('schema = 1\nentry = [{ id = "env_var.A", kind = "env_var", card = "P0-04", boundary = "B1", run = "x" }]\n',
     "unknown keys"),
])
def test_a_corrupt_manifest_is_refused_with_a_message(text, why):
    with pytest.raises(M.ManifestError, match=why):
        M.load_manifest(text=text)


def test_gate_table_resolves_and_names_known_limits():
    m = M.load_manifest()
    assert len(m.gates) >= 20 and M.check_gates(m) == []
    bad = M.load_manifest(text=M.MANIFEST_PATH.read_text(encoding="utf-8").replace(
        'name = "brain.config.load_config"', 'name = "brain.config.load_everything"'))
    assert any("load_everything" in p for p in M.check_gates(bad))


def test_stub_lines_paste_back_into_the_manifest(found):
    line = M.stub_line("tool.speak", found["tool.speak"])
    assert line.startswith('{ id = "tool.speak", kind = "tool", card = "P0-06", boundary = "B6"')
    m = M.load_manifest(text=f"schema = 1\nentry = [\n{line}\n]\n")
    assert m.entries["tool.speak"]["sensitive"] is False
