"""Dreamscape configuration loader.

A single TOML file at ``~/.bog-agents/dreamscape.toml`` holds every
dreamscape knob. The file is optional — when absent we synthesise the
default config in memory and **nothing is enabled**.

Resolution order (highest priority first):

1. ``BOG_AGENTS_DREAMSCAPE_DISABLE=1`` — emergency kill. When set,
   every dreamscape subsystem is forced off regardless of file
   contents or other env vars. Designed for "something is misbehaving
   in CI, please make it stop now."
2. ``BOG_AGENTS_DREAMSCAPE=1`` — master enable, overrides
   ``enabled = false`` in the TOML.
3. Per-feature env vars: ``BOG_AGENTS_DREAMSCAPE_LIFECYCLE``,
   ``BOG_AGENTS_DREAMSCAPE_LAWS``, ``BOG_AGENTS_DREAMSCAPE_DREAMS``,
   ``BOG_AGENTS_DREAMSCAPE_IMAGINATION``,
   ``BOG_AGENTS_DREAMSCAPE_SHARED_MEMORY``.
   Accept ``1/0/true/false/yes/no/on/off``.
4. The TOML file's contents.
5. Built-in defaults — every feature OFF.

The function ``is_emergency_disabled()`` is broken out so tight
inner-loop checks (e.g. inside an ``awrap_model_call`` hook) can avoid
re-reading the whole config every call.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DREAMSCAPE_FILENAME = "dreamscape.toml"
# The "active" file is written by ``agent.py`` at agent-build time and
# reflects the resolved runtime config (file + env-var overlays). The
# dashboard reads this when present so ``/agent-state`` and
# ``/dreamscape status`` show the configuration the running agent is
# *actually* using — not the on-disk file's contents which may differ
# from the env-var-driven runtime resolution.
_DREAMSCAPE_ACTIVE_FILENAME = "dreamscape-active.toml"
_EMERGENCY_DISABLE_ENV = "BOG_AGENTS_DREAMSCAPE_DISABLE"
_MASTER_ENABLE_ENV = "BOG_AGENTS_DREAMSCAPE"


def _env_bool(name: str) -> bool | None:
    """Read a boolean env var. Returns None when unset/blank."""
    raw = os.environ.get(name, "")
    if not raw:
        return None
    norm = raw.strip().lower()
    if norm in {"1", "true", "yes", "on", "y"}:
        return True
    if norm in {"0", "false", "no", "off", "n"}:
        return False
    logger.warning("ignoring unparseable boolean env var %s=%r", name, raw)
    return None


def is_emergency_disabled() -> bool:
    """Check the emergency-disable env var. Cheap; safe to call per-request."""
    return _env_bool(_EMERGENCY_DISABLE_ENV) is True


# ---------------------------------------------------------------------------
# Per-feature configs
# ---------------------------------------------------------------------------


@dataclass
class LifecycleConfig:
    """Knobs for :class:`LifecycleMiddleware`."""

    enabled: bool = False
    """Off by default. When False the middleware loads as a passthrough."""

    dormancy_after_seconds: int = 1800
    """How long without activity before a session transitions to DORMANT."""

    dreaming_after_dormant_seconds: int = 600
    """Additional silence after DORMANT before DREAMING is triggered."""

    persist_state_to_disk: bool = True
    """Write per-agent lifecycle.json snapshots so the dashboard can read it."""


@dataclass
class LawsConfig:
    """Knobs for :class:`LawsMiddleware`."""

    enabled: bool = False
    laws_path: str = "~/.bog-agents/laws.md"
    """Hard rules. Violations are rejected (or logged when ``reject_on_violation=False``)."""

    constitution_path: str = "~/.bog-agents/constitution.md"
    """Soft preferences. Always log-only."""

    reject_on_violation: bool = False
    """When True, Laws violations cause the agent to refuse the request.
    Default False — start in log-only mode so users can audit before
    enabling enforcement."""

    log_constitution_violations: bool = True
    """When True, Constitution violations are recorded in the lifecycle log."""


@dataclass
class SharedMemoryConfig:
    """Knobs for the cross-agent memory tier."""

    enabled: bool = False
    backend: str = "sqlite"
    """One of ``sqlite`` (default), ``postgres``, ``redis``, ``dynamo``.

    Only ``sqlite`` is implemented in-tree; others fall back to a logged
    no-op until a backend module is provided."""

    url: str = ""
    """Backend URL. Empty means use the default SQLite location."""

    sqlite_path: str = "~/.bog-agents/shared-memory.db"
    """Override for the SQLite backend path."""

    redact_secret_patterns: list[str] = field(
        default_factory=lambda: [
            r"sk-[A-Za-z0-9]{16,}",  # OpenAI / Anthropic key shapes
            r"ghp_[A-Za-z0-9]{20,}",  # GitHub PAT
            r"AKIA[0-9A-Z]{16}",  # AWS access key
        ]
    )
    """Regexes for content that's automatically redacted before write."""

    max_entry_chars: int = 8_000
    """Anything larger is truncated with a notice."""


