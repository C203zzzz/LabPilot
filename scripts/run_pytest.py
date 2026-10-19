"""Run pytest with temp directories that stay usable inside the DSH sandbox.

Why this exists
---------------
Two environment artefacts make a plain ``pytest`` run report hundreds of
identical ``E`` setup errors that look like broken code:

1. ``pytest`` creates its base temp directory with ``mkdir(mode=0o700)``.
   On Windows that mode becomes a restrictive DACL, and under the sandbox's
   restricted token the *effective* identity is not the directory owner, so
   the directory is unreadable even to the process that created it
   (``PermissionError: [WinError 5]``).  Every ``tmp_path`` fixture then
   fails before the test body runs.
2. The sandbox temp root (``%TEMP%\\dsh-*``) cannot be listed, so pytest's
   default per-session directory under it fails at session setup.

This wrapper fixes only the environment, never the test outcome:

* ``os.mkdir`` is wrapped to widen the mode argument to ``0o777``;
* ``TMP``/``TEMP`` and ``--basetemp`` are pointed at ``<repo>/tmp`` so temp
  data stays inside the writable workspace (``tmp/`` is gitignored);
* a unique base directory per run avoids stale directories whose restrictive
  DACL can no longer be removed from inside the sandbox.

Usage::

    .venv\\Scripts\\python.exe scripts\\run_pytest.py tests -q
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _permissive_mkdir() -> None:
    """Let directories be created with a DACL this process can still read."""
    real_mkdir = os.mkdir

    def mkdir(path, mode=0o777, *, dir_fd=None):  # noqa: ANN001 - mirrors os.mkdir
        return real_mkdir(path, 0o777, dir_fd=dir_fd)

    os.mkdir = mkdir


def _prepare_temp_root() -> Path:
    root = REPO_ROOT / "tmp"
    root.mkdir(mode=0o777, parents=True, exist_ok=True)
    os.environ["TMP"] = os.environ["TEMP"] = str(root)
    return root


def main(argv: list[str]) -> int:
    _permissive_mkdir()
    temp_root = _prepare_temp_root()
    if not any(arg.split("=")[0] == "--basetemp" for arg in argv):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        argv = ["--basetemp", str(temp_root / f"pytest-{stamp}-{os.getpid()}"), *argv]
    import pytest

    return int(pytest.main(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
