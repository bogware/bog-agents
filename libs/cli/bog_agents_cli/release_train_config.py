"""Configuration loader for ``/release-train`` enrichment sources.

A single TOML file at ``~/.bog-agents/release-train.toml`` defines
which external systems to query when generating release notes. The
file is optional — when absent we synthesise the default config in
memory and **all enrichment is OFF** (commit-only flow, identical
to pre-enrichment behavior).

Resolution order (highest priority first):

1. Per-source env vars:
   * ``BOG_AGENTS_RELEASE_TRAIN_JIRA`` — force jira on/off
   * ``BOG_AGENTS_RELEASE_TRAIN_HALO`` — force halo on/off
   Accept ``1/0/true/false/yes/no/on/off``.
2. The TOML file's contents.
3. Built-in defaults — every source OFF, ``mode = "auto"``.

Credentials are *never* stored in the TOML. The TOML stores the
**name of an env var** (e.g. ``api_token_env = "JIRA_API_TOKEN"``)
and the actual secret lives in the user's environment.

This module never raises into the prompt path. Unreadable or
malformed configs degrade to "all sources OFF" with a logged
warning, matching the dreamscape config pattern.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_FILENAME = "release-train.toml"

_TRUE = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSE = frozenset({"0", "false", "no", "off", "n", "f"})


def _env_bool(name: str) -> bool | None:
    """Parse a boolean env var. Returns ``None`` when unset/unparseable."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return None


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class JiraSourceConfig:
    """Jira enrichment configuration.

    The default ``mode = "auto"`` resolves at runtime:
      * ``"mcp"`` if a MCP server matching ``mcp_server`` is registered
        in the user's MCP config AND the MCP path is reachable
      * ``"api"`` if ``api_base_url`` is set and the env var named by
        ``api_token_env`` is populated
      * ``"off"`` otherwise (silent skip)
    """

    enabled: bool = False
    """Master toggle for the Jira source."""

    mode: str = "auto"
    """``"auto"`` | ``"mcp"`` | ``"api"`` | ``"off"``."""

    mcp_server: str = "atlassian"
    """Name of the MCP server entry to look up in the user's MCP config."""

    mcp_tool_name: str = ""
    """Optional explicit MCP tool name. Empty = auto-detect by heuristic."""

    api_base_url: str = ""
    """Jira base URL, e.g. ``https://acme.atlassian.net``."""

    api_email_env: str = "JIRA_EMAIL"
    """Name of the env var holding the Jira user email (basic auth username)."""

    api_token_env: str = "JIRA_API_TOKEN"
    """Name of the env var holding the Jira API token (basic auth password)."""

    project_keys: list[str] = field(default_factory=list)
    """When non-empty, only issue keys whose prefix matches one of these is fetched."""

    fields: list[str] = field(
        default_factory=lambda: ["summary", "status", "issuetype", "fixVersions"]
    )
    """Jira issue fields to request from the REST API."""

    issue_key_regex: str = r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b"
    """Regex to extract Jira issue keys from commit subjects + PR titles."""

    max_keys: int = 50
    """Cap on number of unique keys looked up in one /release-train run."""

    timeout_seconds: float = 15.0
    """Per-request timeout for REST/MCP calls."""


@dataclass
class HaloSourceConfig:
    """Halo PSA / Halo ITSM enrichment configuration.

    Halo's REST API uses OAuth2 client-credentials. ``api_client_id_env``
    and ``api_client_secret_env`` name the env vars holding the
    credential pair; the secrets themselves stay in the environment.
    """

    enabled: bool = False
    mode: str = "auto"
    mcp_server: str = "halo"
    mcp_tool_name: str = ""
    api_base_url: str = ""
    """Halo base URL, e.g. ``https://acme.halopsa.com``."""

    api_client_id_env: str = "HALO_CLIENT_ID"
    api_client_secret_env: str = "HALO_CLIENT_SECRET"
    api_tenant: str = ""
    """Halo tenant id, when the deployment uses tenant-scoped tokens."""

    api_scope: str = "all"
    """OAuth2 scope requested when minting tokens. Halo's default is ``all``."""

    ticket_types: list[str] = field(default_factory=list)
    """When non-empty, filter resolved tickets to these types (e.g. ``["change", "incident"]``)."""

    ticket_key_regex: str = r"(?i)\b(?:HALO|CHG|INC|TKT)-?(\d+)\b"
    """Regex to extract Halo ticket keys. The first captured group is the numeric id."""

    max_keys: int = 50
    timeout_seconds: float = 15.0


@dataclass
class ReleaseTrainConfig:
    """Top-level release-train configuration."""

    jira: JiraSourceConfig = field(default_factory=JiraSourceConfig)
    halo: HaloSourceConfig = field(default_factory=HaloSourceConfig)

    @property
    def any_enabled(self) -> bool:
        """True when at least one enrichment source is on."""
        return self.jira.enabled or self.halo.enabled


# ---------------------------------------------------------------------------
# Paths + cache
# ---------------------------------------------------------------------------


def release_train_config_path() -> Path:
    """Canonical path to the release-train TOML."""
    return Path.home() / ".bog-agents" / _FILENAME


_CACHED_CONFIG: ReleaseTrainConfig | None = None
_CACHED_FROM_PATH: Path | None = None


