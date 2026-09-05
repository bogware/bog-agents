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


# Models that have been verified end-to-end with the bog-agents tool-call
# stack on Ollama. Add to this list as new candidates are validated.
_OLLAMA_KNOWN_GOOD = (
    "gpt-oss",
    # `mistral-nemo`, `hermes3`, and `qwen2.5-coder` partially work via the
    # ToolCallParserMiddleware text-format recovery; not on the known-good
    # list because recovery is best-effort and depends on prompt shape.
)


def _is_known_good_ollama_model(model: str) -> bool:
    """Return True if `model` is on the validated Ollama tool-call list."""
    name = model.lower()
    return any(prefix in name for prefix in _OLLAMA_KNOWN_GOOD)


def _read_default_ollama_model() -> str | None:
    """Read the user's configured default model and return it iff it's Ollama.

    Returns the bare model identifier (without the ``ollama:`` prefix), or
    ``None`` if no default is set or the default isn't an Ollama model.
    """
    try:
        from bog_agents_cli.model_config import ModelConfig
    except ImportError:
        return None
    try:
        cfg = ModelConfig.load()
    except (OSError, ValueError):
        return None
    spec = cfg.default_model or cfg.recent_model
    if not spec:
        return None
    spec_lower = spec.lower()
    if spec_lower.startswith("ollama:"):
        return spec.split(":", 1)[1] or None
    return None


_CLI_ENTRYPOINTS = ("bog-agents", "bog-agents-cli")
"""Console-script names installed by `bog-agents-cli` (see pyproject scripts)."""


def _entrypoints_on_path(name: str) -> list[str]:
    """Return every resolved path where console script `name` resolves on PATH.

    A uv/pip/pipx upgrade can leave a stale copy of the entrypoint earlier on
    PATH than the freshly upgraded one — so `name` runs the old build while the
    installed package is new. Enumerating *all* matches (not just
    `shutil.which`'s first hit) lets doctor flag that shadowing.

    Args:
        name: Console-script base name (without a platform extension).

    Returns:
        Distinct resolved executable paths in PATH order.
    """
    seen: list[str] = []
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    # On Windows the same logical entrypoint exists as name.exe / name.cmd /
    # name (shim); PATHEXT drives which is executable. Probe each candidate.
    exts = [""]
    if platform.system() == "Windows":
        exts = [*os.environ.get("PATHEXT", ".EXE;.CMD;.BAT").split(os.pathsep), ""]
    for directory in path_dirs:
        if not directory:
            continue
        for ext in exts:
            candidate = Path(directory) / f"{name}{ext}"
            try:
                if candidate.is_file():
                    resolved = str(candidate.resolve())
                    if resolved not in seen:
                        seen.append(resolved)
            except OSError:
                continue
    return seen


def _shadowed_entrypoint_check() -> tuple[str, str] | None:
    """Detect a CLI entrypoint shadowed by a second install on PATH.

    Returns a `(status, detail)` row when at least one of the CLI's console
    scripts resolves to more than one distinct location on PATH (the classic
    "old copy earlier on PATH after an upgrade" trap), or `None` when nothing
    is shadowed and there is nothing worth reporting.

    Returns:
        A `(status, detail)` tuple, or `None` when no shadowing is detected.
    """
    for name in _CLI_ENTRYPOINTS:
        matches = _entrypoints_on_path(name)
        if len(matches) > 1:
            active, *shadowed = matches
            try:
                from bog_agents_cli.update_manager import detect_install_method

                method = detect_install_method().value
            except Exception:  # diagnostic must not crash
                method = "unknown"
            return (
                "WARN",
                f"`{name}` resolves to {len(matches)} installs on PATH — active "
                f"{active} shadows {', '.join(shadowed)} (install method: "
                f"{method}). Remove the stale copy or reorder PATH.",
            )
    return None


