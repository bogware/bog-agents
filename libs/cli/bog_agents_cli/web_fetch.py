"""``/web <url>`` — fetch a URL into the conversation as context.

Self-contained module so the slash-command handler in ``app.py``
stays thin and the logic is testable without spinning up the TUI.

What this does
--------------

1. Validates that *url* is an http(s) URL with no obvious unicode
   spoofing. Reuses :mod:`bog_agents_cli.unicode_security` so we get
   the same gates as our other URL-handling paths.
2. Fetches the page with a short timeout, follows up to a few
   redirects, caps total bytes read.
3. Converts HTML → plain-ish markdown (we strip ``<script>``,
   ``<style>``, etc., collapse whitespace, and decode entities).
4. Returns a synthesized prompt the CLI hands straight to the agent
   so the *agent* sees the fetched content as a normal user turn
   with a citation tag. The agent then operates on that content with
   its existing tool surface.

We deliberately do **not** stash the fetched HTML in some side
channel. The Anthropic / OpenAI provenance loops and trace-mind
benefit from seeing the actual content in the message stream.
"""

from __future__ import annotations

import html
import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from bog_agents_cli.unicode_security import check_url_safety

logger = logging.getLogger(__name__)

# Hard ceiling on redirect hops we are willing to follow. Each hop's host
# is independently re-validated through the SSRF guard, so this only bounds
# redirect loops / chains.
_MAX_REDIRECTS = 5

# Hard ceiling on fetched body size before truncation. ~512 KiB is
# generous for documentation pages but keeps us from hauling a 10 MB
# release-notes page into the model context. Configurable via env.
_DEFAULT_MAX_BYTES = 512 * 1024
_DEFAULT_TIMEOUT_SECONDS = 15.0

# User-agent identifies the request as ours; some sites return a
# javascript-only page to default Python urllib UAs.
_USER_AGENT = "Mozilla/5.0 (compatible; bog-agents-cli/0.8; +https://bog-agents)"


class WebFetchError(RuntimeError):
    """Raised when a fetch fails before we have any usable body."""


class DomainPolicyError(WebFetchError):
    """Raised when a URL's host is refused by the domain policy (ROADMAP #48).

    `web.allowed_domains` / `web.blocked_domains` (or the trust profile's
    lists) are checked before DNS, so a refused host is never even resolved.
    """


class SsrfError(WebFetchError):
    """Raised when a URL is rejected by the SSRF guard.

    A subclass of :class:`WebFetchError` so existing ``except WebFetchError``
    handlers continue to catch it, while callers that care can distinguish an
    SSRF policy denial from a generic network failure.
    """


