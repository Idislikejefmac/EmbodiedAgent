"""Card P0-06 step 1: "type checker passes". Runs mypy in strict mode over the body layer, the tool
registry and the egress guard (the `[tool.mypy]` section of pyproject.toml says which files).

By hand:  .\\.venv\\Scripts\\python -m mypy
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(importlib.util.find_spec("mypy") is None, reason="mypy is not installed (pip install -e .[dev])")
def test_mypy_strict_passes_on_body_agent_and_egress():
    out = subprocess.run([sys.executable, "-m", "mypy"], capture_output=True, text=True, cwd=REPO, timeout=600)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "no issues found" in out.stdout
