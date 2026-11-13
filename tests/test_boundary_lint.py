"""The brain must stay body-agnostic.

Rules, enforced here:
1. Only the phone body (brain/body/phone.py or the brain/body/phone/ package) may
   mention Android or ARCore, in any text file under brain/.
2. Only brain/body/ and the wiring point brain/__main__.py may import the phone body.
"""

import ast
import re
from pathlib import Path

BRAIN = Path(__file__).resolve().parent.parent / "brain"
PHONE_WORDS = re.compile(r"android|arcore|kotlin|camera2|\badb\b", re.IGNORECASE)
TEXT_SUFFIXES = {".py", ".pyi", ".json", ".toml", ".txt", ".md", ".yaml", ".yml"}


def is_phone_body(path: Path, root: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    return rel == "body/phone.py" or rel.startswith("body/phone/")


def may_import_phone(path: Path, root: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    return rel.startswith("body/") or rel == "__main__.py"


def word_violations(root: Path) -> list[str]:
    bad = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        if is_phone_body(path, root):
            continue
        if PHONE_WORDS.search(path.read_text(encoding="utf-8-sig")):
            bad.append(path.relative_to(root).as_posix())
    return bad


def imports_phone(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = [a.name for a in node.names]
            if mod.endswith("body.phone") or mod == "phone" or mod.endswith(".phone"):
                return True
            if mod.endswith("body") and "phone" in names:
                return True
            if mod == "" and "phone" in names:  # from . import phone
                return True
        elif isinstance(node, ast.Import):
            if any(a.name.endswith("body.phone") for a in node.names):
                return True
    return False


def import_violations(root: Path) -> list[str]:
    bad = []
    for path in sorted(root.rglob("*.py")):
        if may_import_phone(path, root):
            continue
        if imports_phone(path.read_text(encoding="utf-8-sig")):
            bad.append(path.relative_to(root).as_posix())
    return bad


def test_brain_has_no_phone_words_outside_phone_body():
    assert word_violations(BRAIN) == []


def test_only_body_and_main_import_the_phone_body():
    assert import_violations(BRAIN) == []


def test_word_check_catches_other_file_types_and_names(tmp_path):
    (tmp_path / "body").mkdir()
    (tmp_path / "body" / "phone.py").write_text("# Android is fine here\n")
    (tmp_path / "agent.py").write_text("x = 'uses ARCore directly'\n")
    (tmp_path / "notes.json").write_text('{"via": "adb"}')
    assert word_violations(tmp_path) == ["agent.py", "notes.json"]


def test_phone_package_is_allowed(tmp_path):
    (tmp_path / "body" / "phone").mkdir(parents=True)
    (tmp_path / "body" / "phone" / "session.py").write_text("# ARCore here is fine\n")
    assert word_violations(tmp_path) == []


def test_import_check_catches_each_import_style(tmp_path):
    cases = {
        "a.py": "from brain.body.phone import PhoneBody\n",
        "b.py": "import brain.body.phone\n",
        "c.py": "from brain.body import phone\n",
        "d.py": "from .body.phone import PhoneBody\n",
    }
    for name, src in cases.items():
        (tmp_path / name).write_text(src)
    (tmp_path / "ok.py").write_text("from brain.body.base import Body\n")
    assert import_violations(tmp_path) == sorted(cases)