def _mcp_oauth_signed_in_count() -> tuple[int, int]:
    """Count remote MCP servers and how many have a live OAuth token.

    Walks the discovered MCP configs for remote (`http`/`sse`) servers and
    asks `mcp_login_controller.status` whether each has a stored, unexpired
    token. Purely informational — never raises to the caller.

    Returns:
        A `(signed_in, total_remote)` tuple. `(0, 0)` when discovery fails or
        there are no remote servers.
    """
    try:
        from bog_agents_cli.mcp_login_controller import status as oauth_status
        from bog_agents_cli.mcp_tools import (
            _resolve_server_type,
            discover_mcp_configs,
            load_mcp_config_lenient,
        )
    except Exception:
        return 0, 0

    remote_names: set[str] = set()
    try:
        for config_path in discover_mcp_configs():
            config = load_mcp_config_lenient(config_path)
            if not config:
                continue
            for server_name, server_config in config.get("mcpServers", {}).items():
                if not isinstance(server_config, dict):
                    continue
                if _resolve_server_type(server_config) in {"http", "sse"}:
                    remote_names.add(server_name)
    except Exception:
        return 0, 0

    signed_in = 0
    for server_name in remote_names:
        try:
            info = oauth_status(server_name)
        except Exception:
            logger.debug("OAuth status probe failed for %s", server_name, exc_info=True)
            continue
        if info.get("has_token") and not info.get("expired"):
            signed_in += 1
    return signed_in, len(remote_names)