@dataclass
class DreamsConfig:
    """Knobs for the dream subsystem (extends the existing ``/dream``)."""

    auto_on_dormancy: bool = False
    """When True and the lifecycle middleware is enabled, a dream is
    generated automatically whenever the agent transitions into
    DREAMING. When False, dreams only happen via the manual
    ``/dream`` command — the v1 behavior."""

    model: str = ""
    """Override the model used for dreams. Empty = use the active
    model. Recommended override: a cheap model (Haiku, Gemini Flash)
    since dreams are background work."""

    max_seeds_per_dream: int = 3
    """How many random seed snippets are mixed into a single dream
    prompt. Seeds come from ``bog_agents_cli/dreamscape/seeds/``."""

    seed_categories: list[str] = field(
        default_factory=lambda: [
            "nature",
            "space",
            "history",
            "myth",
            "computing-history",
        ]
    )
    """Subset of seed categories to draw from. Empty list = all categories."""

    persist_per_agent_log: bool = True
    """Write dreams to ``~/.bog-agents/agents/<id>/dreams/<ts>.md``."""

    imagination_trait_increment: float = 0.01
    """How much each completed dream bumps the agent's ``imagination``
    trait. Capped at 100.0 by the lifecycle store."""


@dataclass
class ImaginationConfig:
    """Knobs for the last-ditch imagination injection middleware."""

    enabled: bool = False
    trigger_after_failures: int = 3
    """How many consecutive tool-call failures before a dream snippet is
    injected. Lower = more aggressive intervention."""

    min_imagination_trait: float = 1.0
    """Don't inject anything if the agent's accumulated imagination
    trait is below this threshold. Forces the agent to "earn" the
    intervention by accumulating dreams first."""

    max_snippets_per_injection: int = 3
    """How many dream excerpts go into a single injection."""

    inject_on_explicit_help: bool = True
    """When True, ``/help-dream`` triggers an injection regardless of
    failure streak."""

    auto_disable_below_success_rate: float = 0.4
    """If the rolling success rate after injection drops below this
    threshold, the middleware auto-disables itself until the next
    dream lands. Set to 0.0 to disable the kill-switch."""


@dataclass
class DashboardConfig:
    """Knobs for the read-only ``/agent-state`` + ``/repo`` views.

    Dashboard is enabled by default because it's pure read-only —
    cannot affect agent behavior, cannot send tokens, cannot mutate
    state. Useful even when the rest of dreamscape is off (it'll just
    show "all features disabled" plus the existing repo summary).
    """

    enabled: bool = True
    verbose: bool = False
    """When True, /agent-state shows full dream excerpts; otherwise just titles."""


@dataclass
class DreamscapeConfig:
    """Top-level dreamscape configuration."""

    master_enabled: bool = False
    """The kill switch everything else checks. Off by default."""

    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
    laws: LawsConfig = field(default_factory=LawsConfig)
    shared_memory: SharedMemoryConfig = field(default_factory=SharedMemoryConfig)
    dreams: DreamsConfig = field(default_factory=DreamsConfig)
    imagination: ImaginationConfig = field(default_factory=ImaginationConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)

    # --- convenience properties ---------------------------------------

    @property
    def any_active(self) -> bool:
        """True when at least one runtime-effecting subsystem is on.

        Used by the wiring layer in ``agent.py`` to short-circuit
        middleware attachment entirely when nothing is enabled. The
        dashboard alone doesn't trip this — it's read-only.
        """
        if not self.master_enabled:
            return False
        return (
            self.lifecycle.enabled
            or self.laws.enabled
            or self.shared_memory.enabled
            or self.imagination.enabled
            or self.dreams.auto_on_dormancy
        )


