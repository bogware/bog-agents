"""Bootstrap for built-in and third-party profile plugins.

Built-in provider and harness profiles are registered via explicit
module imports — not entry points — so a malformed or missing
`dist-info` in the environment cannot silently disable the SDK's own
defaults. Third parties plug in via `importlib.metadata` entry points
under two native groups:

- `bog_agents.provider_profiles` — plugins that call
    `register_provider_profile(...)` to declare provider- or model-keyed
    `ProviderProfile` entries.
- `bog_agents.harness_profiles` — plugins that call
    `register_harness_profile(...)` to declare provider- or model-keyed
    `HarnessProfile` entries.

For interoperability with the upstream deepagents ecosystem, the two
legacy deepagents entry-point groups (`deepagents.provider_profiles` and
`deepagents.harness_profiles`) are enumerated as well, so a third-party
deepagents profile plugin loads unchanged on bog-agents. Native (bog)
groups take precedence: a plugin listed under both a native and a legacy
group is loaded once, from the native group.

Each entry point resolves to a zero-arg callable whose sole job is to
perform the registrations. Built-ins load first, so third-party plugins
registering under the same key layer on top via the additive merge
semantics of `register_*_profile`.

This bog-agents port ships built-in provider and harness profiles for
several frontier model specs (Anthropic Claude Opus/Sonnet/Haiku, OpenAI
Codex, NVIDIA Nemotron) plus provider-construction profiles for OpenAI,
NVIDIA, and OpenRouter. The public bootstrap surface
(`_ensure_builtin_profiles_loaded`, `_BOOTSTRAP_HARNESS_KEYS`) is
preserved so `harness_profiles` and `provider_profiles` import unchanged.
"""

from __future__ import annotations

import logging
import threading
import warnings
from importlib.metadata import EntryPoint, entry_points

from bog_agents.profiles.harness.harness_profiles import _HARNESS_PROFILES
from bog_agents.profiles.provider.provider_profiles import _PROVIDER_PROFILES

logger = logging.getLogger(__name__)


def _format_plugin_label(ep: EntryPoint) -> str:
    """Return a human-readable identifier for a plugin entry point.

    Includes the source distribution name when available so logs can point
    at the misbehaving package, not just the entry-point name (which can
    collide across distributions).
    """
    dist = getattr(ep, "dist", None)
    dist_name = getattr(dist, "name", None) if dist is not None else None
    if isinstance(dist_name, str) and dist_name:
        return f"{ep.name!r} (dist={dist_name!r})"
    return repr(ep.name)


_PROVIDER_PROFILE_GROUP = "bog_agents.provider_profiles"
"""Entry-point group name for third-party `ProviderProfile` plugins."""

_HARNESS_PROFILE_GROUP = "bog_agents.harness_profiles"
"""Entry-point group name for third-party `HarnessProfile` plugins."""

_LEGACY_PROVIDER_PROFILE_GROUP = "deepagents.provider_profiles"
"""Upstream deepagents entry-point group for `ProviderProfile` plugins.

Enumerated in addition to `_PROVIDER_PROFILE_GROUP` so third-party
deepagents plugins load on bog-agents. The native group wins on collision.
"""

_LEGACY_HARNESS_PROFILE_GROUP = "deepagents.harness_profiles"
"""Upstream deepagents entry-point group for `HarnessProfile` plugins.

Enumerated in addition to `_HARNESS_PROFILE_GROUP` so third-party
deepagents plugins load on bog-agents. The native group wins on collision.
"""

_BOOTSTRAP_HARNESS_KEYS: frozenset[str] = frozenset()
"""Snapshot of harness-profile keys registered during bootstrap.

Populated once by `_ensure_builtin_profiles_loaded`. Captures every
harness key in the registry immediately after the built-in and
entry-point phases complete — so both bog-agents' own defaults and any
third-party harness plugins the user has installed are treated uniformly
as "bootstrap-provided." `_has_any_harness_profile` subtracts this set
from the live registry to distinguish those defaults from profiles the
user registers explicitly after import.
"""

_loaded: bool = False
"""Guards `_ensure_builtin_profiles_loaded` against re-running.

Registration callables are not guaranteed idempotent — repeat
invocations would chain `pre_init` hooks or re-merge kwargs with
themselves. The flag ensures the bootstrap runs exactly once per
interpreter, even if the function is called directly from tests or a
reload scenario.
"""

_BOOTSTRAP_CONDITION = threading.Condition()
"""Coordinates first-time lazy bootstrap across threads.

One thread performs the bootstrap while concurrent threads wait for it
to finish. The condition is also used to permit same-thread re-entry:
plugin registration callables invoked *during* bootstrap often call the
public `register_*_profile` helpers, which must short-circuit rather
than deadlock or recurse.
"""

_loading_thread_id: int | None = None
"""Thread currently performing `_ensure_builtin_profiles_loaded`, if any.

Used to distinguish same-thread re-entry (short-circuit) from
cross-thread first access (wait for bootstrap completion).
"""