def _bedrock_credential_status() -> tuple[str, str]:
    """Probe AWS credentials for the Bedrock provider and report their state.

    Walks boto3's standard credential chain (env, profile, SSO, instance
    role) without making a network call. Catches the common failure modes
    a Bedrock user hits at the CLI:

    - boto3 not installed (transitive dep of langchain-aws — should never
      happen but reported gracefully).
    - No credentials anywhere — points the user to ``aws configure``.
    - SSO session expired (TokenRetrievalError) — points the user to
      ``aws sso login``.
    - Credentials present but access_key empty — points at the profile.

    Returns:
        (status, detail) tuple where status is "OK"/"WARN"/"FAIL" and
        detail is a human-readable one-liner suitable for the doctor table.
    """
    try:
        import boto3  # type: ignore[import-untyped]
    except ImportError:
        return "WARN", "boto3 not installed (transitive of langchain-aws)"

    # botocore raises TokenRetrievalError when an SSO token has expired.
    # Map it to a clean actionable message instead of a stack trace.
    try:
        from botocore.exceptions import (  # type: ignore[import-untyped]
            NoCredentialsError,
            TokenRetrievalError,
        )
    except ImportError:
        # Older botocore — fall back to generic Exception
        class TokenRetrievalError(Exception):  # type: ignore[no-redef]
            pass

        class NoCredentialsError(Exception):  # type: ignore[no-redef]
            pass

    # If the runtime credential probe already saw a failure this session,
    # reuse its classification instead of triggering yet another expensive
    # boto3 SSO refresh attempt (which would log another full traceback —
    # issue #53). The cache only stores negative results; on success the
    # runtime probe clears it so this branch falls through.
    try:
        from bog_agents_cli.model_config import _BEDROCK_PROBE_CACHE

        if _BEDROCK_PROBE_CACHE:
            for kind, (_ok, detail) in _BEDROCK_PROBE_CACHE.items():
                if "sso-expired" in kind:
                    return (
                        "FAIL",
                        f"AWS SSO token expired — run `aws sso login` ({detail})",
                    )
                if "no-credentials" in kind:
                    return "WARN", "no AWS credentials found — run `aws configure`"
                return "FAIL", f"AWS credential probe error: {detail}"
    except ImportError:
        pass

    try:
        session = boto3.Session()
        creds = session.get_credentials()
        if creds is None:
            return "WARN", "no AWS credentials found — run `aws configure`"
        # `get_frozen_credentials()` is what triggers SSO token refresh,
        # and where TokenRetrievalError surfaces if the SSO session expired.
        frozen = creds.get_frozen_credentials()
        if not frozen.access_key:
            return "WARN", "AWS credentials resolved but access_key is empty"
        method = getattr(creds, "method", "?")
        return "OK", f"AWS credentials valid (source: {method})"
    except TokenRetrievalError as exc:
        return "FAIL", f"AWS SSO token expired — run `aws sso login` ({exc})"
    except NoCredentialsError:
        return "WARN", "no AWS credentials found — run `aws configure`"
    except Exception as exc:
        return "FAIL", f"AWS credential probe error: {type(exc).__name__}: {exc}"


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

    # 3. Package versions — critical runtime deps that the CLI imports
    # eagerly. A `MISSING` result here usually means an incomplete pip
    # install; `pip install --upgrade --force-reinstall bog-agents-cli`
    # is the recovery hint.
    for pkg_name in [
        "bog-agents",
        "langchain",
        "langchain-core",
        "langgraph",
        "langgraph-sdk",
        "langsmith",
        "httpx",
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
        # Bedrock uses AWS credentials, not a single API key — handled below.
        ("langchain-aws", "__BEDROCK__"),
    ]
    for pkg, env_key in providers:
        try:
            dist = importlib.metadata.distribution(pkg)
            if env_key is None:
                status = "OK"
                detail = f"v{dist.version} (local provider)"
            elif env_key == "__BEDROCK__":
                status, bedrock_detail = _bedrock_credential_status()
                # Surface the resolved region alongside credential state so
                # users immediately see whether AWS_DEFAULT_REGION (or
                # `[models.providers.bedrock] region`) is set up.
                try:
                    from bog_agents_cli.model_config import resolve_aws_region

                    region = resolve_aws_region(fallback=None)
                except Exception:  # best-effort doctor field
                    region = None
                region_part = f"region={region}" if region else "region=UNRESOLVED"
                detail = f"v{dist.version} ({bedrock_detail}; {region_part})"
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
        ("ruff", "Python linter"),
        ("uv", "Package manager"),
        ("node", "Node.js runtime"),
    ]:
        path = shutil.which(tool)
        if path:
            checks.append((f"Tool: {tool}", "OK", f"Found at {path}"))
        else:
            checks.append((f"Tool: {tool}", "WARN", f"Not found ({purpose})"))

    # ripgrep gets a dedicated row so users can see whether the fast search
    # path is served by the checksum-verified managed copy under
    # ~/.bog-agents/bin, a system install on PATH, or is missing entirely.
    try:
        from bog_agents_cli.managed_tools import describe_ripgrep

        rg_status, rg_detail = describe_ripgrep()
    except Exception:  # diagnostic command must not crash
        rg_status, rg_detail = "absent", "ripgrep status could not be determined"
    checks.append(
        (
            "Tool: rg",
            {"managed": "OK", "system": "OK"}.get(rg_status, "WARN"),
            rg_detail,
        )
    )

    # Shadowed-entrypoint check: after a uv/pip/pipx upgrade a stale copy of
    # the `bog-agents` console script can sit earlier on PATH than the new
    # one, so the CLI silently runs the old build. Flag that here.
    try:
        shadow_row = _shadowed_entrypoint_check()
    except Exception:  # diagnostic command must not crash
        shadow_row = None
    if shadow_row is not None:
        checks.append(("CLI entrypoint", shadow_row[0], shadow_row[1]))

    ollama_version = _get_ollama_version()
    if ollama_version:
        checks.append(("Ollama daemon", "OK", f"Reachable (v{ollama_version})"))

        # If the configured default is an Ollama model, hint about which one
        # actually engages tool calling reliably. Most non-OpenAI-trained
        # models emit tool calls in a text format that bog-agents now tries
        # to recover via ToolCallParserMiddleware, but recovery isn't 100%.
        default_model = _read_default_ollama_model()
        if default_model and not _is_known_good_ollama_model(default_model):
            checks.append(
                (
                    "Ollama default model",
                    "WARN",
                    f"'{default_model}' may not engage tool calls cleanly. "
                    "Recommended: 'gpt-oss:20b' (OpenAI tool-call format). "
                    "ToolCallParserMiddleware will try to recover Mistral/"
                    "Hermes/qwen text-shaped tool calls automatically.",
                )
            )
    elif shutil.which("ollama"):
        checks.append(
            (
                "Ollama daemon",
                "WARN",
                f"Not reachable at {_normalize_ollama_host(os.environ.get('OLLAMA_HOST'))}",
            )
        )
    else:
        checks.append(("Ollama daemon", "SKIP", "ollama not installed"))

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
    elif system == "windows":
        # ROADMAP #61: the Store's zero-byte pwsh.exe alias is the classic
        # first-run trap (WinError 5); say so before the agent hits it.
        from bog_agents.tools.powershell import find_powershell, is_windows_apps_alias

        found = find_powershell()
        if found:
            checks.append(("PowerShell", "OK", found))
        else:
            alias = shutil.which("pwsh")
            if alias and is_windows_apps_alias(alias):
                checks.append(
                    (
                        "PowerShell",
                        "WARN",
                        f"pwsh is the Store execution alias ({alias}); install PowerShell 7 or disable the alias",
                    )
                )
            else:
                checks.append(("PowerShell", "WARN", "pwsh/powershell not on PATH"))

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

    # 9. MCP config — discover ALL standard locations, not just cwd.
    # The previous version only checked ``Path.cwd() / ".mcp.json"`` and
    # reported SKIP when the user's actual config lived under
    # ``~/.bog-agents/.mcp.json`` (the default location written by
    # ``/mcp add``) or ``<project>/.bog-agents/.mcp.json``. Use the
    # same discovery the server uses so doctor and runtime stay in sync.
    discovered_configs: list[Path] = []
    try:
        from bog_agents_cli.mcp_tools import discover_mcp_configs

        discovered_configs = discover_mcp_configs()
    except Exception as e:  # diagnostic command must not crash
        checks.append(
            ("MCP config", "WARN", f"Discovery failed: {type(e).__name__}: {e}")
        )

    if discovered_configs:
        # ``discover_mcp_configs`` returns lowest-to-highest precedence.
        # Show all of them so the user can confirm which files actually
        # contribute. First entry gets the OK row; the rest are INFO.
        checks.append(("MCP config", "OK", str(discovered_configs[0])))
        for extra in discovered_configs[1:]:
            checks.append(("MCP config (also)", "INFO", str(extra)))
    elif not any(name == "MCP config" for name, _, _ in checks):
        checks.append(
            (
                "MCP config",
                "SKIP",
                "No .mcp.json found in standard locations "
                "(~/.bog-agents/.mcp.json, <project>/.bog-agents/.mcp.json, "
                "or <project>/.mcp.json)",
            )
        )

    # 9b. MCP trust state — only evaluated for project-level configs (the
    # ones that need stdio approval). User-level configs at
    # ~/.bog-agents/.mcp.json are always trusted by design.
    user_root = (Path.home() / ".bog-agents").resolve()

    def _is_user_level(p: Path) -> bool:
        try:
            return p.resolve().is_relative_to(user_root)
        except (OSError, ValueError):
            return False

    project_configs = [p for p in discovered_configs if not _is_user_level(p)]
    if project_configs:
        try:
            from bog_agents_cli.mcp_trust import (
                compute_config_fingerprint,
                is_project_mcp_trusted,
            )

            project_root = str(Path.cwd().resolve())
            fingerprint = compute_config_fingerprint(project_configs)
            if is_project_mcp_trusted(project_root, fingerprint):
                checks.append(("MCP trust", "OK", "Trusted (fingerprint matches)"))
            else:
                checks.append(
                    (
                        "MCP trust",
                        "WARN",
                        "Untrusted — stdio servers will require approval on launch",
                    )
                )
        except Exception as e:  # diagnostic command must not crash
            checks.append(("MCP trust", "WARN", f"Could not evaluate trust: {e}"))

    # 9c. MCP OAuth — informational count of how many remote (http/sse) servers
    # currently hold a live signed-in token. Helps a user confirm `/mcp login`
    # actually persisted a token before a session starts.
    try:
        signed_in, total_remote = _mcp_oauth_signed_in_count()
    except Exception:  # diagnostic command must not crash
        signed_in, total_remote = 0, 0
    if total_remote:
        server_word = "server" if total_remote == 1 else "servers"
        checks.append(
            (
                "MCP OAuth",
                "INFO",
                f"{signed_in}/{total_remote} remote {server_word} signed in "
                "(/mcp login <name> to authenticate)",
            )
        )

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