DreamscapeFeatureConfig = (
    LifecycleConfig
    | LawsConfig
    | SharedMemoryConfig
    | DreamsConfig
    | ImaginationConfig
    | DashboardConfig
)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def dreamscape_config_path() -> Path:
    """Canonical path to the dreamscape TOML the user edits directly."""
    return Path.home() / ".bog-agents" / _DREAMSCAPE_FILENAME


def dreamscape_active_path() -> Path:
    """Path to the runtime-active TOML written by ``agent.py`` at build time.

    The dashboard prefers this over the canonical file when present —
    it shows what the *running* agent is using, not what's on disk.
    Stale entries are tolerated (the on-disk file is the source of
    truth at next agent build); the dashboard simply notes when the
    active file is missing.
    """
    return Path.home() / ".bog-agents" / _DREAMSCAPE_ACTIVE_FILENAME


def write_active_runtime_config(cfg: DreamscapeConfig) -> Path | None:
    """Persist the resolved runtime config so the dashboard can read it.

    Best-effort — disk failure logs and returns ``None``. Called once
    per agent build from ``agent.py:_attach_dreamscape_middleware``.
    """
    try:
        return save_dreamscape_config(cfg, path=dreamscape_active_path())
    except OSError as exc:
        logger.debug("dreamscape: could not persist active config: %s", exc)
        return None


def load_active_runtime_config() -> DreamscapeConfig | None:
    """Return the runtime-active config if present and parseable.

    Returns ``None`` when the active file doesn't exist or can't be
    read. Callers should fall back to :func:`load_dreamscape_config`
    in that case.
    """
    target = dreamscape_active_path()
    if not target.exists():
        return None
    try:
        return load_dreamscape_config(path=target, use_cache=False)
    except Exception:
        logger.debug("dreamscape: failed to read active config", exc_info=True)
        return None


# Module-level cache. Cleared by ``clear_cache`` (used in tests).
_CACHED_CONFIG: DreamscapeConfig | None = None
_CACHED_FROM_PATH: Path | None = None


def clear_cache() -> None:
    """Drop the cached config so the next ``load_dreamscape_config`` re-reads."""
    global _CACHED_CONFIG, _CACHED_FROM_PATH  # noqa: PLW0603
    _CACHED_CONFIG = None
    _CACHED_FROM_PATH = None


def load_dreamscape_config(
    path: Path | None = None, *, use_cache: bool = True
) -> DreamscapeConfig:
    """Load the dreamscape config with env-var overrides applied.

    The result is cached per-path so that a hot path (a middleware
    hook called once per model call) doesn't re-read the TOML every
    time. Pass ``use_cache=False`` to force a re-read.

    Args:
        path: Override path (tests). Defaults to
            ``~/.bog-agents/dreamscape.toml``.
        use_cache: When True, return the cached result if the path
            matches a previous call.

    Returns:
        A fully-populated :class:`DreamscapeConfig`. NEVER raises —
        unreadable / malformed config files yield the default
        (everything-off) config with a logged warning.
    """
    global _CACHED_CONFIG, _CACHED_FROM_PATH  # noqa: PLW0603

    target = path or dreamscape_config_path()
    if use_cache and _CACHED_CONFIG is not None and target == _CACHED_FROM_PATH:
        return _CACHED_CONFIG

    cfg = DreamscapeConfig()

    # File-driven overrides come first so env vars beat them.
    if target.exists():
        try:
            data = tomllib.loads(target.read_text(encoding="utf-8"))
            _apply_toml(cfg, data)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logger.warning(
                "dreamscape config at %s could not be loaded (%s); "
                "falling back to defaults (everything OFF)",
                target,
                exc,
            )

    _apply_env_overrides(cfg)

    if use_cache:
        _CACHED_CONFIG = cfg
        _CACHED_FROM_PATH = target
    return cfg