def clear_cache() -> None:
    """Drop the cached config so the next ``load_release_train_config`` re-reads."""
    global _CACHED_CONFIG, _CACHED_FROM_PATH  # noqa: PLW0603
    _CACHED_CONFIG = None
    _CACHED_FROM_PATH = None


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def load_release_train_config(
    path: Path | None = None, *, use_cache: bool = True
) -> ReleaseTrainConfig:
    """Load the release-train config with env-var overrides applied.

    Args:
        path: Override path (tests). Defaults to
            ``~/.bog-agents/release-train.toml``.
        use_cache: When True, return the cached result if the path
            matches a previous call.

    Returns:
        A fully-populated :class:`ReleaseTrainConfig`. NEVER raises —
        unreadable/malformed files yield the default (everything-OFF)
        config with a logged warning.
    """
    global _CACHED_CONFIG, _CACHED_FROM_PATH  # noqa: PLW0603

    target = path or release_train_config_path()
    if use_cache and _CACHED_CONFIG is not None and target == _CACHED_FROM_PATH:
        return _CACHED_CONFIG

    cfg = ReleaseTrainConfig()
    if target.exists():
        try:
            data = tomllib.loads(target.read_text(encoding="utf-8"))
            _apply_toml(cfg, data)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logger.warning(
                "release-train config at %s could not be loaded (%s); "
                "falling back to defaults (all sources OFF)",
                target,
                exc,
            )

    _apply_env_overrides(cfg)

    if use_cache:
        _CACHED_CONFIG = cfg
        _CACHED_FROM_PATH = target
    return cfg


def _apply_toml(cfg: ReleaseTrainConfig, data: dict[str, Any]) -> None:
    """Mutate ``cfg`` in place with values from the parsed TOML."""
    # The top level may either be ``[jira]``/``[halo]`` directly or
    # nested under ``[release_train.jira]``. Support both — the latter
    # mirrors how the docs render it.
    sources: dict[str, Any] = {"jira": cfg.jira, "halo": cfg.halo}
    nested = data.get("release_train")
    if isinstance(nested, dict):
        for name, section in sources.items():
            raw = nested.get(name)
            if isinstance(raw, dict):
                _apply_dict_to_dataclass(section, raw)
    for name, section in sources.items():
        raw = data.get(name)
        if isinstance(raw, dict):
            _apply_dict_to_dataclass(section, raw)


def _apply_dict_to_dataclass(obj: Any, raw: dict[str, Any]) -> None:  # noqa: ANN401
    """Copy values from ``raw`` into ``obj`` for keys that match dataclass fields.

    Unknown keys are ignored (forward-compatible). Type mismatches are
    logged and skipped (the default value survives).
    """
    if not is_dataclass(obj):
        return
    valid_names = {f.name for f in fields(obj)}
    for key, value in raw.items():
        if key not in valid_names:
            continue
        current = getattr(obj, key)
        if not _types_compatible(current, value):
            logger.warning(
                "release-train: ignoring %s.%s — expected %s, got %s",
                type(obj).__name__,
                key,
                type(current).__name__,
                type(value).__name__,
            )
            continue
        setattr(obj, key, value)


def _types_compatible(existing: Any, incoming: Any) -> bool:  # noqa: ANN401
    """Loose type check — bool isn't int even though Python says so."""
    if isinstance(existing, bool):
        return isinstance(incoming, bool)
    if isinstance(existing, int) and not isinstance(existing, bool):
        return isinstance(incoming, int) and not isinstance(incoming, bool)
    if isinstance(existing, float):
        return isinstance(incoming, (int, float)) and not isinstance(incoming, bool)
    if isinstance(existing, str):
        return isinstance(incoming, str)
    if isinstance(existing, list):
        return isinstance(incoming, list)
    return type(existing) is type(incoming)


def _apply_env_overrides(cfg: ReleaseTrainConfig) -> None:
    """Layer env vars on top of file-driven config."""
    jira_flag = _env_bool("BOG_AGENTS_RELEASE_TRAIN_JIRA")
    if jira_flag is not None:
        cfg.jira.enabled = jira_flag
    halo_flag = _env_bool("BOG_AGENTS_RELEASE_TRAIN_HALO")
    if halo_flag is not None:
        cfg.halo.enabled = halo_flag


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def save_release_train_config(
    cfg: ReleaseTrainConfig, *, path: Path | None = None
) -> Path:
    """Write the config back to TOML. Used by ``/release-train enable/disable``."""
    import tomli_w

    target = path or release_train_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _dataclass_to_dict(cfg)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("wb") as fh:
        tomli_w.dump(payload, fh)
    tmp.replace(target)
    clear_cache()
    return target


def _dataclass_to_dict(cfg: ReleaseTrainConfig) -> dict[str, Any]:
    """Serialize the config for TOML."""
    return {
        "release_train": {
            "jira": _section_to_dict(cfg.jira),
            "halo": _section_to_dict(cfg.halo),
        }
    }


def _section_to_dict(obj: Any) -> dict[str, Any]:  # noqa: ANN401
    if not is_dataclass(obj):
        return {}
    out: dict[str, Any] = {}
    for f in fields(obj):
        out[f.name] = getattr(obj, f.name)
    return out
