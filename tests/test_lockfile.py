"""requirements.lock (H-03): it covers everything pyproject.toml asks for, and every line is pinned and hashed."""

import re
import tomllib

import pytest

from brain.config import REPO_ROOT

ENTRY = re.compile(r"^([a-z0-9.-]+)==(\S+) \\\n    --hash=sha256:[0-9a-f]{64}$", re.MULTILINE)


def _name(requirement: str) -> str:
    return re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def _locked() -> tuple[str, dict[str, str]]:
    text = (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8")
    return text, dict(ENTRY.findall(text))


def _wanted() -> list[str]:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return [*project["dependencies"], *(r for extra in project["optional-dependencies"].values() for r in extra)]


def test_every_entry_is_pinned_and_hashed():
    text, locked = _locked()
    body = [line for line in text.splitlines() if line and not line.startswith(("#", " "))]
    assert len(body) == len(locked) > 0, "a line that is not `name==version` with a sha256 hash"
    assert "://" not in text, "the lock file must not name a package index"


@pytest.mark.parametrize("requirement", _wanted())
def test_every_declared_dependency_is_locked_at_an_allowed_version(requirement):
    _, locked = _locked()
    name = _name(requirement)
    assert name in locked, f"{name} is not in requirements.lock: run tools/make_lockfile.py"
    exact = re.search(r"==\s*([^;,\s]+)", requirement)
    if exact:
        assert locked[name] == exact.group(1), f"{name}: pyproject pins {exact.group(1)}, the lock has {locked[name]}"