def _register_builtin_profiles() -> None:
    """Register bog-agents' own built-in provider and harness profiles.

    Imports are performed locally so importing this bootstrap module stays
    cheap: the (sometimes heavy) profile modules are only pulled in when
    the bootstrap actually runs. Each module exposes a module-level
    `register()` that calls the bootstrap-internal `_register_*_profile_impl`
    registrar — safe to invoke here because the caller already holds the
    bootstrap coordination and the impl helpers don't re-enter the lazy
    bootstrap.

    Built-in harness profiles cover Anthropic Claude Opus 4.7 / Sonnet 4.6 /
    Haiku 4.5, the OpenAI Codex family, and NVIDIA Nemotron 3 Ultra.
    Built-in provider profiles cover OpenAI (Responses API by default),
    NVIDIA, and OpenRouter (version guard + app attribution).
    """
    from bog_agents.profiles.harness import (
        _anthropic_haiku_4_5,
        _anthropic_opus_4_7,
        _anthropic_sonnet_4_6,
        _lean,
        _nvidia_nemotron_3_ultra,
        _openai_codex,
    )
    from bog_agents.profiles.provider import _nvidia, _openai, _openrouter

    # Harness profiles first, then provider profiles. Order is not
    # significant: harness and provider registries are independent.
    _anthropic_opus_4_7.register()
    _anthropic_sonnet_4_6.register()
    _anthropic_haiku_4_5.register()
    _openai_codex.register()
    _nvidia_nemotron_3_ultra.register()
    _lean.register()
    _openai.register()
    _nvidia.register()
    _openrouter.register()


def _ensure_builtin_profiles_loaded() -> None:
    """Register built-in profiles and discover third-party plugins.

    Runs two phases, both idempotent:

    1. Register bog-agents' own built-in provider and harness profiles via
        explicit module imports (see `_register_builtin_profiles`).
    2. Iterate `importlib.metadata` entry points in the native
        `bog_agents.provider_profiles` / `bog_agents.harness_profiles`
        groups and the legacy `deepagents.provider_profiles` /
        `deepagents.harness_profiles` groups. Native groups win on
        collision. Third-party failures are logged at `ERROR`/`WARNING`
        and skipped so one misbehaving distribution cannot prevent
        `bog_agents.profiles` from importing.

    Built-ins run first, so third-party plugins registering under the
    same key layer on top via additive merge semantics in
    `register_*_profile`.

    The whole two-phase body runs inside a try/rollback: if built-in
    registration or plugin discovery raises, the pre-bootstrap registry
    state is restored in place (so modules holding registry references
    keep seeing the same dict objects) before the exception propagates.

    After both phases complete, snapshots the harness registry so
    downstream callers can distinguish bootstrap-registered profiles
    from profiles registered later via user code.

    The function is invoked lazily from `register_*_profile` and
    `get_*_profile` entry points; importing `bog_agents.profiles` itself
    does not trigger bootstrap. Same-thread re-entrant calls that occur
    *during* bootstrap (for example, plugin registration helpers calling
    the public `register_*_profile` APIs) short-circuit, while other
    threads block until bootstrap completes so they never observe a
    partially populated registry.
    """
    global _loaded, _BOOTSTRAP_HARNESS_KEYS, _loading_thread_id  # noqa: PLW0603
    thread_id = threading.get_ident()
    with _BOOTSTRAP_CONDITION:
        if _loaded:
            return
        if _loading_thread_id == thread_id:
            return
        while _loading_thread_id is not None:
            _BOOTSTRAP_CONDITION.wait()
            if _loaded:
                return
        _loading_thread_id = thread_id
    saved_provider_profiles = dict(_PROVIDER_PROFILES)
    saved_harness_profiles = dict(_HARNESS_PROFILES)
    saved_bootstrap_harness_keys = _BOOTSTRAP_HARNESS_KEYS
    try:
        # Phase 1: first-party built-in registrations.
        _register_builtin_profiles()
        # Phase 2: third-party entry-point plugins (native + legacy groups).
        _invoke_profile_plugins(_PROVIDER_PROFILE_GROUP, _LEGACY_PROVIDER_PROFILE_GROUP)
        _invoke_profile_plugins(_HARNESS_PROFILE_GROUP, _LEGACY_HARNESS_PROFILE_GROUP)
        bootstrap_harness_keys = frozenset(_HARNESS_PROFILES)
    except Exception:
        logger.exception("Built-in profile bootstrap failed; restoring pre-bootstrap registry state.")
        # Restore in place so modules holding registry references keep seeing
        # the same dict objects after rollback.
        _PROVIDER_PROFILES.clear()
        _PROVIDER_PROFILES.update(saved_provider_profiles)
        _HARNESS_PROFILES.clear()
        _HARNESS_PROFILES.update(saved_harness_profiles)
        with _BOOTSTRAP_CONDITION:
            _BOOTSTRAP_HARNESS_KEYS = saved_bootstrap_harness_keys
            _loading_thread_id = None
            _BOOTSTRAP_CONDITION.notify_all()
        raise
    with _BOOTSTRAP_CONDITION:
        _BOOTSTRAP_HARNESS_KEYS = bootstrap_harness_keys
        _loaded = True
        _loading_thread_id = None
        _BOOTSTRAP_CONDITION.notify_all()


