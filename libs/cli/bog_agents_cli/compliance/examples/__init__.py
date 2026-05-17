"""Bundled example audit packs.

Files in this directory are *not* imported as Python — the audit
controller resolves them as YAML at run time. This module exists
so the directory is part of the wheel and ``examples_dir()`` can
return a stable ``Path`` even when bog-agents-cli is installed.
"""

from __future__ import annotations
