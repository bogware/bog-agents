"""Canonical manifest and resolver for user-tunable scalar config options.

This module is the single source of truth for the configuration *surface*: the
set of scalar options the CLI reads, their types, typed defaults, environment
variable names, and `config.toml` locations. It is a read-only introspection
layer — it describes where each option comes from and resolves the effective
value; it never mutates config.

`resolve_scalar` is the shared resolution engine used by the `config` headless
command so introspection can never drift from the precedence the app follows:
an environment variable beats `config.toml`, and the typed default is the final
fallback. A malformed numeric value or an unrecognized boolean token is logged
and falls back to the next layer rather than raising, so a bad value never
blocks resolution.

Credentials are *derived* from `model_config.PROVIDER_API_KEY_ENV` rather than
hand-listed, so every provider the app knows how to authenticate automatically
gets a manifest entry and the P0-G registry-sync invariant is preserved: the
credential surface can never drift from the provider/key registry.

Import discipline: the module top level stays stdlib + `_env_vars` only (both
light) so importing the manifest never pulls the heavy `model_config`/agent
runtime onto a fast path. Anything needing `model_config` (the provider
credentials, the config path) is imported lazily inside functions.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents_cli import _env_vars
from bog_agents_cli._env_vars import classify_env_bool

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


class OptionKind(Enum):
    """How an option's raw env/TOML value is coerced to a typed value.

    All kinds flow through `resolve_scalar`. `BOOL` accepts the usual on/off
    tokens; `BOOL_PRESENCE` treats any non-empty env value as enabled; `INT`,
    `FLOAT`, and `STR` coerce inline. An unrecognized/malformed value is logged
    and skipped to the next resolution layer rather than raising.
    """

    BOOL = "bool"
    """Recognized truthy (`1`/`true`/`yes`/`on`) or falsy (`0`/`false`/`no`/`off`)
    tokens; an unrecognized value is logged and skipped to the next layer."""

    BOOL_PRESENCE = "bool_presence"
    """Any non-empty env value enables the flag (regardless of its content)."""

    INT = "int"

    FLOAT = "float"

    STR = "str"


_KIND_TYPE_LABEL: dict[OptionKind, str] = {
    OptionKind.BOOL: "bool",
    OptionKind.BOOL_PRESENCE: "bool",
    OptionKind.INT: "int",
    OptionKind.FLOAT: "float",
    OptionKind.STR: "str",
}

if _KIND_TYPE_LABEL.keys() != set(OptionKind):
    # Fail at import (and in the test suite) rather than KeyError-ing from
    # `ConfigOption.type` only when an unlabeled kind happens to be rendered.
    msg = "_KIND_TYPE_LABEL is missing an OptionKind entry"
    raise RuntimeError(msg)


# Python types accepted for a `ConfigOption.default` of each kind, enforced by
# `ConfigOption.__post_init__`.
_KIND_DEFAULT_TYPES: dict[OptionKind, tuple[type, ...]] = {
    OptionKind.BOOL: (bool,),
    OptionKind.BOOL_PRESENCE: (bool,),
    OptionKind.INT: (int,),
    OptionKind.FLOAT: (int, float),
    OptionKind.STR: (str,),
}


@dataclass(frozen=True)
class ConfigOption:
    """One user-tunable configuration option and where it can be set."""

    key: str
    """Canonical dotted identifier used by `config get` and as the display key."""

    group: str
    """Human-readable grouping for the `config` command listing."""

    summary: str
    """One-line description of what the option controls."""

    kind: OptionKind
    """How env/TOML values are coerced to a typed value."""

    default: Any = None
    """Typed default value, or `None` when there is no static default."""

    none_sentinels: tuple[str, ...] = ()
    """Case-insensitive string values that coerce to `None` (i.e. "disabled").

    For a numeric (`INT`/`FLOAT`) option whose consumer treats a word like
    `none`/`off` as "no limit", listing those words here makes the manifest
    coerce them to `None` instead of warning `Ignoring …=… (expected number)`.
    Keeps the manifest honest with a documented sentinel (e.g. the help text for
    `BOG_AGENTS_REMOTE_READ_TIMEOUT` tells users to set it to `none`).
    """

    env_var: str | None = None
    """Primary environment variable name the loader reads, or `None`."""

    fallback_env_vars: tuple[str, ...] = ()
    """Secondary env vars read (in order) when `env_var` is unset."""

    toml_keys: tuple[str, ...] | None = None
    """Section/key path within `config.toml`, or `None`."""

    redacted: bool = False
    """Whether the `config` command reports only set/not-set, never the raw value.

    Named `redacted` rather than `secret` so the value carries no
    credential-suggesting identifier — the flag is boolean metadata only.
    """

    provider: str | None = None
    """Provider name a credential option authenticates, or `None`.

    Set only for `Credentials`-group options, where it is the provider key from
    `PROVIDER_API_KEY_ENV` the credential belongs to. `None` for every other
    option.
    """

    def __post_init__(self) -> None:
        """Reject a `default` that contradicts `kind` at construction time.

        The manifest is a hand-edited literal table with `default: Any`, so a
        mistyped default (an `INT` option defaulting to a `str`) or a mutable
        one would otherwise slip through to runtime — a wrong-typed default is
        served verbatim by `resolve_scalar`, and a mutable default is shared by
        reference through the `get_config_options` `lru_cache`. Catching it here
        fails the import (and the test suite).

        Raises:
            TypeError: When `fallback_env_vars` is not a tuple of non-empty
                strings, `default` is mutable, or a scalar option's default has
                the wrong type for its `kind`.
        """
        if not isinstance(self.fallback_env_vars, tuple) or any(
            not isinstance(name, str) or not name for name in self.fallback_env_vars
        ):
            msg = (
                f"{self.key}: fallback_env_vars must be a tuple of non-empty "
                f"strings, got {self.fallback_env_vars!r}"
            )
            raise TypeError(msg)

        default = self.default
        if default is None:
            return
        if isinstance(default, (list, dict, set)):
            msg = (
                f"{self.key}: mutable default {default!r} is unsafe under the "
                "shared lru_cache; use an immutable value (e.g. a tuple)"
            )
            raise TypeError(msg)
        expected = _KIND_DEFAULT_TYPES[self.kind]
        # `bool` is an `int` subclass; an INT/FLOAT default must not be a bool.
        if not isinstance(default, expected) or (
            self.kind in {OptionKind.INT, OptionKind.FLOAT}
            and isinstance(default, bool)
        ):
            msg = (
                f"{self.key}: default {default!r} is not valid for kind "
                f"{self.kind.value}"
            )
            raise TypeError(msg)

    @property
    def type(self) -> str:
        """Human-readable type label derived from `kind`."""
        return _KIND_TYPE_LABEL[self.kind]

    @property
    def toml_path(self) -> str | None:
        """Render `toml_keys` as a `[section].key` display string."""
        if not self.toml_keys:
            return None
        *sections, leaf = self.toml_keys
        if not sections:
            return leaf
        return f"[{'.'.join(sections)}].{leaf}"


# --- Resolution -------------------------------------------------------------

_INVALID = object()
"""Sentinel: a raw value failed coercion and the next layer should be tried."""


def load_config_toml() -> dict[str, Any]:
    """Load the user's `~/.bog-agents/config.toml`.

    Returns:
        The parsed config mapping, or `{}` when the file is absent or invalid.
    """
    import tomllib

    from bog_agents_cli.model_config import DEFAULT_CONFIG_PATH

    try:
        with DEFAULT_CONFIG_PATH.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError):
        logger.warning(
            "Could not read config from %s; using defaults for all options",
            DEFAULT_CONFIG_PATH,
            exc_info=True,
        )
        return {}


def _toml_lookup(data: dict[str, Any], keys: tuple[str, ...]) -> tuple[bool, Any]:
    """Navigate nested `keys` in `data`.

    Returns:
        `(found, value)`, where `found` is `False` if any key was missing.
    """
    node: Any = data
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, node


def _coerce_env(option: ConfigOption, raw: str, name: str) -> object:
    """Coerce a raw environment-variable string by the option's kind.

    Returns:
        The typed value, or `_INVALID` when the raw value cannot be coerced.
    """
    kind = option.kind
    # A documented "disabled" sentinel (e.g. `none`/`off` for a timeout) coerces
    # to None rather than tripping the numeric/bool warnings below.
    if option.none_sentinels and raw.strip().lower() in option.none_sentinels:
        return None
    if kind is OptionKind.BOOL:
        classified = classify_env_bool(raw)
        if classified is None:
            logger.warning("Ignoring %s=%r (expected bool)", name, raw)
            return _INVALID
        return classified
    if kind is OptionKind.BOOL_PRESENCE:
        return bool(raw)
    if kind is OptionKind.STR:
        return raw
    if kind is OptionKind.INT:
        try:
            return int(raw.strip())
        except ValueError:
            logger.warning("Ignoring %s=%r (expected int)", name, raw)
            return _INVALID
    if kind is OptionKind.FLOAT:
        try:
            return float(raw.strip())
        except ValueError:
            logger.warning("Ignoring %s=%r (expected number)", name, raw)
            return _INVALID
    # Unreachable: every OptionKind is handled above. Fall back defensively.
    logger.warning("Unhandled option kind %r for %s", kind, name)
    return _INVALID


def _coerce_toml(option: ConfigOption, raw: object) -> object:
    """Coerce a raw TOML value by the option's kind, logging on mismatch.

    Returns:
        The typed value, or `_INVALID` when the raw value has the wrong shape.
    """
    kind = option.kind
    label = option.toml_path or option.key

    if (
        option.none_sentinels
        and isinstance(raw, str)
        and raw.strip().lower() in option.none_sentinels
    ):
        return None
    if kind in {OptionKind.BOOL, OptionKind.BOOL_PRESENCE}:
        if isinstance(raw, bool):
            return raw
    elif kind is OptionKind.INT:
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
    elif kind is OptionKind.FLOAT:
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
    elif kind is OptionKind.STR and isinstance(raw, str):
        return raw

    logger.warning(
        "Ignoring %s=%r in config.toml (expected %s)", label, raw, option.type
    )
    return _INVALID


def resolve_scalar(
    option: ConfigOption, *, toml_data: dict[str, Any]
) -> tuple[Any, str]:
    """Resolve an option against the environment then `config.toml`.

    Args:
        option: The option to resolve.
        toml_data: Parsed `config.toml` mapping (see `load_config_toml`).

    Resolution order is: the primary `env_var`, then each `fallback_env_vars`
    name in declaration order, then `config.toml`, then the typed `default`.

    Returns:
        `(value, source)`, where `source` is `env (<name>)`, `config.toml`, or
        `default`. A malformed `int`/`float` value, an unrecognized boolean
        token, or a TOML value of the wrong type is logged and skipped so the
        next layer (or the typed default) applies. An empty env value is treated
        as unset, so it falls through to the next name rather than counting as
        set.
    """
    names: list[str] = []
    if option.env_var:
        names.append(option.env_var)
    names.extend(option.fallback_env_vars)
    for name in names:
        raw = os.environ.get(name)
        if not raw:
            continue
        value = _coerce_env(option, raw, name)
        if value is not _INVALID:
            return value, f"env ({name})"

    if option.toml_keys:
        found, raw = _toml_lookup(toml_data, option.toml_keys)
        if found:
            value = _coerce_toml(option, raw)
            if value is not _INVALID:
                return value, "config.toml"

    return option.default, "default"


# --- Option definitions -----------------------------------------------------


def _credential_options() -> tuple[ConfigOption, ...]:
    """Build credential options from the canonical provider/key registry.

    Generating these from `PROVIDER_API_KEY_ENV` (rather than hand-listing
    them) guarantees every provider the app knows how to authenticate has a
    manifest entry — new providers can never silently miss the config surface —
    and keeps the credential surface locked to the P0-G registry-sync invariant.

    Returns:
        One redacted credential `ConfigOption` per known provider/key env var,
        deduplicated by env var (providers sharing a key collapse to one entry).
    """
    from bog_agents_cli.model_config import PROVIDER_API_KEY_ENV

    options: list[ConfigOption] = []
    seen: set[str] = set()
    for provider, env_var in sorted(PROVIDER_API_KEY_ENV.items()):
        if env_var in seen:
            continue
        seen.add(env_var)
        options.append(
            ConfigOption(
                key=f"credentials.{provider}",
                group="Credentials",
                summary=f"API credential for the {provider} provider ({env_var}).",
                kind=OptionKind.STR,
                env_var=env_var,
                redacted=True,
                provider=provider,
            )
        )
    return tuple(options)


# Static (non-credential) options, grouped by domain. Every `env_var` references
# a `_env_vars` constant so the name has a single source of truth.
_STATIC_OPTIONS: tuple[ConfigOption, ...] = (
    # --- Models --------------------------------------------------------
    ConfigOption(
        key="models.default",
        group="Models",
        summary="Intentional default model spec ('provider:model') used at launch.",
        kind=OptionKind.STR,
        toml_keys=("models", "default"),
    ),
    ConfigOption(
        key="models.recent",
        group="Models",
        summary="Most recently switched-to model (managed by the app).",
        kind=OptionKind.STR,
        toml_keys=("models", "recent"),
    ),
    ConfigOption(
        key="models.apply",
        group="Models",
        summary="Small/fast model spec used for apply-style edits.",
        kind=OptionKind.STR,
        toml_keys=("models", "apply"),
    ),
    ConfigOption(
        key="models.plan",
        group="Models",
        summary="Model spec used for plan-mode reasoning.",
        kind=OptionKind.STR,
        toml_keys=("models", "plan"),
    ),
    ConfigOption(
        key="models.disable_streaming",
        group="Models",
        summary="Disable token-level model response streaming.",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.DISABLE_MODEL_STREAMING,
    ),
    ConfigOption(
        key="models.read_timeout",
        group="Models",
        summary="Read-timeout (seconds) for model HTTP requests.",
        kind=OptionKind.FLOAT,
        env_var=_env_vars.MODEL_READ_TIMEOUT,
    ),
    ConfigOption(
        key="models.thinking",
        group="Models",
        summary="Enable extended thinking / reasoning for the model.",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.THINKING,
    ),
    ConfigOption(
        key="models.thinking_budget",
        group="Models",
        summary="Token budget for extended thinking / reasoning.",
        kind=OptionKind.INT,
        env_var=_env_vars.THINKING_BUDGET,
    ),
    # --- Tracing -------------------------------------------------------
    ConfigOption(
        key="tracing.langsmith_project",
        group="Tracing",
        summary="LangSmith project name for agent traces.",
        kind=OptionKind.STR,
        env_var=_env_vars.LANGSMITH_PROJECT,
        fallback_env_vars=("LANGSMITH_PROJECT",),
    ),
    # --- Tools ---------------------------------------------------------
    ConfigOption(
        key="tools.auto_install",
        group="Tools",
        summary="Auto-install managed tool binaries (e.g. ripgrep) when missing.",
        kind=OptionKind.BOOL,
        default=True,
        toml_keys=("tools", "auto_install"),
    ),
    ConfigOption(
        key="tools.disable_subagents",
        group="Tools",
        summary="Disable spawning of sub-agents (the task/sub-agent tool).",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.DISABLE_SUBAGENTS,
    ),
    ConfigOption(
        key="tools.fs_unsandboxed",
        group="Tools",
        summary="Allow filesystem tools to operate outside the sandboxed root.",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.FS_UNSANDBOXED,
    ),
    ConfigOption(
        key="approvals.timeout_seconds",
        group="Tools",
        summary="Seconds an approval prompt waits before auto-rejecting (fail-closed). Unset = wait forever.",
        kind=OptionKind.FLOAT,
        none_sentinels=("none", "off"),
        env_var=_env_vars.APPROVAL_TIMEOUT,
        toml_keys=("approvals", "timeout_seconds"),
    ),
    ConfigOption(
        key="tools.powershell",
        group="Tools",
        summary="Register the opt-in `powershell` tool (pwsh / Windows PowerShell via argv, never cmd.exe). No-op when PowerShell is absent.",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.POWERSHELL_TOOL,
        toml_keys=("tools", "powershell"),
    ),
    ConfigOption(
        key="tools.code_mode",
        group="Tools",
        summary="Register the governed `run_code` tool: the model scripts tool calls in a child interpreter; every call re-enters the tool path (ROADMAP #72). Off under --restricted.",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.CODE_MODE,
        toml_keys=("tools", "code_mode"),
    ),
    ConfigOption(
        key="web.allowed_domains",
        group="Tools",
        summary="Comma-separated domains the web tools may fetch (suffix-matched: example.com covers api.example.com). Empty = any public host.",
        kind=OptionKind.STR,
        env_var=_env_vars.WEB_ALLOWED_DOMAINS,
        toml_keys=("web", "allowed_domains"),
    ),
    ConfigOption(
        key="web.blocked_domains",
        group="Tools",
        summary="Comma-separated domains the web tools must never fetch; wins over the allow-list.",
        kind=OptionKind.STR,
        env_var=_env_vars.WEB_BLOCKED_DOMAINS,
        toml_keys=("web", "blocked_domains"),
    ),
    ConfigOption(
        key="compliance.action_log",
        group="Tools",
        summary="Write every model call, tool call, approval and Expert verdict into a hash-chained JSONL under ~/.bog-agents/action-log (ROADMAP #74).",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.ACTION_LOG,
        toml_keys=("compliance", "action_log"),
    ),
    ConfigOption(
        key="compliance.retention_days",
        group="Tools",
        summary="Days to keep action-log chains before `/actionlog prune` removes them.",
        kind=OptionKind.INT,
        default=90,
        toml_keys=("compliance", "retention_days"),
    ),
    ConfigOption(
        key="compliance.otel_endpoint",
        group="Tools",
        summary="OTLP/HTTP collector base URL; GenAI-semconv spans for model, tool and subagent calls are posted there.",
        kind=OptionKind.STR,
        env_var=_env_vars.OTEL_ENDPOINT,
        toml_keys=("compliance", "otel_endpoint"),
    ),
    ConfigOption(
        key="tools.timeout",
        group="Tools",
        summary="Default per-tool execution timeout (seconds).",
        kind=OptionKind.FLOAT,
        env_var=_env_vars.TOOL_TIMEOUT,
    ),
    ConfigOption(
        key="shell.allow_list",
        group="Tools",
        summary=(
            "Shell commands allowed without approval (comma-separated, or "
            "'recommended'/'all')."
        ),
        kind=OptionKind.STR,
        env_var=_env_vars.SHELL_ALLOW_LIST,
    ),
    # --- MCP -----------------------------------------------------------
    ConfigOption(
        key="mcp.startup_timeout",
        group="MCP",
        summary="Seconds to wait for an MCP server to start before giving up.",
        kind=OptionKind.FLOAT,
        env_var=_env_vars.MCP_STARTUP_TIMEOUT,
    ),
    ConfigOption(
        key="mcp.trust",
        group="MCP",
        summary="Pre-approve project MCP servers (trust decision) when truthy.",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.MCP_TRUST,
    ),
    # --- Operator ------------------------------------------------------
    ConfigOption(
        key="operator.enabled",
        group="Operator",
        summary="Enable Operator prompt-routing mode by default.",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.OPERATOR,
    ),
    ConfigOption(
        key="operator.disable",
        group="Operator",
        summary="Emergency kill switch for Operator mode (beats every toggle).",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.OPERATOR_DISABLE,
    ),
    # --- Dreamscape ----------------------------------------------------
    ConfigOption(
        key="dreamscape.enabled",
        group="Dreamscape",
        summary="Master enable for the Dreamscape subsystem (opt-in).",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.DREAMSCAPE,
    ),
    ConfigOption(
        key="dreamscape.disable",
        group="Dreamscape",
        summary="Emergency kill switch forcing every Dreamscape subsystem off.",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.DREAMSCAPE_DISABLE,
    ),
    # --- Bedrock -------------------------------------------------------
    ConfigOption(
        key="bedrock.auth_mode",
        group="Bedrock",
        summary="AWS Bedrock authentication mode (e.g. 'profile' vs default chain).",
        kind=OptionKind.STR,
        env_var=_env_vars.BEDROCK_AUTH_MODE,
    ),
    ConfigOption(
        key="bedrock.profile",
        group="Bedrock",
        summary="Named AWS profile to use for Bedrock credentials.",
        kind=OptionKind.STR,
        env_var=_env_vars.BEDROCK_PROFILE,
    ),
    ConfigOption(
        key="bedrock.no_probe",
        group="Bedrock",
        summary="Skip the Bedrock model-reachability probe at startup.",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.BEDROCK_NO_PROBE,
    ),
    # --- Runtime -------------------------------------------------------
    ConfigOption(
        key="runtime.offline",
        group="Runtime",
        summary="Disable managed binary downloads and use local fallbacks.",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.OFFLINE,
    ),
    ConfigOption(
        key="runtime.turn_timeout_seconds",
        group="Runtime",
        summary="Wall-clock timeout (seconds) for a single agent turn.",
        kind=OptionKind.FLOAT,
        env_var=_env_vars.TURN_TIMEOUT_SECONDS,
    ),
    ConfigOption(
        key="runtime.stream_chunk_timeout_seconds",
        group="Runtime",
        summary="Per-chunk timeout (seconds) for non-interactive streaming output.",
        kind=OptionKind.FLOAT,
        env_var=_env_vars.STREAM_CHUNK_TIMEOUT_SECONDS,
    ),
    ConfigOption(
        key="runtime.stall_dump_secs",
        group="Runtime",
        summary="Seconds of apparent stall before the server dumps diagnostics.",
        kind=OptionKind.FLOAT,
        env_var=_env_vars.STALL_DUMP_SECS,
    ),
    ConfigOption(
        key="runtime.remote_read_timeout",
        group="Runtime",
        summary="Read-timeout (seconds) for the remote client HTTP requests; `none` disables it.",
        kind=OptionKind.FLOAT,
        env_var=_env_vars.REMOTE_READ_TIMEOUT,
        none_sentinels=("none", "off"),
    ),
    ConfigOption(
        key="runtime.shell_auto_background_after",
        group="Runtime",
        summary="Seconds before a slow foreground shell command is moved to the background instead of killed; off by default (opt in with e.g. `60`), `off`/`0` disables.",
        kind=OptionKind.FLOAT,
        env_var=_env_vars.SHELL_AUTO_BACKGROUND_AFTER,
        none_sentinels=("off", "none"),
    ),
    ConfigOption(
        key="ui.vim_mode",
        group="UI",
        summary="Vim-style modal editing (normal/insert) in the chat input.",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.VIM_MODE,
        toml_keys=("ui", "vim_mode"),
    ),
    ConfigOption(
        key="runtime.memory_vector",
        group="Runtime",
        summary="Light up the vector path in `memory_search` (needs an embeddings-capable provider); keyword-only when off.",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.MEMORY_VECTOR,
    ),
    ConfigOption(
        key="runtime.stop_gate_checks",
        group="Runtime",
        summary="Semicolon-separated commands that must pass before the agent may finish a turn (e.g. 'uv run pytest -q').",
        kind=OptionKind.STR,
        env_var=_env_vars.STOP_GATE_CHECKS,
    ),
    # --- Updates -------------------------------------------------------
    ConfigOption(
        key="update.no_update_check",
        group="Updates",
        summary="Disable the automatic update check at startup.",
        kind=OptionKind.BOOL_PRESENCE,
        default=False,
        env_var=_env_vars.NO_UPDATE_CHECK,
    ),
    # --- UI ------------------------------------------------------------
    ConfigOption(
        key="ui.sounds",
        group="UI",
        summary="Play a notification sound when the agent finishes (off by default; opt in with 1/true).",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.SOUNDS,
    ),
    # --- Hooks ---------------------------------------------------------
    ConfigOption(
        key="hooks.trust_project",
        group="Hooks",
        summary="Pre-trust project-level hooks (skip the approval prompt).",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.TRUST_PROJECT_HOOKS,
    ),
    # --- Paths ---------------------------------------------------------
    ConfigOption(
        key="paths.home",
        group="Paths",
        summary="Base bog-agents home directory (config, vault, MCP OAuth tokens, state, sessions db); read at startup.",
        kind=OptionKind.STR,
        default=str(Path.home() / ".bog-agents"),
        env_var=_env_vars.HOME,
    ),
    ConfigOption(
        key="paths.project_root",
        group="Paths",
        summary="Override the detected project root directory.",
        kind=OptionKind.STR,
        env_var=_env_vars.PROJECT_ROOT,
    ),
    # --- Logging / Debug -----------------------------------------------
    ConfigOption(
        key="log.level",
        group="Debug",
        summary="Root logging level (e.g. 'DEBUG', 'INFO').",
        kind=OptionKind.STR,
        default="INFO",
        env_var=_env_vars.LOG_LEVEL,
    ),
    ConfigOption(
        key="debug.enabled",
        group="Debug",
        summary="Enable verbose debug logging.",
        kind=OptionKind.BOOL,
        default=False,
        env_var=_env_vars.DEBUG,
    ),
    ConfigOption(
        key="debug.file",
        group="Debug",
        summary="Path for the debug log file when debug logging is active.",
        kind=OptionKind.STR,
        env_var=_env_vars.DEBUG_FILE,
    ),
    # --- Cost certainty (ROADMAP #51) ---
    ConfigOption(
        key="cost.budget_usd",
        group="Cost",
        summary="Session cost cap in USD; the agent pauses with a budget_reached prompt when it is hit. Unset = unlimited.",
        kind=OptionKind.FLOAT,
        none_sentinels=("none", "off", "unlimited"),
        env_var=_env_vars.BUDGET_USD,
        toml_keys=("cost", "budget_usd"),
    ),
    ConfigOption(
        key="cost.warn_at_percent",
        group="Cost",
        summary="Percent of a budget or daily ceiling at which /cost and the turn gate start warning.",
        kind=OptionKind.INT,
        default=80,
        env_var=_env_vars.BUDGET_WARN_AT_PERCENT,
        toml_keys=("cost", "warn_at_percent"),
    ),
    ConfigOption(
        key="cost.daily_ceiling_usd",
        group="Cost",
        summary="Per-day ceiling in USD for this user across sessions; new turns are refused once reached. Unset = unlimited.",
        kind=OptionKind.FLOAT,
        none_sentinels=("none", "off", "unlimited"),
        env_var=_env_vars.DAILY_CEILING_USD,
        toml_keys=("cost", "daily_ceiling_usd"),
    ),
    ConfigOption(
        key="cost.max_subagents",
        group="Cost",
        summary="Subagent/teammate spawns allowed per session (CostLedger runaway cap); 'none' lifts it.",
        kind=OptionKind.INT,
        default=8,
        none_sentinels=("none", "unlimited"),
        env_var=_env_vars.MAX_SUBAGENTS,
        toml_keys=("cost", "max_subagents"),
    ),
    ConfigOption(
        key="cost.max_web_searches",
        group="Cost",
        summary="Web searches allowed per session (CostLedger runaway cap); 'none' lifts it.",
        kind=OptionKind.INT,
        default=50,
        none_sentinels=("none", "unlimited"),
        env_var=_env_vars.MAX_WEB_SEARCHES,
        toml_keys=("cost", "max_web_searches"),
    ),
    ConfigOption(
        key="cost.preflight_threshold_usd",
        group="Cost",
        summary="Projected spend (high estimate) above which /team run, /butcher and /best-of-n ask for confirmation first; 'off' disables.",
        kind=OptionKind.FLOAT,
        default=1.0,
        none_sentinels=("none", "off"),
        env_var=_env_vars.PREFLIGHT_THRESHOLD_USD,
        toml_keys=("cost", "preflight_threshold_usd"),
    ),
)


@lru_cache(maxsize=1)
def get_config_options() -> tuple[ConfigOption, ...]:
    """Return every option, credentials-first then by domain group.

    Cached: provider credentials are generated once from `PROVIDER_API_KEY_ENV`
    on first call (which lazily imports `model_config`). The cache assumes that
    registry is an immutable module constant; a test that monkeypatches it must
    call `get_config_options.cache_clear()` and `_options_by_key.cache_clear()`.
    """
    return _credential_options() + _STATIC_OPTIONS


@lru_cache(maxsize=1)
def _options_by_key() -> dict[str, ConfigOption]:
    return {opt.key: opt for opt in get_config_options()}


def get_option(key: str) -> ConfigOption | None:
    """Return the manifest entry for `key`, or `None` when unknown."""
    return _options_by_key().get(key)


def resolve_option(key: str) -> Any:  # noqa: ANN401 - typed per option kind
    """Resolve one manifest option to its effective typed value.

    The consumer-side twin of the `config` command's introspection: env var,
    then `config.toml`, then the typed default, with the same coercion and
    the same `none_sentinels`.

    Args:
        key: A manifest key such as `cost.budget_usd`.

    Returns:
        The effective value (possibly `None`).

    Raises:
        KeyError: If `key` is not in the manifest.
    """
    option = get_option(key)
    if option is None:
        raise KeyError(key)
    value, _source = resolve_scalar(option, toml_data=load_config_toml())
    return value


def option_keys() -> tuple[str, ...]:
    """Return every manifest key in definition order."""
    return tuple(opt.key for opt in get_config_options())


def iter_groups(options: Iterable[ConfigOption]) -> list[str]:
    """Return group names from `options` in first-seen order."""
    groups: list[str] = []
    for opt in options:
        if opt.group not in groups:
            groups.append(opt.group)
    return groups