def _load_group_entry_points(group: str) -> list[EntryPoint]:
    """Return the entry points declared under `group`, isolating enumeration failures.

    A failure of `entry_points(group=...)` itself (e.g. malformed
    `dist-info` metadata) is environment-level breakage, not attributable
    to a specific plugin. It is logged at `WARNING`, surfaced as a warning,
    and treated as "no plugins in this group" so bootstrap continues.

    Args:
        group: Entry-point group name to enumerate.

    Returns:
        The list of entry points in `group`, or an empty list if
            enumeration failed.
    """
    try:
        return list(entry_points(group=group))
    except Exception as exc:  # noqa: BLE001
        msg = f"Failed to enumerate {group} entry points; no third-party plugins in this group will load: {type(exc).__name__}: {exc}"
        logger.warning(msg, exc_info=True)
        warnings.warn(msg, stacklevel=2)
        return []


def _invoke_entry_point(ep: EntryPoint, group: str) -> None:
    """Load and invoke a single profile-plugin entry point, isolating failures.

    Failure handling differentiates plugin-level bug classes:

    1. `ep.load()` raises (missing dependency, import-time error). Logged
        at `ERROR` with the source distribution name — a structural bug
        in the plugin that the plugin author needs to fix.
    2. The entry-point target resolves to something that is not callable.
        Logged at `ERROR` (declaring a non-callable as a registration hook
        is a plugin bug).
    3. The registration callable raises when invoked. Logged at `ERROR` —
        the plugin attempted to register but produced a `ValueError` /
        `TypeError` / etc. The plugin's registrations are silently absent
        if this is suppressed, so the elevated level helps users notice.

    Args:
        ep: The entry point to load and invoke.
        group: Owning entry-point group name, used for log/warning messages.
    """
    plugin_label = _format_plugin_label(ep)
    try:
        register = ep.load()
    except Exception as exc:
        msg = f"Skipping {group} plugin {plugin_label}: failed to load entry point {ep.value!r}: {type(exc).__name__}: {exc}"
        logger.exception(msg)
        warnings.warn(msg, stacklevel=2)
        return
    if not callable(register):
        msg = f"Skipping {group} plugin {plugin_label}: entry point {ep.value!r} did not resolve to a callable."
        logger.error(msg)
        warnings.warn(msg, stacklevel=2)
        return
    try:
        register()
    except Exception as exc:
        msg = f"Skipping {group} plugin {plugin_label}: registration callable {ep.value!r} raised: {type(exc).__name__}: {exc}"
        logger.exception(msg)
        warnings.warn(msg, stacklevel=2)


def _invoke_profile_plugins(native_group: str, legacy_group: str) -> None:
    """Invoke every registration callable in `native_group`, then `legacy_group`.

    The native (bog-agents) group is iterated first, then the legacy
    deepagents group. Entry points are de-duplicated by `(name, value)`
    identity: a plugin listed under both a native and a legacy group is
    invoked once, from the native group, so migrating a plugin's group
    declaration (or shipping it under both for cross-ecosystem support)
    never double-registers it. Because `register_*_profile` merges
    additively, running the same plugin twice would chain its `pre_init`
    hooks or re-merge its kwargs with themselves — de-duplication avoids
    that.

    An `INFO` breadcrumb is logged when a plugin is loaded from the legacy
    group, so users can trace which registrations came in through the
    deepagents-compatibility path and migrate them.

    Failure isolation mirrors `_load_group_entry_points` (group-level) and
    `_invoke_entry_point` (plugin-level): a broken group or plugin is
    skipped rather than aborting bootstrap.

    Plugins are iterated in whatever order
    `importlib.metadata.entry_points` returns — callers MUST NOT rely on
    a specific ordering when two plugins register under the same key.
    Registration semantics are additive (`register_*_profile` merges on
    top), so later entries layer on earlier ones.

    Args:
        native_group: Native bog-agents entry-point group name (e.g.
            `bog_agents.provider_profiles`). Takes precedence on collision.
        legacy_group: Legacy deepagents entry-point group name (e.g.
            `deepagents.provider_profiles`). Loaded for interoperability.
    """
    seen: set[tuple[str, str]] = set()
    for ep in _load_group_entry_points(native_group):
        seen.add((ep.name, ep.value))
        _invoke_entry_point(ep, native_group)
    for ep in _load_group_entry_points(legacy_group):
        identity = (ep.name, ep.value)
        if identity in seen:
            # Already loaded from the native group; native wins on collision.
            continue
        seen.add(identity)
        logger.info(
            "Loading %s plugin %s from legacy deepagents entry-point group; consider migrating it to %r.",
            legacy_group,
            _format_plugin_label(ep),
            native_group,
        )
        _invoke_entry_point(ep, legacy_group)
