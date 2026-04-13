"""Health check diagnostics for the CLI.

Feature #33: /doctor command — self-diagnostics to verify the CLI
environment is properly configured.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
import platform
import shutil
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

logger = logging.getLogger(__name__)


def _normalize_ollama_host(raw_host: str | None) -> str:
    """Normalize Ollama host configuration into an HTTP base URL."""
    host = (raw_host or "").strip() or "http://127.0.0.1:11434"
    if "://" not in host:
        host = f"http://{host}"
    parsed = urlparse(host)
    scheme = parsed.scheme or "http"
    netloc = parsed.netloc or parsed.path
    path = parsed.path if parsed.netloc else ""
    return f"{scheme}://{netloc}{path}".rstrip("/")


def _get_ollama_version() -> str | None:
    """Return the Ollama daemon version when the local API is reachable."""
    base_url = _normalize_ollama_host(os.environ.get("OLLAMA_HOST"))
    try:
        with urlopen(f"{base_url}/api/version", timeout=1.5) as response:
            import json

            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, RuntimeError, ValueError):
        return None

    version = payload.get("version")
    return str(version).strip() if version else None


def run_doctor() -> str:
    """Run comprehensive health checks and return a diagnostic report.

    Returns:
        Formatted diagnostic report string.
    """
    checks: list[tuple[str, str, str]] = []  # (name, status, detail)

    # 1. Python version
    py_version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    if sys.version_info >= (3, 11):
        checks.append(("Python Version", "OK", py_version))
    else:
        checks.append(("Python Version", "WARN", f"{py_version} (3.11+ recommended)"))

    # 2. Platform info
    checks.append(
        (
            "Platform",
            "INFO",
            f"{platform.system()} {platform.release()} ({platform.machine()})",
        )
    )

    # 3. Package versions
    for pkg_name in [
        "bog-agents",
        "langchain",
        "langchain-core",
        "langgraph",
        "textual",
    ]:
        try:
            dist = importlib.metadata.distribution(pkg_name)
            checks.append((f"Package: {pkg_name}", "OK", dist.version))
        except importlib.metadata.PackageNotFoundError:
            checks.append((f"Package: {pkg_name}", "MISSING", "Not installed"))

    # 4. Provider packages
    providers = [
        ("langchain-anthropic", "ANTHROPIC_API_KEY"),
        ("langchain-openai", "OPENAI_API_KEY"),
        ("langchain-google-genai", "GOOGLE_API_KEY"),
        ("langchain-ollama", None),
    ]
    for pkg, env_key in providers:
        try:
            dist = importlib.metadata.distribution(pkg)
            if env_key is None:
                status = "OK"
                detail = f"v{dist.version} (local provider)"
            else:
                has_key = bool(os.environ.get(env_key))
                status = "OK" if has_key else "WARN"
                detail = f"v{dist.version}" + (
                    " (API key set)" if has_key else f" ({env_key} not set)"
                )
            checks.append((f"Provider: {pkg}", status, detail))
        except importlib.metadata.PackageNotFoundError:
            checks.append((f"Provider: {pkg}", "SKIP", "Not installed (optional)"))

    # 5. CLI tools
    for tool, purpose in [
        ("git", "Version control"),
        ("ollama", "Local model runtime"),
        ("rg", "Fast text search (ripgrep)"),
        ("ruff", "Python linter"),
        ("uv", "Package manager"),
        ("node", "Node.js runtime"),
    ]:
        path = shutil.which(tool)
        if path:
            checks.append((f"Tool: {tool}", "OK", f"Found at {path}"))
        else:
            checks.append((f"Tool: {tool}", "WARN", f"Not found ({purpose})"))

    ollama_version = _get_ollama_version()
    if ollama_version:
        checks.append(("Ollama daemon", "OK", f"Reachable (v{ollama_version})"))
    elif shutil.which("ollama"):
        checks.append(
            (
                "Ollama daemon",
                "WARN",
                f"Not reachable at {_normalize_ollama_host(os.environ.get('OLLAMA_HOST'))}",
            )
        )

    # 6. Sandbox support
    system = platform.system().lower()
    if system == "linux":
        if shutil.which("bwrap"):
            checks.append(("Sandbox: bubblewrap", "OK", "Available"))
        else:
            checks.append(
                (
                    "Sandbox: bubblewrap",
                    "WARN",
                    "Not installed (apt install bubblewrap)",
                )
            )
    elif system == "darwin":
        if shutil.which("sandbox-exec"):
            checks.append(("Sandbox: seatbelt", "OK", "Available"))
        else:
            checks.append(("Sandbox: seatbelt", "WARN", "Not available"))

    # 7. Config directory
    config_dir = Path.home() / ".bog-agents"
    if config_dir.exists():
        hooks_file = config_dir / "hooks.json"
        config_file = config_dir / "config.toml"
        checks.append(("Config dir", "OK", str(config_dir)))
        if hooks_file.exists():
            checks.append(("Hooks config", "OK", str(hooks_file)))
        if config_file.exists():
            checks.append(("Config file", "OK", str(config_file)))
    else:
        checks.append(
            ("Config dir", "INFO", f"{config_dir} (will be created on first use)")
        )

    # 8. Environment variables
    env_checks = [
        "LANGSMITH_API_KEY",
        "LANGSMITH_PROJECT",
        "BOG_AGENTS_LANGSMITH_PROJECT",
    ]
    for env_var in env_checks:
        value = os.environ.get(env_var)
        if value:
            # Mask the value
            masked = value[:4] + "..." + value[-4:] if len(value) > 8 else "***"
            checks.append((f"Env: {env_var}", "OK", masked))
        else:
            checks.append((f"Env: {env_var}", "SKIP", "Not set"))

    # 9. MCP config
    mcp_config = Path.cwd() / ".mcp.json"
    if mcp_config.exists():
        checks.append(("MCP config", "OK", str(mcp_config)))
    else:
        checks.append(("MCP config", "SKIP", "No .mcp.json in current directory"))

    # Format output
    lines = ["## Bog Agents Health Check\n"]

    ok_count = sum(1 for _, s, _ in checks if s == "OK")
    warn_count = sum(1 for _, s, _ in checks if s == "WARN")
    missing_count = sum(1 for _, s, _ in checks if s == "MISSING")

    for name, status, detail in checks:
        icon = {
            "OK": "  OK ",
            "WARN": "WARN",
            "MISSING": "FAIL",
            "INFO": "INFO",
            "SKIP": "SKIP",
        }.get(status, "????")
        lines.append(f"[{icon}] {name}: {detail}")

    lines.append("")
    lines.append(
        f"Summary: {ok_count} OK, {warn_count} warnings, {missing_count} missing"
    )

    if missing_count > 0:
        lines.append(
            "\nAction required: Install missing packages to resolve FAIL items."
        )
    elif warn_count > 0:
        lines.append(
            "\nSome optional components are missing. The CLI will work but with reduced functionality."
        )
    else:
        lines.append("\nAll checks passed! Your environment is fully configured.")

    return "\n".join(lines)
