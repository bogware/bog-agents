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
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from bog_agents_cli.unicode_security import check_url_safety

logger = logging.getLogger(__name__)

# Hard ceiling on fetched body size before truncation. ~512 KiB is
# generous for documentation pages but keeps us from hauling a 10 MB
# release-notes page into the model context. Configurable via env.
_DEFAULT_MAX_BYTES = 512 * 1024
_DEFAULT_TIMEOUT_SECONDS = 15.0

# User-agent identifies the request as ours; some sites return a
# javascript-only page to default Python urllib UAs.
_USER_AGENT = (
    "Mozilla/5.0 (compatible; bog-agents-cli/0.8; +https://bog-agents)"
)


class WebFetchError(RuntimeError):
    """Raised when a fetch fails before we have any usable body."""


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

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout_seconds
        ) as response:
            final_url = response.geturl()
            status = response.status
            content_type = response.headers.get_content_type() or ""
            raw = response.read(max_bytes + 1)
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
            charset = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip()
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
    "WebFetchError",
    "fetch_url",
    "render_prompt_block",
]
