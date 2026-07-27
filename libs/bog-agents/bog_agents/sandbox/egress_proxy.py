"""Localhost allowlist egress proxy for sandboxed shell execution (#22).

A native OS sandbox (bubblewrap / seatbelt, see `local_sandbox.py`) can only
cut the network namespace *entirely* — it has no notion of "allow github.com
but nothing else". Fine-grained egress needs a proxy: keep the sandbox's
network namespace open, then route all traffic through a small forward proxy
that only lets allowlisted hosts through.

This module provides that proxy plus the pure pieces around it:

  * `host_allowed(host, allowlist)` — the allow/deny decision (suffix match).
  * `parse_connect_target(request_line)` — parse a CONNECT request line.
  * `AllowlistEgressProxy` — a threaded HTTP CONNECT proxy that tunnels
    allowlisted hosts and returns ``403`` for everything else.
  * `egress_env_for(proxy_url)` — the proxy env vars to inject into a child so
    well-behaved tools (curl, pip, git-over-https) route through it.

Honesty caveat (documented, by design): a forward proxy constrains
proxy-respecting tools; it is **not** a kernel boundary. A process that opens a
raw socket to a bare IP bypasses it. For a hard guarantee use a full network
cut (`allow_network=False` and no allowlist → `--unshare-net`). The allowlist
mode is the standard "bounded egress for cooperating tooling" that Codex /
Claude `/sandbox` ship, and it is enforced here for exactly that class of tool.
"""

from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass
from typing import Self

logger = logging.getLogger(__name__)

# Env var carrying the proxy URL a runner exports so a backend can route a
# sandboxed child's egress through the allowlist proxy.
SANDBOX_EGRESS_PROXY_ENV = "BOG_AGENTS_SANDBOX_EGRESS_PROXY"

# Hosts that must never be proxied (the proxy itself + loopback).
_DEFAULT_NO_PROXY = "localhost,127.0.0.1,::1"

_RELAY_CHUNK = 65536
_CONNECT_TIMEOUT = 10.0


def host_allowed(host: str, allowlist: list[str] | tuple[str, ...]) -> bool:
    """Return whether `host` is permitted by `allowlist`.

    Matching is case-insensitive and suffix-based on domain labels: an entry
    ``github.com`` allows ``github.com`` and any subdomain (``api.github.com``)
    but not a look-alike (``notgithub.com``). An empty allowlist denies
    everything (fail-closed) — an allowlist proxy with no entries is a hard
    block, which is the safe default if a caller forgets to populate it.

    Args:
        host: The target hostname (no port).
        allowlist: Permitted hostnames / parent domains.

    Returns:
        True if egress to `host` is allowed.
    """
    if not host or not allowlist:
        return False
    h = host.strip().rstrip(".").lower()
    for entry in allowlist:
        e = entry.strip().rstrip(".").lower()
        if not e:
            continue
        if h == e or h.endswith("." + e):
            return True
    return False


def parse_connect_target(request_line: str) -> tuple[str, int] | None:
    """Parse ``CONNECT host:port HTTP/1.1`` into ``(host, port)``.

    Args:
        request_line: The first line of an HTTP request (no trailing CRLF
            required).

    Returns:
        ``(host, port)`` for a well-formed CONNECT line, else None. A missing
        port defaults to 443 (CONNECT is HTTPS in practice).
    """
    parts = request_line.split()
    if len(parts) < 2 or parts[0].upper() != "CONNECT":  # noqa: PLR2004
        return None
    target = parts[1]
    # IPv6 literal form [::1]:443
    if target.startswith("[") and "]" in target:
        host, _, port_str = target.partition("]")
        host = host[1:]
        port_str = port_str.lstrip(":")
    else:
        host, _, port_str = target.partition(":")
    if not host:
        return None
    try:
        port = int(port_str) if port_str else 443
    except ValueError:
        return None
    return host, port


def egress_env_for(proxy_url: str, *, no_proxy: str = _DEFAULT_NO_PROXY) -> dict[str, str]:
    """Build the proxy environment variables for a sandboxed child.

    Sets both upper- and lower-case forms (tools disagree on which they read)
    for HTTP and HTTPS, plus ``NO_PROXY`` so loopback (and the proxy itself)
    is never routed through the proxy.

    Args:
        proxy_url: The allowlist proxy URL, e.g. ``http://127.0.0.1:8888``.
        no_proxy: Comma-separated hosts to exclude from proxying.

    Returns:
        A dict of environment variables to merge into the child's environment.
    """
    return {
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
    }


@dataclass
class _ProxyStats:
    """Counters for observability / tests."""

    allowed: int = 0
    denied: int = 0