# ---------------------------------------------------------------------------
# SSRF guard (shared by /web, the agent fetch_url tool, and http_request)
# ---------------------------------------------------------------------------


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether an IP address falls in a range we refuse to fetch.

    Blocks loopback, link-local (incl. the 169.254.169.254 cloud metadata
    endpoint), private, reserved, multicast, and unspecified ranges, plus
    IPv4-mapped IPv6 addresses whose embedded IPv4 is itself blocked.

    Args:
        ip: A parsed IPv4 or IPv6 address.

    Returns:
        `True` when the address must not be fetched, else `False`.
    """
    # Unwrap IPv4-mapped / 6to4 IPv6 addresses so ::ffff:169.254.169.254 and
    # similar can't tunnel past the IPv4 checks.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _ip_is_blocked(mapped)
    sixtofour = getattr(ip, "sixtofour", None)
    if sixtofour is not None and _ip_is_blocked(sixtofour):
        return True

    return bool(
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_host_addresses(
    host: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve *host* to every IP address it maps to.

    Accepts a bare IP literal (returned directly) or a DNS name (resolved
    via :func:`socket.getaddrinfo`). Stripping any surrounding brackets from
    IPv6 literals is handled by the caller via ``urlsplit().hostname``.

    Args:
        host: Hostname or IP literal (no brackets, no port).

    Returns:
        List of parsed IP addresses the host resolves to (non-empty on
        success).

    Raises:
        SsrfError: When the host cannot be resolved.
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        msg = f"Could not resolve host {host!r}: {exc}"
        raise SsrfError(msg) from exc

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        raw_ip = sockaddr[0]
        try:
            addresses.append(ipaddress.ip_address(raw_ip))
        except ValueError:
            continue
    if not addresses:
        msg = f"Could not resolve host {host!r} to any IP address."
        raise SsrfError(msg)
    return addresses


def assert_fetch_allowed(url: str) -> None:
    """Validate *url* against the SSRF policy, raising on any violation.

    This is the single shared gate for every outbound fetch in the CLI. It
    is intentionally *separate* from
    :func:`bog_agents_cli.unicode_security.check_url_safety` (the unicode /
    confusable gate) — both should be applied.

    Policy:
        * Only ``http`` / ``https`` schemes are permitted.
        * The URL must carry a host.
        * Every IP the host resolves to must be a public, routable address.
          Loopback, link-local (incl. ``169.254.169.254``), private,
          reserved, multicast, and unspecified addresses are rejected. This
          covers ``localhost`` and ``::1`` because they resolve to loopback.

    Args:
        url: The URL about to be fetched (an absolute http(s) URL).

    Raises:
        SsrfError: When the scheme is not http(s), the host is missing, the
            host cannot be resolved, or any resolved address is blocked.
    """
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        msg = f"Only http and https URLs are supported (got {scheme!r})."
        raise SsrfError(msg)

    host = parsed.hostname
    if not host:
        msg = "URL has no host."
        raise SsrfError(msg)

    # ROADMAP #48: the domain policy gates every hop before DNS.
    from bog_agents_cli.web_policy import get_web_policy

    reason = get_web_policy().violation(host)
    if reason is not None:
        msg = f"Refusing to fetch {url!r}: {reason} (domain policy)."
        raise DomainPolicyError(msg)

    for ip in _resolve_host_addresses(host):
        if _ip_is_blocked(ip):
            msg = (
                f"Refusing to fetch {url!r}: host {host!r} resolves to "
                f"non-public address {ip} (SSRF guard)."
            )
            raise SsrfError(msg)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib redirect handler that never auto-follows.

    Returning the redirect response (rather than following it) lets us
    re-validate the ``Location`` host through the SSRF guard before issuing
    the next request.
    """

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:  # noqa: PLR6301  # overrides base method
        return None


