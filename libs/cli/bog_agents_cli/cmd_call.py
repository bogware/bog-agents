"""bog-agents call — thin HTTP client for a long-lived `--serve` instance.

Eliminates the ~5-10s langgraph dev startup cost per `-n` invocation by
talking to a server you've already started elsewhere with
`bog-agents-cli --serve`. Maps directly to the `/invoke` endpoint
exposed by `bog_agents.serve`.

Examples:
    # one-off:
    bog-agents-cli --serve --serve-port 8420 &
    bog-agents-cli call "Reply with: pong"

    # JSON envelope:
    bog-agents-cli call "Summarize this repo" --json

    # resume an existing thread:
    bog-agents-cli call "and now what about errors?" --thread <id>
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8420
_DEFAULT_TIMEOUT_SECS = 1800  # 30 min — matches BOG_DAEMON_AGENT_TIMEOUT


def _server_url(host: str, port: int) -> str:
    """Build the base server URL for the running --serve instance."""
    return f"http://{host}:{port}"


def _post_invoke(
    host: str,
    port: int,
    *,
    message: str,
    thread_id: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT_SECS,
) -> dict[str, Any]:
    """POST /invoke and return the parsed JSON response.

    Args:
        host: Server host.
        port: Server port.
        message: User message to send.
        thread_id: Optional existing thread ID (resume conversation).
        timeout: Per-request timeout in seconds.

    Returns:
        Parsed JSON response dict.
    """
    url = f"{_server_url(host, port)}/invoke"
    body: dict[str, Any] = {"message": message}
    if thread_id:
        body["thread_id"] = thread_id
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cmd_call(args: argparse.Namespace) -> int:
    """Send a single message to a running `bog-agents-cli --serve` instance.

    Args:
        args: Parsed argparse namespace with `message`, `host`, `port`,
            `thread`, `timeout`, `output_format`.

    Returns:
        Process exit code (0 success, 1 transport error, 2 server error).
    """
    message = args.message
    if not message:
        sys.stderr.write("Error: call requires a non-empty message argument.\n")
        return 2

    host: str = args.host or _DEFAULT_HOST
    port: int = args.port or _DEFAULT_PORT
    thread_id: str | None = args.thread or None
    timeout: int = args.timeout or _DEFAULT_TIMEOUT_SECS
    output_format: str = getattr(args, "output_format", "text") or "text"

    try:
        result = _post_invoke(
            host, port, message=message, thread_id=thread_id, timeout=timeout
        )
    except urllib.error.HTTPError as exc:
        # Server is up, but rejected our request — surface the status + body
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        sys.stderr.write(f"Server error {exc.code}: {exc.reason}\n{detail}\n")
        return 2
    except (urllib.error.URLError, OSError) as exc:
        sys.stderr.write(
            f"Cannot reach bog-agents server at {_server_url(host, port)}.\n"
            f"Start one with: bog-agents-cli --serve --serve-host {host} --serve-port {port}\n"
            f"Underlying error: {exc}\n"
        )
        return 1
    except TimeoutError:
        sys.stderr.write(f"Request timed out after {timeout}s.\n")
        return 1

    response_text = result.get("response", "") or ""
    received_thread = result.get("thread_id", "") or ""

    if output_format == "json":
        envelope = {
            "schema_version": 1,
            "command": "call",
            "data": {
                "thread_id": received_thread,
                "response": response_text,
                "metadata": result.get("metadata") or {},
            },
        }
        sys.stdout.write(json.dumps(envelope) + "\n")
    else:
        sys.stdout.write(response_text)
        if response_text and not response_text.endswith("\n"):
            sys.stdout.write("\n")
        if received_thread and not args.quiet:
            sys.stderr.write(f"thread_id: {received_thread}\n")
    sys.stdout.flush()
    return 0


def setup_call_parser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: Iterable[argparse.ArgumentParser] = (),
) -> None:
    """Wire the `call` subcommand into the top-level argparse tree.

    Args:
        subparsers: argparse subparsers action returned by `add_subparsers()`.
        parents: Parent parsers to inherit (for shared `-h`/`--help`).
    """
    p = subparsers.add_parser(
        "call",
        help="Send one message to a running `--serve` instance and print the response",
        parents=list(parents),
        add_help=not list(parents),
    )
    p.add_argument("message", help="Message to send to the agent")
    p.add_argument(
        "--host", default=_DEFAULT_HOST, help=f"Server host (default: {_DEFAULT_HOST})"
    )
    p.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help=f"Server port (default: {_DEFAULT_PORT})",
    )
    p.add_argument(
        "--thread",
        default="",
        metavar="ID",
        help="Resume an existing thread by ID (omit to start a new one)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=_DEFAULT_TIMEOUT_SECS,
        help=f"Request timeout in seconds (default: {_DEFAULT_TIMEOUT_SECS})",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress the trailing thread-id stderr line",
    )
    p.add_argument(
        "--json",
        action="store_const",
        const="json",
        dest="output_format",
        help="Emit a JSON envelope on stdout",
    )
