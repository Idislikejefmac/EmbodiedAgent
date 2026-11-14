"""Card P0-06 step 1: "type checker passes". Runs mypy in strict mode over the packages named in the
`[tool.mypy]` section of pyproject.toml (`files`).

A missing mypy is a FAILURE, not a skip (H-13, R-FND-58): a check that cannot run must not pass. Set
EA_ALLOW_MISSING_TOOLS=1 to skip on a machine that deliberately has no dev tools. Do not copy this
pattern to any other tool check.

By hand:  .venv/Scripts/python -m mypy
"""

import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def require_tool(module: str, *, find: Callable[[str], object | None] = importlib.util.find_spec,
                 env: Mapping[str, str] = os.environ) -> None:
    """Return if `module` is installed. Otherwise fail the test, or skip it only when EA_ALLOW_MISSING_TOOLS=1."""
    if find(module) is not None:
        return
    message = f"{module} is not installed (pip install -e .[dev]); the check cannot run"
    if env.get("EA_ALLOW_MISSING_TOOLS") == "1":
        pytest.skip(message + " (EA_ALLOW_MISSING_TOOLS=1)")
    pytest.fail(message + "; set EA_ALLOW_MISSING_TOOLS=1 only on a machine without dev tools")


def test_mypy_strict_passes_on_the_checked_packages():
    require_tool("mypy")
    out = subprocess.run([sys.executable, "-m", "mypy"], capture_output=True, text=True, cwd=REPO, timeout=900)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "no issues found" in out.stdout