def _apply_toml(cfg: DreamscapeConfig, data: dict[str, Any]) -> None:
    """Mutate ``cfg`` in place with values from the parsed TOML."""
    master = data.get("enabled")
    if isinstance(master, bool):
        cfg.master_enabled = master

    # Each section maps to one of the nested dataclasses. Missing
    # sections are fine; unknown keys are silently ignored.
    sections: dict[str, Any] = {
        "lifecycle": cfg.lifecycle,
        "laws": cfg.laws,
        "shared_memory": cfg.shared_memory,
        "dreams": cfg.dreams,
        "imagination": cfg.imagination,
        "dashboard": cfg.dashboard,
    }
    for section_name, section_obj in sections.items():
        raw_section = data.get(section_name)
        if isinstance(raw_section, dict):
            _apply_dict_to_dataclass(section_obj, raw_section)


def _apply_dict_to_dataclass(obj: Any, raw: dict[str, Any]) -> None:
    """Copy values from ``raw`` into ``obj`` for keys that match dataclass fields.

    Unknown keys are ignored (forward-compatible). Type mismatches
    are logged and skipped (the default value survives).
    """
    if not is_dataclass(obj):
        return
    valid_names = {f.name for f in fields(obj)}
    for key, value in raw.items():
        if key not in valid_names:
            continue
        # Get the existing default to learn the expected type.
        current = getattr(obj, key)
        if not _types_compatible(current, value):
            logger.warning(
                "dreamscape: ignoring %s.%s — expected %s, got %s",
                type(obj).__name__,
                key,
                type(current).__name__,
                type(value).__name__,
            )
            continue
        setattr(obj, key, value)


def _types_compatible(existing: Any, incoming: Any) -> bool:
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


def _apply_env_overrides(cfg: DreamscapeConfig) -> None:
    """Layer env vars on top of file-driven config."""
    if is_emergency_disabled():
        cfg.master_enabled = False
        cfg.lifecycle.enabled = False
        cfg.laws.enabled = False
        cfg.shared_memory.enabled = False
        cfg.dreams.auto_on_dormancy = False
        cfg.imagination.enabled = False
        return

    master = _env_bool(_MASTER_ENABLE_ENV)
    if master is not None:
        cfg.master_enabled = master

    overrides: tuple[tuple[str, Any, str], ...] = (
        ("BOG_AGENTS_DREAMSCAPE_LIFECYCLE", cfg.lifecycle, "enabled"),
        ("BOG_AGENTS_DREAMSCAPE_LAWS", cfg.laws, "enabled"),
        ("BOG_AGENTS_DREAMSCAPE_SHARED_MEMORY", cfg.shared_memory, "enabled"),
        ("BOG_AGENTS_DREAMSCAPE_DREAMS_AUTO", cfg.dreams, "auto_on_dormancy"),
        ("BOG_AGENTS_DREAMSCAPE_IMAGINATION", cfg.imagination, "enabled"),
    )
    for env, target, attr in overrides:
        flag = _env_bool(env)
        if flag is not None:
            setattr(target, attr, flag)


def save_dreamscape_config(cfg: DreamscapeConfig, *, path: Path | None = None) -> Path:
    """Write the config back to TOML. Used by ``/dreamscape init`` wizard."""
    import tomli_w

    target = path or dreamscape_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _dataclass_to_dict(cfg)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("wb") as fh:
        tomli_w.dump(payload, fh)
    tmp.replace(target)
    clear_cache()
    return target


def _dataclass_to_dict(cfg: DreamscapeConfig) -> dict[str, Any]:
    """Serialize the config for TOML. The top-level ``enabled`` flag is
    promoted out of the master_enabled attribute so the on-disk shape
    reads naturally.
    """
    out: dict[str, Any] = {"enabled": cfg.master_enabled}
    for section_name in (
        "lifecycle",
        "laws",
        "shared_memory",
        "dreams",
        "imagination",
        "dashboard",
    ):
        section = getattr(cfg, section_name)
        out[section_name] = {f.name: getattr(section, f.name) for f in fields(section)}
    return out
