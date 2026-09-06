"""Managed governance layer (ROADMAP #50): an org policy above every other setting.

A platform team publishes one signed JSON document — at a URL or a path in
the repo — and every bog session on the machine fetches it at start, verifies
it against the pinned Ed25519 public key (the same key format the TraceFile
signer uses), caches the last good copy and enforces it at the few places
policy can bite: MCP discovery (`allowed_mcp_servers`), skill loading
(`skill_allowlist`), plugin installs (`required` / `optional` / `forbidden`),
`create_model` (`provider_lock`: gateway-only `base_url`), `/model`
(`model_policy`) and the `zero_retention` flag that turns off the sidechain /
memory writers. Nothing here prompts: a policy either verifies and applies,
or it is rejected loudly and the session runs without it. Every check is a
pure function of a `ManagedPolicy` so the enforcement points stay one-liners.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CACHE_NAME = "managed-policy.json"
_FETCH_TIMEOUT = 5.0
_MAX_BYTES = 256_000
_CACHE: dict[str, Any] = {}


@dataclass(frozen=True)
class ManagedPolicy:
    """The org's policy, verified."""

    source: str = ""
    org: str = ""
    version: str = ""
    fingerprint: str = ""
    signed: bool = False
    allowed_mcp_servers: tuple[str, ...] | None = None
    skill_allowlist: tuple[str, ...] | None = None
    required_plugins: tuple[str, ...] = ()
    optional_plugins: tuple[str, ...] = ()
    forbidden_plugins: tuple[str, ...] = ()
    provider_lock: dict[str, str] = field(default_factory=dict)
    zero_retention: bool = False
    model_allow: tuple[str, ...] = ()
    model_deny: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    # -- pure checks -----------------------------------------------------------

    def mcp_server_allowed(self, name: str) -> bool:
        """Whether an MCP server may be connected (`None` allowlist = anything)."""
        return self.allowed_mcp_servers is None or _matches(
            name, self.allowed_mcp_servers
        )

    def filter_mcp_servers(
        self, servers: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """Keep the allowed servers; returns `(kept, removed names)`."""
        kept = {
            name: cfg for name, cfg in servers.items() if self.mcp_server_allowed(name)
        }
        return kept, [name for name in servers if name not in kept]

    def skill_allowed(self, name_or_path: str) -> bool:
        """Whether a skill (by directory name) may load (`None` allowlist = anything)."""
        if self.skill_allowlist is None:
            return True
        name = Path(str(name_or_path).rstrip("/\\")).name
        return _matches(name, self.skill_allowlist) or _matches(
            str(name_or_path), self.skill_allowlist
        )

    def plugin_verdict(self, name: str) -> str:
        """`forbidden` | `required` | `optional` | `unlisted`."""
        if _matches(name, self.forbidden_plugins):
            return "forbidden"
        if _matches(name, self.required_plugins):
            return "required"
        if _matches(name, self.optional_plugins):
            return "optional"
        return "unlisted"

    def locked_base_url(self, provider: str) -> str | None:
        """The gateway `base_url` this provider must use, or `None`."""
        return self.provider_lock.get(provider) or self.provider_lock.get("*")

    def model_switch_refusal(self, spec: str) -> str | None:
        """Why switching to `spec` is refused, or `None`."""
        if self.model_deny and _matches(spec, self.model_deny):
            return f"model {spec!r} is denied by the managed policy" + (
                f" ({self.org})" if self.org else ""
            )
        if self.model_allow and not _matches(spec, self.model_allow):
            return f"model {spec!r} is outside the managed allow-list ({', '.join(self.model_allow)})"
        return None

    def missing_required_plugins(self, installed: list[str]) -> list[str]:
        """Required plugins not present (soft-fail: reported, never blocking)."""
        return [
            p
            for p in self.required_plugins
            if not any(_matches(name, (p,)) for name in installed)
        ]

    def rows(self) -> list[str]:
        """Org-pinned rows for `/permissions` and `/doctor`."""
        head = (
            f"Managed policy: {self.org or 'org'}"
            + (f" v{self.version}" if self.version else "")
            + (" (signed)" if self.signed else " (UNSIGNED)")
        )
        rows = [head + f" — {self.source}"]
        if self.allowed_mcp_servers is not None:
            rows.append(
                f"  MCP servers: {', '.join(self.allowed_mcp_servers) or 'none'}"
            )
        if self.skill_allowlist is not None:
            rows.append(f"  skills: {', '.join(self.skill_allowlist) or 'none'}")
        if self.required_plugins or self.forbidden_plugins:
            rows.append(
                f"  plugins: required {', '.join(self.required_plugins) or '-'}; forbidden {', '.join(self.forbidden_plugins) or '-'}"
            )
        if self.provider_lock:
            rows.append(
                "  provider lock: "
                + ", ".join(f"{p} → {u}" for p, u in self.provider_lock.items())
            )
        if self.model_allow or self.model_deny:
            rows.append(
                f"  models: allow {', '.join(self.model_allow) or '*'}; deny {', '.join(self.model_deny) or '-'}"
            )
        if self.zero_retention:
            rows.append("  zero retention: sidechains and memory writers are off")
        rows.extend(f"  {note}" for note in self.notes)
        return rows

    def to_metadata(self) -> dict[str, Any]:
        """What the evidence pack records."""
        return {
            "source": self.source,
            "org": self.org,
            "version": self.version,
            "fingerprint": self.fingerprint,
            "signed": self.signed,
        }


def _matches(value: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(fnmatch.fnmatchcase(value, str(p)) or value == str(p) for p in patterns)


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(v.strip() for v in value.split(",") if v.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return ()


def _canonical(policy: dict[str, Any]) -> bytes:
    return json.dumps(
        policy, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def parse_policy(body: dict[str, Any], *, source: str, signed: bool) -> ManagedPolicy:
    """Build a `ManagedPolicy` from the `policy` object of a document."""
    plugins = body.get("plugins") if isinstance(body.get("plugins"), dict) else {}
    models = (
        body.get("model_policy") if isinstance(body.get("model_policy"), dict) else {}
    )
    lock = (
        body.get("provider_lock") if isinstance(body.get("provider_lock"), dict) else {}
    )
    return ManagedPolicy(
        source=source,
        org=str(body.get("org", "") or ""),
        version=str(body.get("version", "") or ""),
        fingerprint="sha256:" + hashlib.sha256(_canonical(body)).hexdigest()[:24],
        signed=signed,
        allowed_mcp_servers=_strings(body["allowed_mcp_servers"])
        if "allowed_mcp_servers" in body
        else None,
        skill_allowlist=_strings(body["skill_allowlist"])
        if "skill_allowlist" in body
        else None,
        required_plugins=_strings(plugins.get("required")),
        optional_plugins=_strings(plugins.get("optional")),
        forbidden_plugins=_strings(plugins.get("forbidden")),
        provider_lock={str(k): str(v) for k, v in lock.items() if str(v).strip()},
        zero_retention=bool(body.get("zero_retention")),
        model_allow=_strings(models.get("allow")),
        model_deny=_strings(models.get("deny")),
        notes=_strings(body.get("notes")),
    )


def verify_document(
    document: dict[str, Any], *, public_key_b64: str | None
) -> tuple[dict[str, Any], bool]:
    """Return `(policy body, signed)` for a policy document.

    Rules: with a pinned key, the document must carry a `signature` over the
    canonical `policy` bytes made by that key; without a pinned key the body is
    accepted unsigned (the caller decides whether that is acceptable).

    Raises:
        TypeError: When the document has no `policy` object.
        ValueError: When a pinned key is set and the signature is missing or does not verify.
    """
    body = document.get("policy")
    if not isinstance(body, dict):
        msg = "managed policy document has no `policy` object"
        raise TypeError(msg)
    signature = document.get("signature")
    if public_key_b64:
        if not isinstance(signature, str) or not signature:
            msg = "managed policy is unsigned but a public key is pinned"
            raise ValueError(msg)
        from bog_agents_cli.tracefile.signing import (
            SignatureVerificationError,
            material_from_public_b64,
            verify,
        )

        try:
            material = material_from_public_b64(public_key_b64)
            verified = verify(material, _canonical(body), signature)
        except SignatureVerificationError as exc:
            msg = f"managed policy signature does not verify against the pinned key: {exc}"
            raise ValueError(msg) from exc
        if not verified:
            msg = "managed policy signature does not verify against the pinned key"
            raise ValueError(msg)
        return body, True
    return body, False


def sign_document(body: dict[str, Any], *, key_path: Path) -> dict[str, Any]:
    """Wrap `body` as a signed document with the TraceFile key at `key_path` (org tooling / tests)."""
    from bog_agents_cli.tracefile.signing import load_keypair_from_path, sign

    material = load_keypair_from_path(key_path)
    return {
        "policy": body,
        "signature": sign(material, _canonical(body)),
        "signer": material.public_key_b64,
    }


def _read_source(source: str) -> bytes:
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT) as response:
            return response.read(_MAX_BYTES + 1)
    return Path(source).read_bytes()[: _MAX_BYTES + 1]


def load_managed_policy(
    *,
    source: str | None,
    public_key_b64: str | None,
    cache_dir: Path,
    fetch: Any = None,  # noqa: ANN401 - Callable[[str], bytes] for tests
) -> ManagedPolicy | None:
    """Fetch, verify, cache and parse the policy; `None` when no source is configured or it is rejected.

    A URL source needs a pinned key (an unsigned org policy over the network is
    refused); a path source may be unsigned. On a fetch failure the last good
    cached copy is used.
    """
    if not source:
        return None
    if source.startswith(("http://", "https://")) and not public_key_b64:
        logger.warning(
            "managed policy REJECTED (%s): a policy served over the network requires managed.policy_public_key",
            source,
        )
        return None
    reader = fetch or _read_source
    cache_path = Path(cache_dir) / CACHE_NAME
    raw: bytes | None = None
    try:
        raw = reader(source)
    except Exception as exc:
        logger.warning(
            "managed policy: could not fetch %s (%s); using the cached copy if any",
            source,
            exc,
        )
    if raw is None or len(raw) > _MAX_BYTES:
        if raw is not None:
            logger.warning(
                "managed policy: %s exceeds %d bytes; using the cached copy if any",
                source,
                _MAX_BYTES,
            )
        try:
            raw = cache_path.read_bytes()
        except OSError:
            return None
    try:
        document = json.loads(raw.decode("utf-8"))
        body, signed = verify_document(
            document if isinstance(document, dict) else {},
            public_key_b64=public_key_b64,
        )
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        logger.warning("managed policy REJECTED (%s): %s", source, exc)
        return None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("managed policy: cache write failed", exc_info=True)
    return parse_policy(body, source=source, signed=signed)


def configured_source() -> tuple[str | None, str | None]:
    """`(source, public_key_b64)` from `managed.policy_source` / `managed.policy_public_key`."""
    try:
        from bog_agents_cli.config_manifest import resolve_option

        return (
            str(resolve_option("managed.policy_source") or "") or None,
            str(resolve_option("managed.policy_public_key") or "") or None,
        )
    except Exception:
        return None, None


def _cache_dir() -> Path:
    from bog_agents_cli.config import settings

    return Path(settings.user_agents_dir)


def active_policy(*, refresh: bool = False) -> ManagedPolicy | None:
    """The process-wide policy (loaded once per process; `refresh=True` re-fetches)."""
    if not refresh and "policy" in _CACHE and _CACHE.get("at", 0) > time.time() - 3600:
        return _CACHE["policy"]
    source, key = configured_source()
    policy = None
    if source:
        policy = load_managed_policy(
            source=source, public_key_b64=key, cache_dir=_cache_dir()
        )
    _CACHE["policy"] = policy
    _CACHE["at"] = time.time()
    return policy


def reset_cache() -> None:
    """Forget the process-wide policy (tests, `/permissions reload-policy`)."""
    _CACHE.clear()


def install_skill_filter(policy: ManagedPolicy | None) -> None:
    """Install (or clear) the SDK skill-directory filter from the policy's `skill_allowlist`."""
    from bog_agents.middleware.skills import set_skill_dir_filter

    if policy is None or policy.skill_allowlist is None:
        set_skill_dir_filter(None)
        return
    set_skill_dir_filter(policy.skill_allowed)


def assert_model_switch_fact(
    spec: str, working_dir: str | Path | None, *, refusal: str | None
) -> None:
    """Assert a `model_switch` fact into the Expert engine so YAML rules can react (best effort)."""
    if working_dir is None:
        return
    try:
        from bog_agents.middleware.expert_engine.types import Fact

        from bog_agents_cli.expert_controller import get_controller

        engine = get_controller(working_dir).middleware.engine
        engine.assert_fact(
            Fact(
                fact_type="model_switch",
                data={"model": spec, "refused": bool(refusal), "reason": refusal or ""},
            )
        )
    except Exception:
        logger.debug("model_switch fact not asserted", exc_info=True)


__all__ = [
    "CACHE_NAME",
    "ManagedPolicy",
    "active_policy",
    "assert_model_switch_fact",
    "configured_source",
    "install_skill_filter",
    "load_managed_policy",
    "parse_policy",
    "reset_cache",
    "sign_document",
    "verify_document",
]
