"""Code explanation helper for the bog-agents CLI.

Gathers context around a symbol, file, or concept and builds a structured
prompt for the /explain slash command.
"""

from __future__ import annotations

import logging
import shutil
import subprocess  # noqa: S404
from pathlib import Path

logger = logging.getLogger(__name__)

# Extensions treated as source code files
_SOURCE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".rs",
    ".java", ".kt", ".c", ".cpp", ".h", ".hpp", ".rb", ".swift",
}

# Number of context lines to read around a matched definition
_CONTEXT_LINES = 20

# Maximum number of call-site examples to collect
_MAX_CALLERS = 5


def _grep_tool() -> str:
    """Return 'rg' if ripgrep is available, otherwise 'grep'.

    Returns:
        Command name string.
    """
    return "rg" if shutil.which("rg") else "grep"


def _looks_like_file(target: str) -> bool:
    """Return True if target looks like a file path rather than a symbol name.

    Args:
        target: The raw /explain argument.

    Returns:
        True when the target contains a path separator or has a known source
        file extension.
    """
    if "/" in target or "\\" in target:
        return True
    p = Path(target)
    return p.suffix in _SOURCE_EXTENSIONS


def _read_lines(path: Path, start: int, end: int) -> str:
    """Read a slice of a file by 1-based line numbers.

    Args:
        path: File to read.
        start: First line to include (1-based, inclusive).
        end: Last line to include (1-based, inclusive).

    Returns:
        The requested lines joined by newlines, or empty string on error.
    """
    try:
        all_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    start_idx = max(0, start - 1)
    end_idx = min(len(all_lines), end)
    return "\n".join(all_lines[start_idx:end_idx])


def _find_definition(symbol: str, cwd: Path) -> tuple[Path, int] | None:
    """Search for the first definition of `symbol` in the working tree.

    Uses ripgrep or grep to locate lines matching common definition patterns
    (def, class, function, const, type, interface, fn, func).

    Args:
        symbol: Symbol name to search for.
        cwd: Root directory to search within.

    Returns:
        Tuple of (file_path, 1-based line number) for the first match, or
        None if nothing is found.
    """
    tool = _grep_tool()
    # Build a pattern that matches common definition keywords
    pattern = (
        rf"^\s*(def {symbol}|class {symbol}|function {symbol}|"
        rf"const {symbol}|type {symbol}|interface {symbol}|"
        rf"fn {symbol}|func {symbol})[(\s{{]"
    )

    if tool == "rg":
        cmd = ["rg", "--line-number", "--no-heading", "-m", "1", pattern, str(cwd)]
    else:
        cmd = ["grep", "-rn", "--include=*.*", "-m", "1", "-E", pattern, str(cwd)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15)  # noqa: S603
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    for line in result.stdout.splitlines():
        # rg/grep output format: <file>:<lineno>:<content>
        parts = line.split(":", 2)
        if len(parts) >= 2:
            try:
                file_path = Path(parts[0])
                lineno = int(parts[1])
                if file_path.exists():
                    return file_path, lineno
            except (ValueError, IndexError):
                continue

    return None


def _find_callers(symbol: str, cwd: Path, exclude_file: Path | None = None) -> list[str]:
    """Find call sites that reference `symbol` in the working tree.

    Args:
        symbol: Symbol name to search for.
        cwd: Root directory to search within.
        exclude_file: Optional file to exclude (typically the definition file).

    Returns:
        List of formatted strings like "path/to/file.py:42: calling_line_text",
        up to `_MAX_CALLERS` entries.
    """
    tool = _grep_tool()

    if tool == "rg":
        cmd = ["rg", "--line-number", "--no-heading", symbol, str(cwd)]
    else:
        cmd = ["grep", "-rn", "--include=*.*", symbol, str(cwd)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15)  # noqa: S603
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    callers: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        file_str, lineno_str, content = parts[0], parts[1], parts[2]
        # Skip the definition file itself
        if exclude_file and Path(file_str).resolve() == exclude_file.resolve():
            continue
        # Skip definition lines (already shown as content)
        if any(kw in content for kw in ("def ", "class ", "function ", "fn ", "func ")):
            continue
        callers.append(f"{file_str}:{lineno_str}: {content.strip()}")
        if len(callers) >= _MAX_CALLERS:
            break

    return callers


