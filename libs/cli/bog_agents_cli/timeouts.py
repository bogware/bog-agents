"""User-facing timeout settings for long-running model calls and tool execution.

Three pieces of work fight against premature ReadTimeoutError on long turns:

- The **model** layer (``BOG_AGENTS_MODEL_READ_TIMEOUT``) controls how long
  the LLM HTTP client will wait for the provider to respond.
- The **remote** layer (``BOG_AGENTS_REMOTE_READ_TIMEOUT``) controls how
  long the CLI's ``RemoteGraph`` SSE stream will wait between chunks before
  it gives up. NB: this is a per-chunk read deadline — a healthy stream
  with regular keepalives stays alive forever under it.
- The **tool** layer (``LocalShellBackend(timeout=...)``) controls how
  long a single shell command can run before being killed.

This module centralises the configuration surface so users have one
place — ``~/.bog-agents/settings.json`` ``[timeouts]`` — to tune all of
them. We translate settings into the env vars the SDK already consumes
so the SDK does not need to know about the CLI's settings cascade.

Cascade precedence (highest first):

1. The env var itself, if it was already set in the user's shell.
2. ``<project>/.bog-agents/settings.json`` ``timeouts`` section.
3. ``~/.bog-agents/settings.json`` ``timeouts`` section.
4. Built-in defaults (2 hours for model + remote, 2 hours for tools).

A value of ``"none"``, ``"off"``, or ``0`` disables that timeout entirely.

Example ``settings.json``::

    {"timeouts": {"model_read_seconds": 7200, "remote_read_seconds": 7200, "tool_seconds": "none"}}
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from bog_agents_cli._settings_cascade import load_layered_section

logger = logging.getLogger(__name__)


# Defaults.
#
# - ``model_read_seconds`` is the **per-chunk** read deadline for model
#   HTTP streams. With Anthropic streaming, healthy responses send a
#   chunk (or keepalive) within a few hundred milliseconds — a gap of
#   minutes is a hung stream, not a slow model. The previous default
#   was 7200s (2 hours), which made a hung stream effectively
#   permanent: the user always killed the run before the timeout
#   fired, leaving them with no actionable error. 600s (10 min) is
#   well above any legitimate inter-chunk gap (extended thinking,
#   high-effort responses) while still giving the user a visible
#   ``ReadTimeout`` rather than an open-ended stall.
#
# - ``remote_read_seconds`` is the SSE deadline between the CLI and
#   the langgraph dev server. Same reasoning, same value.
#
# - ``tool_seconds`` covers shell-tool execution. Long builds and
#   test suites are legitimate; we keep this generous.
#
# Override any of these via env (``BOG_AGENTS_MODEL_READ_TIMEOUT=7200``
# restores the previous behaviour) or per-project settings.json.
_DEFAULT_MODEL_READ_SECS = 600
_DEFAULT_REMOTE_READ_SECS = 600
_DEFAULT_TOOL_SECS = 7200

_MODEL_ENV = "BOG_AGENTS_MODEL_READ_TIMEOUT"
_REMOTE_ENV = "BOG_AGENTS_REMOTE_READ_TIMEOUT"
_TOOL_ENV = "BOG_AGENTS_TOOL_TIMEOUT"

_DISABLED_TOKENS = {"none", "off", "0", ""}


@dataclass(frozen=True)
class TimeoutSettings:
    """Resolved timeout configuration in seconds.

    A value of ``None`` means "no timeout" (caller should pass through to
    httpx / subprocess as ``None`` / unlimited). A positive integer is the
    deadline in seconds.
    """

    model_read_seconds: int | None = _DEFAULT_MODEL_READ_SECS
    remote_read_seconds: int | None = _DEFAULT_REMOTE_READ_SECS
    tool_seconds: int | None = _DEFAULT_TOOL_SECS

    def merge_dict(self, override: dict[str, Any]) -> TimeoutSettings:
        """Merge a settings.json ``timeouts`` block into this config.

        Unknown keys are ignored with a debug log so future schema additions
        don't crash older CLIs. Bad values fall back to the existing setting
        with a warning so a typo in settings.json never turns into a
        runtime error mid-turn.
        """
        kwargs: dict[str, Any] = {}
        for key in ("model_read_seconds", "remote_read_seconds", "tool_seconds"):
            if key not in override:
                continue
            value = _coerce_value(key, override[key])
            # Skip the sentinel: malformed entries leave the existing
            # value untouched rather than overwriting it with garbage.
            if value is _SENTINEL_KEEP:
                continue
            kwargs[key] = value
        unknown = set(override) - {
            "model_read_seconds",
            "remote_read_seconds",
            "tool_seconds",
        }
        if unknown:
            logger.debug("timeouts: ignoring unknown setting keys %s", sorted(unknown))
        return replace(self, **kwargs) if kwargs else self


def _coerce_value(key: str, raw: Any) -> int | None:  # noqa: ANN401  # JSON values are typed as Any
    """Coerce a JSON value to a non-negative int, or ``None`` for disabled."""
    if isinstance(raw, str) and raw.strip().lower() in _DISABLED_TOKENS:
        return None
    if isinstance(raw, bool):
        # ``bool`` is an ``int`` subclass — reject it explicitly so
        # ``"tool_seconds": false`` is treated as a typo, not as 0.
        logger.warning(
            "timeouts: %s=%r is a bool — expected int seconds or 'none'; ignoring",
            key,
            raw,
        )
        return _SENTINEL_KEEP
    if isinstance(raw, (int, float)):
        value = int(raw)
        if value <= 0:
            return None
        return value
    if isinstance(raw, str):
        try:
            value = int(raw.strip())
        except ValueError:
            logger.warning("timeouts: %s=%r is not a valid int; ignoring", key, raw)
            return _SENTINEL_KEEP
        return None if value <= 0 else value
    logger.warning(
        "timeouts: %s=%r has unsupported type %s; ignoring",
        key,
        raw,
        type(raw).__name__,
    )
    return _SENTINEL_KEEP


# Sentinel returned from ``_coerce_value`` when the override is malformed.
# Callers detect it and skip the assignment so the existing value survives.
_SENTINEL_KEEP: Any = object()


def load_timeout_settings(*, project_root: Path | None = None) -> TimeoutSettings:
    """Resolve the cascade-merged timeout configuration."""

    def _merge(current: TimeoutSettings, override: dict[str, Any]) -> TimeoutSettings:
        # Drop sentinels before passing through so malformed entries don't
        # clobber the prior layer's good value.
        cleaned = {
            k: v
            for k, v in current.merge_dict(override).__dict__.items()
            if v is not _SENTINEL_KEEP
        }
        return TimeoutSettings(**cleaned)

    return load_layered_section(
        section="timeouts",
        initial=TimeoutSettings(),
        merge=_merge,
        project_root=project_root,
    )


def apply_to_env(
    settings: TimeoutSettings | None = None,
    *,
    project_root: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Translate resolved timeout settings into env vars the SDK consumes.

    Existing env values win — this respects the user's explicit shell
    overrides. Only env vars that are *currently unset* get populated from
    the cascade. ``"none"`` becomes the literal env string ``"none"`` so
    the SDK's parser disables the deadline.

    Args:
        settings: Pre-resolved settings. When ``None`` the cascade is read.
        project_root: Optional project dir for the cascade walk.
        env: Override target environment (defaults to ``os.environ``).
            Tests pass an empty dict to verify behaviour without touching
            the real process env.
    """
    target = env if env is not None else os.environ
    if settings is None:
        settings = load_timeout_settings(project_root=project_root)

    for key, env_name in (
        ("model_read_seconds", _MODEL_ENV),
        ("remote_read_seconds", _REMOTE_ENV),
        ("tool_seconds", _TOOL_ENV),
    ):
        if env_name in target:
            continue
        value = getattr(settings, key)
        target[env_name] = "none" if value is None else str(value)


def resolve_tool_timeout() -> int | None:
    """Return the effective tool-execution timeout in seconds, or ``None``.

    Callers (e.g. ``LocalShellBackend`` setup at CLI launch) use this to
    pick the runtime ``timeout`` for new shell-backed sandboxes. Mirrors
    the env-var logic in ``_models.py`` so the contract is identical.
    """
    raw = os.environ.get(_TOOL_ENV)
    if raw is None:
        return _DEFAULT_TOOL_SECS
    if raw.strip().lower() in _DISABLED_TOKENS:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r, falling back to default %ds",
            _TOOL_ENV,
            raw,
            _DEFAULT_TOOL_SECS,
        )
        return _DEFAULT_TOOL_SECS
    return None if value <= 0 else value
