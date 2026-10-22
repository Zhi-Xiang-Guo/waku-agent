"""Memory row actions must preserve other editors through the real render guard.

Run the shipped edit handlers and render() in Node with a small DOM/HTTP stub,
like the other dashboard evals. The browser reproduction covers the real DOM.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("case", ["save", "cancel", "single", "error", "missing", "network", "late"])
def test_memory_row_drafts(case):
    script = ROOT / "evals" / "fixtures" / "memory_row_drafts.cjs"
    result = subprocess.run(  # noqa: S603
        [NODE, str(script), str(ROOT / "waku" / "ops" / "static" / "js"), case],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"case": case, "passed": True}
