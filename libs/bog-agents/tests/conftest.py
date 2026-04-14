"""Shared pytest configuration for SDK tests."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

_STATE: dict[str, Path | None] = {"temp_root": None}


def pytest_configure(config: pytest.Config) -> None:
    """Force temp files into a workspace-local writable root."""
    del config
    temp_root = Path(__file__).resolve().parent.parent / ".pytest-tmp-runtime" / f"session-{os.getpid()}-{uuid4().hex[:8]}"
    temp_root.mkdir(parents=True, exist_ok=True)

    temp_root_str = str(temp_root)
    os.environ["TMP"] = temp_root_str
    os.environ["TEMP"] = temp_root_str
    os.environ["TMPDIR"] = temp_root_str
    os.environ["PYTEST_DEBUG_TEMPROOT"] = temp_root_str
    tempfile.tempdir = temp_root_str

    _STATE["temp_root"] = temp_root


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """Provide a Windows-safe temporary directory within the workspace."""
    temp_root = _STATE["temp_root"]
    if temp_root is None:
        msg = "Workspace temp root was not initialized."
        raise RuntimeError(msg)

    path = temp_root / f"test-{uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    yield path
    shutil.rmtree(path, ignore_errors=True)
