"""Declarative per-run sandbox environment via `.bog-agents/sandbox.toml` (#16).

Safe unattended/cloud execution needs reproducible, pre-provisioned environments
and bounded network access — nobody is watching egress at 3am. This module loads
a committed-in-repo `.bog-agents/sandbox.toml` describing preinstall steps, a
runner size, a base image, an optional reusable snapshot, and a network egress
allowlist, and can materialize the preinstall steps into a setup script
compatible with the existing ``--sandbox-setup`` flag.

Example `.bog-agents/sandbox.toml`::

    [sandbox]
    base_image = "python:3.11-slim"
    runner_size = "medium"
    snapshot = "ci-base"
    preinstall = [
        "uv sync --all-groups",
        "apt-get install -y ripgrep",
    ]
    network_allowlist = ["pypi.org", "github.com", "api.anthropic.com"]

The loader + materialization are the testable foundation; enforcing the egress
allowlist in each sandbox backend is the integration follow-up (the allowlist is
surfaced here for backends/daemon to consume).
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_REL_PATH = Path(".bog-agents") / "sandbox.toml"
_VALID_SIZES = ("small", "medium", "large")
_DEFAULT_SIZE = "medium"


@dataclass
class SandboxConfig:
    """Parsed `.bog-agents/sandbox.toml` settings.

    Attributes:
        base_image: Container image to provision (provider-specific; optional).
        runner_size: One of small/medium/large (advisory sizing hint).
        snapshot: Optional named reusable snapshot so setup isn't repeated.
        preinstall: Shell commands run once to provision the environment.
        network_allowlist: Hostnames the sandbox may reach; empty means the
            backend's default (no restriction declared here).
        source: Path the config was loaded from.
    """

    base_image: str | None = None
    runner_size: str = _DEFAULT_SIZE
    snapshot: str | None = None
    preinstall: list[str] = field(default_factory=list)
    network_allowlist: list[str] = field(default_factory=list)
    source: str = ""

    def materialize_setup_script(self, dest: str | Path) -> Path:
        """Write the preinstall steps as a POSIX shell script at ``dest``.

        The script is compatible with ``--sandbox-setup`` (and runs each step
        with ``set -e`` so provisioning fails loudly). Returns the path written.
        """
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "#!/usr/bin/env bash",
            "# Auto-generated from .bog-agents/sandbox.toml — do not edit by hand.",
            "set -euo pipefail",
            "",
            *self.preinstall,
            "",
        ]
        dest_path.write_text("\n".join(lines), encoding="utf-8")
        return dest_path

    def summary(self) -> str:
        """A one-line human summary of the sandbox config."""
        net = (
            f"{len(self.network_allowlist)} allowed host(s)"
            if self.network_allowlist
            else "no egress allowlist"
        )
        return (
            f"sandbox: image={self.base_image or 'default'}, size={self.runner_size}, "
            f"snapshot={self.snapshot or 'none'}, {len(self.preinstall)} preinstall step(s), {net}"
        )


def _coerce_str_list(value: object) -> list[str]:
    """Coerce a TOML value to a list of non-empty strings."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def load_sandbox_config(cwd: str | Path | None = None) -> SandboxConfig | None:
    """Load `.bog-agents/sandbox.toml` from ``cwd`` (or the process CWD).

    Returns:
        A :class:`SandboxConfig`, or ``None`` when the file is absent. Malformed
        files log a warning and return ``None`` rather than raising — sandbox
        config is best-effort and must never block a run.
    """
    root = Path(cwd) if cwd else Path.cwd()
    path = root / _CONFIG_REL_PATH
    if not path.is_file():
        return None
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        logger.warning("Ignoring malformed %s: %s", path, exc)
        return None

    section = raw.get("sandbox", raw)
    if not isinstance(section, dict):
        section = {}

    size = str(section.get("runner_size", _DEFAULT_SIZE)).strip().lower()
    if size not in _VALID_SIZES:
        logger.debug("Unknown runner_size %r; defaulting to %s", size, _DEFAULT_SIZE)
        size = _DEFAULT_SIZE

    base_image = section.get("base_image")
    snapshot = section.get("snapshot")
    return SandboxConfig(
        base_image=str(base_image).strip() if base_image else None,
        runner_size=size,
        snapshot=str(snapshot).strip() if snapshot else None,
        preinstall=_coerce_str_list(section.get("preinstall")),
        network_allowlist=_coerce_str_list(section.get("network_allowlist")),
        source=str(path),
    )