def gather_explain_context(target: str, cwd: Path) -> dict[str, str]:
    """Find and gather context for explaining a symbol, file, or concept.

    When `target` looks like a file path the entire file is read. Otherwise
    a grep search locates the definition and surrounding lines, and call sites
    are collected separately.

    Args:
        target: What to explain — a file path, function name, class name, or concept.
        cwd: Working directory used to resolve relative paths and run searches.

    Returns:
        Dict with keys:
            'type': 'file' | 'symbol' | 'unknown'
            'content': Source code text (file contents or context window).
            'location': String like "path/to/file.py:42".
            'imports': Relevant import lines extracted from the same file.
            'callers': Formatted call-site lines, newline-separated.
    """
    ctx: dict[str, str] = {
        "type": "unknown",
        "content": "",
        "location": "",
        "imports": "",
        "callers": "",
    }

    if _looks_like_file(target):
        file_path = Path(target) if Path(target).is_absolute() else cwd / target
        try:
            content = file_path.read_text(encoding="utf-8")
            ctx["type"] = "file"
            ctx["content"] = content
            ctx["location"] = str(file_path)
            # Collect import lines
            import_lines = [ln for ln in content.splitlines() if ln.startswith(("import ", "from ", "require(", "use "))]
            ctx["imports"] = "\n".join(import_lines[:20])
        except OSError as exc:
            logger.debug("Could not read file %s: %s", file_path, exc)
        return ctx

    # Symbol search
    definition = _find_definition(target, cwd)
    if definition is None:
        return ctx

    def_file, def_line = definition
    ctx["type"] = "symbol"
    ctx["location"] = f"{def_file}:{def_line}"

    # Read ±_CONTEXT_LINES around the definition
    start = max(1, def_line - _CONTEXT_LINES)
    end = def_line + _CONTEXT_LINES
    ctx["content"] = _read_lines(def_file, start, end)

    # Extract import block from the top of the definition file
    try:
        all_lines = def_file.read_text(encoding="utf-8").splitlines()
        import_lines = [ln for ln in all_lines[:50] if ln.startswith(("import ", "from ", "require(", "use "))]
        ctx["imports"] = "\n".join(import_lines)
    except OSError:
        pass

    # Collect call sites
    callers = _find_callers(target, cwd, exclude_file=def_file)
    ctx["callers"] = "\n".join(callers)

    return ctx


def build_explain_prompt(target: str, context: dict[str, str]) -> str:
    """Build a rich explanation prompt for the LLM.

    Produces a structured prompt that directs the agent to explain the target
    covering purpose, design rationale, implementation details, edge cases,
    and usage examples.

    Args:
        target: The symbol, file, or concept the user wants explained.
        context: Context dict as returned by `gather_explain_context`.

    Returns:
        Structured prompt string ready to be sent to the agent.
    """
    parts: list[str] = [
        f"Please explain `{target}` thoroughly for a developer who is new to this codebase.",
        "",
        "Cover all of the following:",
        "1. **What it does** — the high-level purpose and responsibility.",
        "2. **Why it exists** — the design rationale or problem it solves.",
        "3. **How it works** — key implementation steps or data flow.",
        "4. **Edge cases & gotchas** — anything non-obvious or surprising.",
        "5. **Example usage** — a short, concrete example (real or illustrative).",
        "",
    ]

    location = context.get("location", "")
    if location:
        parts.append(f"Location: `{location}`")
        parts.append("")

    content = context.get("content", "")
    if content:
        parts.append("Relevant source code:")
        parts.append("```")
        parts.append(content.rstrip())
        parts.append("```")
        parts.append("")

    imports = context.get("imports", "")
    if imports:
        parts.append("Imports / dependencies in the same file:")
        parts.append("```")
        parts.append(imports.rstrip())
        parts.append("```")
        parts.append("")

    callers = context.get("callers", "")
    if callers:
        parts.append("Call sites (where it is used):")
        parts.append("```")
        parts.append(callers.rstrip())
        parts.append("```")
        parts.append("")

    return "\n".join(parts)


def format_explain_not_found(target: str) -> str:
    """Return a helpful 'not found' message when a symbol cannot be located.

    Args:
        target: The symbol or path that was searched for.

    Returns:
        Rich-formatted string with suggestions.
    """
    return (
        f"[yellow]Could not locate[/yellow] [bold]{target!r}[/bold] in the working directory.\n\n"
        "Suggestions:\n"
        "  • Check spelling — symbol names are case-sensitive.\n"
        "  • Use a relative file path if explaining a whole file, e.g. [dim]src/agent.py[/dim].\n"
        "  • Make sure you are in the correct project directory.\n"
        "  • For JavaScript/TypeScript symbols try the full export name."
    )


def format_explain_help() -> str:
    """Return usage help for /explain command.

    Returns:
        Multi-line help string describing /explain usage and examples.
    """
    return (
        "[bold]/explain[/bold] — Explain a symbol, file, or concept\n\n"
        "Usage:\n"
        "  /explain <symbol>        Explain a function, class, or variable\n"
        "  /explain <file>          Explain an entire source file\n"
        "  /explain <concept>       Ask the agent to explain a concept in context\n\n"
        "Examples:\n"
        "  /explain create_agent\n"
        "  /explain libs/bog-agents/bog_agents/middleware/git.py\n"
        "  /explain AgentMiddleware\n"
        "  /explain checkpointing"
    )
