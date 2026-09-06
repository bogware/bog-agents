"""Canonical registry of `BOG_AGENTS_*` environment variables.

Every environment variable the CLI reads whose name starts with
`BOG_AGENTS_` should be defined here as a module-level constant. A
drift-detection test (`tests/unit_tests/test_env_vars.py`) greps the package
for bare `"BOG_AGENTS_*"` string literals and asserts each one appears as a
value in this registry, so a *new* variable cannot be introduced without also
registering it here.

This module is the single source of truth for the variable *names*. Migrating
existing call sites to import these constants (instead of re-typing the string
literal) is an incremental cleanup — the registry does not require it, it only
guards against unregistered new variables.

Import the short-name constants (e.g. `OFFLINE`, `DEBUG`) and pass them to
`os.environ.get()` instead of using raw string literals. If a variable is ever
renamed, only the value here changes.

Shared boolean parsing helpers (`is_env_truthy`, `env_bool`, `classify_env_bool`)
match the `1`/`true`/`yes`/`on` (case-insensitive, trimmed) convention already
used across the codebase (see `operator_mode.is_emergency_disabled`,
`managed_tools.is_offline`, `dreamscape.config._env_bool`) so a future migration
to these helpers is behaviour-preserving.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants — import these instead of bare string literals.
# Keep alphabetically sorted by constant name.
# ---------------------------------------------------------------------------

ACTION_LOG = "BOG_AGENTS_ACTION_LOG"
"""Write the hash-chained action log (`~/.bog-agents/action-log`, ROADMAP #74) when truthy."""

APPROVAL_TIMEOUT = "BOG_AGENTS_APPROVAL_TIMEOUT"
"""Seconds an approval prompt waits before auto-rejecting (fail-closed); unset = wait forever (#49)."""

BEDROCK_AUTH_MODE = "BOG_AGENTS_BEDROCK_AUTH_MODE"
"""Select the AWS Bedrock authentication mode (e.g. `profile` vs. default chain)."""

BEDROCK_NO_PROBE = "BOG_AGENTS_BEDROCK_NO_PROBE"
"""Skip the Bedrock model-reachability probe at startup when truthy."""

BEDROCK_PROFILE = "BOG_AGENTS_BEDROCK_PROFILE"
"""Named AWS profile to use for Bedrock credentials."""

BUDGET_USD = "BOG_AGENTS_BUDGET_USD"
"""Session cost cap in USD; the agent pauses with a `budget_reached` prompt when it is hit (ROADMAP #51)."""

BUDGET_WARN_AT_PERCENT = "BOG_AGENTS_BUDGET_WARN_AT_PERCENT"
"""Percent of a budget or daily ceiling at which `/cost` and the turn gate start warning (#51)."""

CODE_MODE = "BOG_AGENTS_CODE_MODE"
"""Register the governed `run_code` tool (ROADMAP #72) when truthy; never under --restricted."""

DAILY_CEILING_USD = "BOG_AGENTS_DAILY_CEILING_USD"
"""Per-day spend ceiling in USD for this user across sessions; new turns are refused once reached (#51)."""

DEBUG = "BOG_AGENTS_DEBUG"
"""Enable verbose debug logging.

Parsed as a boolean: `1`/`true`/`yes`/`on` (case-insensitive) count as enabled.
"""

DEBUG_FILE = "BOG_AGENTS_DEBUG_FILE"
"""Path for the debug log file when debug logging is active."""

DISABLE_MODEL_STREAMING = "BOG_AGENTS_DISABLE_MODEL_STREAMING"
"""Disable token-level model response streaming when truthy."""

DISABLE_SUBAGENTS = "BOG_AGENTS_DISABLE_SUBAGENTS"
"""Disable spawning of sub-agents (the task/sub-agent tool) when truthy."""

DREAMSCAPE = "BOG_AGENTS_DREAMSCAPE"
"""Master enable for the Dreamscape subsystem; overrides `enabled = false` in
`~/.bog-agents/dreamscape.toml`."""

DREAMSCAPE_DISABLE = "BOG_AGENTS_DREAMSCAPE_DISABLE"
"""Emergency kill switch for Dreamscape. When truthy, every dreamscape
subsystem is forced off regardless of file contents or other env vars."""

DREAMSCAPE_DREAMS = "BOG_AGENTS_DREAMSCAPE_DREAMS"
"""Per-feature toggle for dream generation."""

DREAMSCAPE_DREAMS_AUTO = "BOG_AGENTS_DREAMSCAPE_DREAMS_AUTO"
"""Toggle automatic (dormancy-triggered) dream generation."""

DREAMSCAPE_IMAGINATION = "BOG_AGENTS_DREAMSCAPE_IMAGINATION"
"""Per-feature toggle for imagination injection."""

DREAMSCAPE_LAWS = "BOG_AGENTS_DREAMSCAPE_LAWS"
"""Per-feature toggle for the two-tier laws/constitution enforcement."""

DREAMSCAPE_LIFECYCLE = "BOG_AGENTS_DREAMSCAPE_LIFECYCLE"
"""Per-feature toggle for the agent lifecycle state machine."""

DREAMSCAPE_SHARED_MEMORY = "BOG_AGENTS_DREAMSCAPE_SHARED_MEMORY"
"""Per-feature toggle for the shared-memory backend."""

EXTENSION_MANIFEST = "BOG_AGENTS_EXTENSION_MANIFEST"
"""Path to a Claude-Code-compatible extension manifest to load."""

FS_UNSANDBOXED = "BOG_AGENTS_FS_UNSANDBOXED"
"""Allow the agent filesystem tools to operate outside the sandboxed root when
truthy. Loosens a safety boundary; intended for trusted local use."""

HOME = "BOG_AGENTS_HOME"
"""Override the base `~/.bog-agents` home directory (config, vault, state).

Read through `bog_agents_home()` — new code must call that helper instead of
hardcoding `Path.home() / ".bog-agents"` so the override keeps its promise.
"""

LANGSMITH_PROJECT = "BOG_AGENTS_LANGSMITH_PROJECT"
"""Override the LangSmith project name for agent traces."""

LOG_LEVEL = "BOG_AGENTS_LOG_LEVEL"
"""Override the root logging level (e.g. `DEBUG`, `INFO`)."""

MANAGED_POLICY = "BOG_AGENTS_MANAGED_POLICY"
"""URL or path of the org's signed managed policy document (ROADMAP #50)."""

MANAGED_POLICY_KEY = "BOG_AGENTS_MANAGED_POLICY_KEY"
"""Base64 Ed25519 public key the managed policy must be signed with; required for URL sources."""

MAX_SUBAGENTS = "BOG_AGENTS_MAX_SUBAGENTS"
"""Subagent/teammate spawns allowed per session — the CostLedger runaway cap (#51)."""

MAX_WEB_SEARCHES = "BOG_AGENTS_MAX_WEB_SEARCHES"
"""Web searches allowed per session — the CostLedger runaway cap (#51)."""

MCP_STARTUP_TIMEOUT = "BOG_AGENTS_MCP_STARTUP_TIMEOUT"
"""Seconds to wait for an MCP server to start before giving up."""

MCP_TRUST = "BOG_AGENTS_MCP_TRUST"
"""Pre-approve project MCP servers (trust decision) when truthy."""

MEMORY_VECTOR = "BOG_AGENTS_MEMORY_VECTOR"
"""Enable hybrid vector search in the memory_search tool (needs an embedder)."""

MODEL = "BOG_AGENTS_MODEL"
"""Override the active model identifier."""

MODEL_READ_TIMEOUT = "BOG_AGENTS_MODEL_READ_TIMEOUT"
"""Read-timeout (seconds) for model HTTP requests."""

NO_UPDATE_CHECK = "BOG_AGENTS_NO_UPDATE_CHECK"
"""Disable the automatic update check at startup when truthy."""

OFFLINE = "BOG_AGENTS_OFFLINE"
"""Disable all managed-tool network downloads (e.g. ripgrep).

Parsed as a boolean: `1`/`true`/`yes`/`on` (case-insensitive) count as enabled.
Set on air-gapped machines so startup never touches the network.
"""

OPERATOR = "BOG_AGENTS_OPERATOR"
"""Env default for Operator prompt-routing mode (`1` on / `0` off)."""

OPERATOR_DISABLE = "BOG_AGENTS_OPERATOR_DISABLE"
"""Emergency kill switch for Operator mode; beats every other toggle."""

OTEL_ENDPOINT = "BOG_AGENTS_OTEL_ENDPOINT"
"""OTLP/HTTP collector base URL for GenAI-semconv spans (ROADMAP #74); unset = no export."""

POWERSHELL_TOOL = "BOG_AGENTS_POWERSHELL_TOOL"
"""Register the opt-in `powershell` tool (pwsh / Windows PowerShell via argv, never cmd.exe) when truthy (#61)."""

PREFLIGHT_THRESHOLD_USD = "BOG_AGENTS_PREFLIGHT_THRESHOLD_USD"
"""Projected spend above which /team run, /butcher and /best-of-n confirm before starting (#51)."""

PROJECT_ROOT = "BOG_AGENTS_PROJECT_ROOT"
"""Override the detected project root directory (used by project hooks)."""

RELEASE_TRAIN_HALO = "BOG_AGENTS_RELEASE_TRAIN_HALO"
"""Halo integration configuration for the release-train feature."""

RELEASE_TRAIN_JIRA = "BOG_AGENTS_RELEASE_TRAIN_JIRA"
"""Jira integration configuration for the release-train feature."""

REMOTE_READ_TIMEOUT = "BOG_AGENTS_REMOTE_READ_TIMEOUT"
"""Read-timeout (seconds) for the remote client HTTP requests."""

SHELL_ALLOW_LIST = "BOG_AGENTS_SHELL_ALLOW_LIST"
"""Comma-separated shell commands to allow (or `recommended`/`all`)."""

SHELL_AUTO_BACKGROUND_AFTER = "BOG_AGENTS_SHELL_AUTO_BACKGROUND_AFTER"
"""Seconds before a slow foreground shell command is moved to the background."""

SOUNDS = "BOG_AGENTS_SOUNDS"
"""Toggle CLI notification sounds."""

STALL_DUMP_SECS = "BOG_AGENTS_STALL_DUMP_SECS"
"""Seconds of apparent stall after which the server graph dumps diagnostics."""

STOP_GATE_CHECKS = "BOG_AGENTS_STOP_GATE_CHECKS"
"""Semicolon-separated commands that must pass before the agent may finish a turn."""

STREAM_CHUNK_TIMEOUT_SECONDS = "BOG_AGENTS_STREAM_CHUNK_TIMEOUT_SECONDS"
"""Per-chunk timeout (seconds) for non-interactive streaming output."""

THINKING = "BOG_AGENTS_THINKING"
"""Toggle extended thinking / reasoning for the model."""

THINKING_BUDGET = "BOG_AGENTS_THINKING_BUDGET"
"""Token budget for extended thinking / reasoning."""

TOOL_TIMEOUT = "BOG_AGENTS_TOOL_TIMEOUT"
"""Default per-tool execution timeout (seconds)."""

TRACEFILE_KEY = "BOG_AGENTS_TRACEFILE_KEY"
"""Signing key for the TraceFile header/audit trail."""

TRUST_PROJECT_HOOKS = "BOG_AGENTS_TRUST_PROJECT_HOOKS"
"""Pre-trust project-level hooks (skip the approval prompt) when truthy."""

TURN_TIMEOUT_SECONDS = "BOG_AGENTS_TURN_TIMEOUT_SECONDS"
"""Wall-clock timeout (seconds) for a single agent turn."""

VIM_MODE = "BOG_AGENTS_VIM_MODE"
"""Enable vim-style modal editing in the chat input.

Parsed as a boolean: `1`/`true`/`yes`/`on` (case-insensitive) count as enabled.
An explicit value wins over the `[ui].vim_mode` config-file entry.
"""

WEB_ALLOWED_DOMAINS = "BOG_AGENTS_WEB_ALLOWED_DOMAINS"
"""Comma-separated domains `fetch_url` / `http_request` may reach (suffix-matched); empty = any public host."""

WEB_BLOCKED_DOMAINS = "BOG_AGENTS_WEB_BLOCKED_DOMAINS"
# ROADMAP #73: register author_workflow / list_workflows even before the first workflow exists
WORKFLOW_TOOLS = "BOG_AGENTS_WORKFLOW_TOOLS"
"""Comma-separated domains the web tools must never reach (wins over the allow-list)."""

# ---------------------------------------------------------------------------
# Home-directory resolution.
# ---------------------------------------------------------------------------


def bog_agents_home() -> Path:
    """Resolve the bog-agents home directory, honoring `BOG_AGENTS_HOME`.

    The single source of truth for the base directory that holds user-level
    config (`config.toml`, `.mcp.json`, `.env`), the secret vault, MCP OAuth
    tokens, feature state, and the sessions database. When the
    `BOG_AGENTS_HOME` environment variable is set to a non-blank value it is
    used (with `~` expanded); otherwise the default `~/.bog-agents` applies.

    The variable is read on every call so call-time consumers honor a change
    within the process; module-level path constants derived from this helper
    are captured at import time, which matches the manifest's documented
    "read at startup" semantics (`paths.home`).

    Returns:
        The resolved bog-agents home directory (not created here).
    """
    raw = os.environ.get(HOME, "")
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return Path.home() / ".bog-agents"


# ---------------------------------------------------------------------------
# Shared boolean parsing helpers.
# ---------------------------------------------------------------------------

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSY_VALUES = frozenset({"0", "false", "no", "off", ""})


def classify_env_bool(raw: str) -> bool | None:
    """Classify a raw env-var string as truthy, falsy, or unrecognized.

    The single source of truth for which strings count as boolean on/off
    values; `is_env_truthy` and `env_bool` both build on it so they agree on
    what "recognizably boolean" means.

    Args:
        raw: The raw (unstripped) environment-variable value.

    Returns:
        `True` for `1`/`true`/`yes`/`on`, `False` for `0`/`false`/`no`/`off`/
        empty string (case-insensitive, surrounding whitespace ignored), or
        `None` when the value is neither.
    """
    lowered = raw.strip().lower()
    if lowered in _TRUTHY_VALUES:
        return True
    if lowered in _FALSY_VALUES:
        return False
    return None


def is_env_truthy(name: str, *, default: bool = False) -> bool:
    """Return whether env var *name* is set to a recognizably truthy value.

    Unlike `bool(os.environ.get(name))`, this does not treat `"0"` or
    `"false"` as enabled. Use this for on/off flags where the user would
    reasonably expect `VAR=0` to mean "disabled".

    Args:
        name: Environment variable name (typically a `BOG_AGENTS_*` constant
            from this module).
        default: Value returned when the variable is unset OR set to a value
            that is neither recognizably truthy nor falsy.

    Returns:
        `True` for `1`/`true`/`yes`/`on` (case-insensitive), `False` for
        `0`/`false`/`no`/`off`/empty string, or *default* otherwise.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    classified = classify_env_bool(raw)
    return default if classified is None else classified


def env_bool(name: str, default: bool = False) -> bool:
    """Positional-default convenience wrapper around `is_env_truthy`.

    Behaviourally identical to `is_env_truthy(name, default=default)`; provided
    because many existing call sites read a boolean with a positional default.

    Args:
        name: Environment variable name (typically a `BOG_AGENTS_*` constant
            from this module).
        default: Value returned when the variable is unset OR set to an
            unrecognized (non-boolean) token.

    Returns:
        `True` for `1`/`true`/`yes`/`on`, `False` for `0`/`false`/`no`/`off`/
        empty string, or *default* when unset/unrecognized.
    """
    return is_env_truthy(name, default=default)