class AllowlistEgressProxy:
    """A threaded HTTP CONNECT proxy that only tunnels allowlisted hosts.

    Start it, read `url`, export `egress_env_for(proxy.url)` into a sandboxed
    child, and every HTTPS connection the child opens is checked against the
    allowlist: allowed hosts are tunnelled transparently, denied hosts get a
    ``403 Forbidden`` and the connection is closed.

    Only the CONNECT method is handled (the HTTPS case that covers pip / git /
    curl / package managers). Plain-HTTP absolute-form requests are refused
    with ``405`` — route sensitive egress over HTTPS.

    Example:
        ```python
        proxy = AllowlistEgressProxy(["pypi.org", "github.com"])
        proxy.start()
        env = egress_env_for(proxy.url)
        # ... run child with env merged in ...
        proxy.stop()
        ```
    """

    def __init__(
        self,
        allowlist: list[str] | tuple[str, ...],
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        """Initialize the proxy.

        Args:
            allowlist: Hostnames / parent domains egress is permitted to.
            host: Bind address (loopback by default — never bind publicly).
            port: Bind port; 0 (default) picks an ephemeral free port.
        """
        self._allowlist = tuple(allowlist)
        self._bind_host = host
        self._bind_port = port
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._bound: tuple[str, int] | None = None
        self.stats = _ProxyStats()

    @property
    def url(self) -> str:
        """The ``http://host:port`` URL clients should use as their proxy."""
        if self._bound is None:
            msg = "proxy is not started"
            raise RuntimeError(msg)
        host, port = self._bound
        return f"http://{host}:{port}"

    @property
    def address(self) -> tuple[str, int]:
        """The bound ``(host, port)`` (valid after `start`)."""
        if self._bound is None:
            msg = "proxy is not started"
            raise RuntimeError(msg)
        return self._bound

    def start(self) -> None:
        """Bind, listen, and start serving on a background thread."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self._bind_host, self._bind_port))
        server.listen(64)
        server.settimeout(0.5)
        self._server = server
        self._bound = server.getsockname()
        self._thread = threading.Thread(target=self._serve, name="egress-proxy", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and close the listen socket."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._server is not None:
            self._server.close()
            self._server = None

    def __enter__(self) -> Self:
        """Start the proxy on context entry."""
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop the proxy on context exit."""
        self.stop()

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                client, _addr = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            client.settimeout(_CONNECT_TIMEOUT)
            request_line = self._read_request_line(client)
            target = parse_connect_target(request_line)
            if target is None:
                self._respond(client, 405, "Method Not Allowed", "Only CONNECT is supported.")
                return
            host, port = target
            if not host_allowed(host, self._allowlist):
                self.stats.denied += 1
                logger.info("egress denied: %s:%s not in allowlist", host, port)
                self._respond(client, 403, "Forbidden", f"Egress to {host} is not allowlisted.")
                return
            self.stats.allowed += 1
            self._tunnel(client, host, port)
        except OSError:
            pass
        finally:
            with _suppress_oserror():
                client.close()

    @staticmethod
    def _read_request_line(client: socket.socket) -> str:
        """Read bytes up to the first CRLF (the request line)."""
        buf = bytearray()
        while b"\r\n" not in buf and len(buf) < 8192:  # noqa: PLR2004
            chunk = client.recv(1024)
            if not chunk:
                break
            buf.extend(chunk)
        line, _, _ = bytes(buf).partition(b"\r\n")
        return line.decode("latin-1", errors="replace")

    def _respond(self, client: socket.socket, code: int, reason: str, body: str) -> None:
        payload = body.encode("utf-8")
        header = (f"HTTP/1.1 {code} {reason}\r\nContent-Length: {len(payload)}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n").encode(
            "latin-1"
        )
        with _suppress_oserror():
            client.sendall(header + payload)

    def _tunnel(self, client: socket.socket, host: str, port: int) -> None:
        try:
            upstream = socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT)
        except OSError as exc:
            self._respond(client, 502, "Bad Gateway", f"Upstream connect failed: {exc}")
            return
        try:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._relay(client, upstream)
        finally:
            with _suppress_oserror():
                upstream.close()

    def _relay(self, a: socket.socket, b: socket.socket) -> None:
        """Bidirectionally relay bytes between two sockets until both close.

        Blocks until both directions reach EOF — the caller (`_handle`) closes
        the client socket in its ``finally``, so returning early here would tear
        the tunnel down mid-transfer.
        """
        pumps = [
            threading.Thread(target=self._pump, args=(a, b), daemon=True),
            threading.Thread(target=self._pump, args=(b, a), daemon=True),
        ]
        for t in pumps:
            t.start()
        for t in pumps:
            t.join()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        with _suppress_oserror():
            src.settimeout(None)
            while not self._stop.is_set():
                data = src.recv(_RELAY_CHUNK)
                if not data:
                    break
                dst.sendall(data)
        with _suppress_oserror():
            dst.shutdown(socket.SHUT_WR)


class _suppress_oserror:  # noqa: N801 - context-manager helper, lowercase by convention
    """Tiny context manager: swallow OSError during best-effort socket teardown."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_rest: object) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


__all__ = [
    "SANDBOX_EGRESS_PROXY_ENV",
    "AllowlistEgressProxy",
    "egress_env_for",
    "host_allowed",
    "parse_connect_target",
]
