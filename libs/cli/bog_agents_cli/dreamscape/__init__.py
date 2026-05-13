"""Dreamscape — agent lifecycle, dreams, imagination, and two-tier laws.

The dreamscape is opt-in by design. With the default configuration
(no ``~/.bog-agents/dreamscape.toml`` file, or one with ``enabled = false``),
EVERY middleware in this package is a no-op and the agent behaves
exactly as it did before the dreamscape work shipped.

When the master switch is on, individual subsystems remain opt-in via
per-feature toggles:

* ``[lifecycle]`` — state tracking (Awake / Idle / Dormant / Dreaming / Imagining)
* ``[laws]`` — two-tier rules: ``.bog-agents/laws.md`` (hard, reject)
  and ``.bog-agents/constitution.md`` (soft, log-only by default)
* ``[shared_memory]`` — cross-agent memory tier
* ``[dreams]`` — dormancy-triggered dream generation (on-demand /dream
  still works regardless of this setting)
* ``[imagination]`` — last-ditch dream-snippet injection on N stuck
  tool calls

Anything that goes wrong inside a dreamscape middleware MUST fall
through to the underlying agent behavior. We never raise a dreamscape
error into the user's prompt path. See ``_safe`` in each middleware
for the try/except pattern.

The single source of truth for whether a feature is on:

    from bog_agents_cli.dreamscape.config import load_dreamscape_config
    cfg = load_dreamscape_config()
    if cfg.lifecycle.enabled and cfg.master_enabled:
        ...

The conjunction with ``master_enabled`` is intentional and present at
every check site. Flipping the master switch off should kill every
dreamscape side effect in one move.
"""

from __future__ import annotations

from bog_agents_cli.dreamscape.config import (
    DreamscapeConfig,
    DreamscapeFeatureConfig,
    dreamscape_active_path,
    dreamscape_config_path,
    is_emergency_disabled,
    load_active_runtime_config,
    load_dreamscape_config,
    save_dreamscape_config,
    write_active_runtime_config,
)

__all__ = [
    "DreamscapeConfig",
    "DreamscapeFeatureConfig",
    "dreamscape_active_path",
    "dreamscape_config_path",
    "is_emergency_disabled",
    "load_active_runtime_config",
    "load_dreamscape_config",
    "save_dreamscape_config",
    "write_active_runtime_config",
]