def _open_with_guarded_redirects(
    url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
) -> tuple[str, int, str, bytes]:
    """Fetch *url*, manually following redirects with a per-hop SSRF check.

    Auto-redirects are disabled; each ``3xx`` ``Location`` is resolved
    against the current URL and re-validated via :func:`assert_fetch_allowed`
    before the next hop is issued. This defeats a public-host → private-host
    redirect (e.g. a 302 into the cloud metadata endpoint).

    Args:
        url: The already-validated initial URL.
        timeout_seconds: Per-request timeout.
        max_bytes: Hard cap on the body read.

    Returns:
        Tuple of ``(final_url, status_code, content_type, raw_body_bytes)``.

    Note:
        Non-redirect error responses surface as ``urllib.error.HTTPError``
        propagated from the opener; the caller turns those into a
        :class:`FetchResult`.

    Raises:
        SsrfError: When a redirect target violates the SSRF policy or a
            redirect is missing its ``Location`` header.
        WebFetchError: When too many redirects are encountered.
    """
    opener = urllib.request.build_opener(_NoRedirectHandler)
    current_url = url

    for _hop in range(_MAX_REDIRECTS + 1):
        request = urllib.request.Request(
            current_url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with opener.open(request, timeout=timeout_seconds) as response:
            status = response.status
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    msg = f"Redirect ({status}) from {current_url!r} had no Location header."
                    raise SsrfError(msg)
                next_url = urllib.parse.urljoin(current_url, location)
                # Re-validate scheme + host of the new hop BEFORE connecting.
                assert_fetch_allowed(next_url)
                current_url = next_url
                continue

            final_url = response.geturl()
            content_type = response.headers.get_content_type() or ""
            raw = response.read(max_bytes + 1)
            return final_url, status, content_type, raw

    msg = f"Too many redirects (>{_MAX_REDIRECTS}) starting from {url!r}."
    raise WebFetchError(msg)


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of a ``/web`` fetch."""

    url: str
    final_url: str
    """The URL we actually fetched, after redirects."""
    status_code: int
    content_type: str
    body: str
    """Plain-text rendering of the page body."""
    truncated: bool


def fetch_url(
    url: str,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> FetchResult:
    """Fetch *url* and return a :class:`FetchResult` with cleaned text.

    Args:
        url: ``http://`` or ``https://`` URL.
        timeout_seconds: Per-request timeout.
        max_bytes: Hard cap on response body size; bodies larger than
            this are truncated and ``truncated=True`` on the result.

    Returns:
        :class:`FetchResult` with a plain-text rendering. Even
        non-2xx responses return a result (so the agent sees the
        actual status code and message); only network-level failures
        raise :class:`WebFetchError`.

    Raises:
        WebFetchError: Validation failure (bad scheme, unsafe URL) or
            network-level failure (DNS, TLS, timeout).
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        msg = f"Only http and https URLs are supported (got {parsed.scheme!r})."
        raise WebFetchError(msg)
    if not parsed.netloc:
        msg = "URL has no host."
        raise WebFetchError(msg)
    safety = check_url_safety(url)
    if not safety.safe:
        reason = safety.warnings[0] if safety.warnings else "unicode spoofing risk"
        msg = f"Refusing to fetch unsafe URL: {reason}"
        raise WebFetchError(msg)
    # SSRF guard — separate from the unicode gate above. Validate the
    # initial URL; each redirect hop below is re-validated independently.
    assert_fetch_allowed(url)

    try:
        final_url, status, content_type, raw = _open_with_guarded_redirects(
            url,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )
    except urllib.error.HTTPError as exc:
        # Non-2xx responses are still useful — return them rather than
        # raise so the agent can act on the status code.
        try:
            body = exc.read().decode("utf-8", errors="replace")[:max_bytes]
        except Exception:
            body = ""
        return FetchResult(
            url=url,
            final_url=getattr(exc, "url", url) or url,
            status_code=exc.code,
            content_type=exc.headers.get_content_type() if exc.headers else "",
            body=_clean_text(body),
            truncated=False,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        msg = f"Network error fetching {url}: {exc}"
        raise WebFetchError(msg) from exc

    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    body_text = _decode_body(raw, content_type)
    if "html" in content_type.lower():
        body_text = _html_to_text(body_text)
    body_text = _clean_text(body_text)

    return FetchResult(
        url=url,
        final_url=final_url,
        status_code=status,
        content_type=content_type,
        body=body_text,
        truncated=truncated,
    )


def render_prompt_block(result: FetchResult, *, intent: str = "") -> str:
    """Format a :class:`FetchResult` as a prompt the agent can consume.

    The block carries a citation marker, the final URL (so the agent
    cites the post-redirect canonical URL), and the cleaned body. We
    leave an empty trailing ``"Question:"`` line so the wrapping
    intent string is added on top by the caller without ambiguity.
    """
    lines = [
        "# Fetched web context",
        f"Source: {result.final_url}",
        f"HTTP status: {result.status_code}  Content-type: {result.content_type or 'unknown'}",
    ]
    if result.truncated:
        lines.append("(body truncated to the configured size cap)")
    lines.extend(["", "## Body", "", result.body])
    if intent.strip():
        lines.extend(["", "## Instruction", intent.strip()])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_body(raw: bytes, content_type: str) -> str:
    """Best-effort decode of the response body to text.

    Honors ``charset=`` in the Content-Type header when present;
    otherwise falls back to utf-8 with replacement so malformed bytes
    don't crash the pipeline.
    """
    charset = "utf-8"
    if "charset=" in content_type.lower():
        try:
            charset = (
                content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip()
            )
        except IndexError:
            pass
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript|template)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINE_RE = re.compile(r"\n\s*\n\s*\n+")


def _html_to_text(html_text: str) -> str:
    """Strip HTML tags and decode entities to plain text.

    Intentionally simple — we are not trying to compete with
    Readability or trafilatura. The goal is "the page's main text
    content, more or less, without script/style noise". The agent
    can ask for the original URL again if it wants the structured
    form.
    """
    no_script = _SCRIPT_STYLE_RE.sub("", html_text)
    no_tags = _TAG_RE.sub("\n", no_script)
    return html.unescape(no_tags)


def _clean_text(text: str) -> str:
    """Normalise whitespace so the prompt block stays compact."""
    if not text:
        return ""
    # Collapse runs of horizontal whitespace.
    text = _WHITESPACE_RE.sub(" ", text)
    # Drop trailing whitespace per line.
    text = "\n".join(line.rstrip() for line in text.splitlines())
    # Collapse multi-blank-line runs to a single blank line.
    text = _BLANK_LINE_RE.sub("\n\n", text)
    return text.strip()


__all__ = [
    "FetchResult",
    "SsrfError",
    "WebFetchError",
    "assert_fetch_allowed",
    "fetch_url",
    "render_prompt_block",
]
