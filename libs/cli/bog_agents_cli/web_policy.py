"""Outbound domain policy for `fetch_url` / `http_request` (ROADMAP #48).

One process-wide `WebPolicy` — allowed and blocked domain lists — consulted by
`web_fetch.assert_fetch_allowed`, the single gate every outbound fetch and
every redirect hop already passes through for SSRF checks. Matching reuses
the egress proxy's suffix rule (`example.com` also covers `api.example.com`,
never `notexample.com`). A blocklist always wins; an allowlist, when set,
refuses everything not on it; an empty policy is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass

from bog_agents.sandbox.egress_proxy import host_allowed


@dataclass(frozen=True)
class WebPolicy:
    """Allowed / blocked domain lists (suffix-matched on labels)."""

    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        """Whether the policy constrains anything."""
        return bool(self.allowed_domains or self.blocked_domains)

    def violation(self, host: str) -> str | None:
        """Why `host` may not be fetched, or `None` when it may."""
        name = (host or "").strip().lower().rstrip(".")
        if not name:
            return None
        if self.blocked_domains and host_allowed(name, self.blocked_domains):
            return f"{name} is on the blocked-domain list"
        if self.allowed_domains and not host_allowed(name, self.allowed_domains):
            return f"{name} is not on the allowed-domain list ({', '.join(self.allowed_domains)})"
        return None

    def merged(self, other: WebPolicy | None) -> WebPolicy:
        """Union of two policies (both allowlists apply; blocklists concatenate)."""
        if other is None or not other.active:
            return self
        return WebPolicy(
            allowed_domains=tuple(
                dict.fromkeys(self.allowed_domains + other.allowed_domains)
            ),
            blocked_domains=tuple(
                dict.fromkeys(self.blocked_domains + other.blocked_domains)
            ),
        )


_POLICY = WebPolicy()


def set_web_policy(policy: WebPolicy | None) -> None:
    """Install the process-wide policy (`None` clears it)."""
    global _POLICY  # noqa: PLW0603 - process-wide policy by design
    _POLICY = policy or WebPolicy()


def get_web_policy() -> WebPolicy:
    """The active policy."""
    return _POLICY


def policy_from_strings(allowed: str | None, blocked: str | None) -> WebPolicy:
    """Build a policy from comma-separated manifest values."""

    def _split(raw: str | None) -> tuple[str, ...]:
        return tuple(v.strip().lower() for v in (raw or "").split(",") if v.strip())

    return WebPolicy(allowed_domains=_split(allowed), blocked_domains=_split(blocked))


__all__ = ["WebPolicy", "get_web_policy", "policy_from_strings", "set_web_policy"]
