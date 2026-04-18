"""@-mentions context injection system for bog-agents CLI.

Parses @-mention tokens from user messages and resolves them to their content,
which is injected into the agent context before the message is processed.

Supported mention types::

    @file:path/to/file.py      — inject file contents
    @folder:src/               — inject directory listing
    @symbol:MyClass            — inject symbol definition from repo map
    @url:https://example.com   — fetch and inject webpage as markdown
    @memory:key-name           — inject a specific memory entry
    @skill:skill-name          — inject a skill definition

Legacy bare mentions (backward compatible)::

    @path/to/file.py           — equivalent to @file:path/to/file.py

Usage::

    from bog_agents_cli.mentions import resolve_mentions

    augmented_text, injected = resolve_mentions(
        "@file:src/auth.py can you explain this?",
        cwd=Path("/my/project"),
    )
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token parsing
# ---------------------------------------------------------------------------

# Matches @type:value or bare @path (legacy)
_MENTION_RE = re.compile(
    r"@(?:"
    r"(file|folder|symbol|url|memory|skill):([^\s]+)"  # typed mention: @type:value
    r"|"
    r"([^\s@:]+(?:/[^\s@:]+)*)"                       # bare path: @src/main.py
    r")",
    re.IGNORECASE,
)

_MAX_FILE_BYTES = 100_000    # 100KB per file
_MAX_URL_BYTES = 50_000      # 50KB per URL
_URL_TIMEOUT = 8             # seconds


@dataclass
class MentionToken:
    """A parsed @-mention token."""

    kind: str          # "file" | "folder" | "symbol" | "url" | "memory" | "skill"
    value: str         # the argument after the colon (or bare path)
    raw: str           # original text in the message (for replacement)
    resolved: str = ""  # resolved content (filled by resolve())
    error: str = ""    # error message if resolution failed


@dataclass
class MentionResolution:
    """Result of resolving all @-mentions in a message."""

    original: str
    augmented: str                      # message with injected context prepended
    tokens: list[MentionToken] = field(default_factory=list)
    context_blocks: list[str] = field(default_factory=list)  # injected text blocks


def parse_mentions(text: str) -> list[MentionToken]:
    """Extract all @-mention tokens from *text*.

    Args:
        text: Raw message text from the user.

    Returns:
        List of parsed MentionToken objects.
    """
    tokens: list[MentionToken] = []
    for m in _MENTION_RE.finditer(text):
        if m.group(1) and m.group(2):
            # Typed mention: @type:value
            tokens.append(MentionToken(kind=m.group(1).lower(), value=m.group(2), raw=m.group(0)))
        elif m.group(3):
            # Bare path: @src/something → treat as @file:
            tokens.append(MentionToken(kind="file", value=m.group(3), raw=m.group(0)))
    return tokens


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------


def _resolve_file(value: str, cwd: Path) -> str:
    """Inject file contents."""
    # Try relative to cwd, then absolute
    candidates = [cwd / value, Path(value)]
    for path in candidates:
        if path.is_file():
            try:
                size = path.stat().st_size
                if size > _MAX_FILE_BYTES:
                    return f"[File too large to inject: {size:,} bytes. Max {_MAX_FILE_BYTES:,}.]"
                content = path.read_text(errors="replace")
                lang = _lang_hint(path)
                return f"```{lang}\n# {path.name}\n{content}\n```"
            except OSError as exc:
                return f"[Cannot read {value}: {exc}]"
    return f"[File not found: {value}]"


def _resolve_folder(value: str, cwd: Path) -> str:
    """Inject directory listing."""
    candidates = [cwd / value, Path(value)]
    for path in candidates:
        if path.is_dir():
            try:
                items: list[str] = []
                for child in sorted(path.iterdir()):
                    rel = child.relative_to(cwd) if child.is_relative_to(cwd) else child
                    suffix = "/" if child.is_dir() else ""
                    items.append(f"  {rel}{suffix}")
                if not items:
                    return f"[{value}/ is empty]"
                return f"Directory listing of `{value}/`:\n" + "\n".join(items[:200])
            except OSError as exc:
                return f"[Cannot list {value}: {exc}]"
    return f"[Directory not found: {value}]"


def _resolve_symbol(value: str, cwd: Path) -> str:
    """Inject symbol definition from the repo map."""
    try:
        from bog_agents.middleware.repo_map import RepoMapCache

        cache = RepoMapCache(cwd)
        cache.load()

        results: list[str] = []
        query = value.lower()

        for sym in cache.all_symbols():
            matches: list[str] = []
            for cls in sym.classes:
                if query in cls.lower():
                    matches.append(f"  class {cls}")
            for fn in sym.functions:
                if query in fn.lower():
                    matches.append(f"  def {fn}()")
            for cat, items in sym.other.items():
                for item in items:
                    if query in item.lower():
                        matches.append(f"  {cat} {item}")
            if matches:
                results.append(f"{sym.path}:\n" + "\n".join(matches))

        if not results:
            return (
                f"[Symbol '{value}' not found in repo map. "
                "Run /repomap to rebuild the index, then try again.]"
            )
        return f"Symbol search for `{value}`:\n\n" + "\n\n".join(results[:10])
    except Exception as exc:
        return f"[Symbol lookup failed: {exc}]"


def _resolve_url(value: str) -> str:
    """Fetch a URL and return as markdown text."""
    import urllib.error
    import urllib.request

    if not value.startswith(("http://", "https://")):
        return f"[Invalid URL: {value}. Must start with http:// or https://]"

    try:
        req = urllib.request.Request(
            value,
            headers={
                "User-Agent": "bog-agents-cli/mentions",
                "Accept": "text/html,text/plain",
            },
        )
        with urllib.request.urlopen(req, timeout=_URL_TIMEOUT) as resp:
            raw = resp.read(_MAX_URL_BYTES).decode("utf-8", errors="replace")
            content_type = resp.headers.get("Content-Type", "")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return f"[Cannot fetch {value}: {exc}]"

    # Convert HTML to plain text if needed
    if "text/html" in content_type:
        try:
            from html.parser import HTMLParser

            class _TextExtractor(HTMLParser):
                def __init__(self) -> None:
                    """Initialize extractor."""
                    super().__init__()
                    self._texts: list[str] = []
                    self._skip = False

                def handle_starttag(self, tag: str, attrs: object) -> None:  # noqa: ARG002
                    if tag in ("script", "style", "nav", "footer", "header"):
                        self._skip = True

                def handle_endtag(self, tag: str) -> None:
                    if tag in ("script", "style", "nav", "footer", "header"):
                        self._skip = False

                def handle_data(self, data: str) -> None:
                    if not self._skip:
                        stripped = data.strip()
                        if stripped:
                            self._texts.append(stripped)

                def get_text(self) -> str:
                    return " ".join(self._texts)

            extractor = _TextExtractor()
            extractor.feed(raw)
            raw = extractor.get_text()
        except Exception:
            logger.debug("HTML text extraction failed", exc_info=True)

    return f"Content of <{value}>:\n\n{raw.strip()}"


def _resolve_memory(value: str, cwd: Path) -> str:
    """Inject a named memory entry."""
    # Check project memory (.bog-agents.md) and global memory (~/.bog-agents/memory.md)
    search_files = [
        cwd / ".bog-agents.md",
        Path.home() / ".bog-agents" / "memory.md",
    ]
    query = value.lower().replace("-", " ").replace("_", " ")

    for mem_file in search_files:
        if not mem_file.exists():
            continue
        try:
            content = mem_file.read_text(encoding="utf-8", errors="replace")
            # Search for a heading or section matching the query
            lines = content.splitlines()
            in_section = False
            section_lines: list[str] = []
            for line in lines:
                stripped = line.strip().lstrip("#").strip().lower()
                if any(q in stripped for q in [query, value.lower()]):
                    in_section = True
                    section_lines = [line]
                elif in_section:
                    if line.startswith("#") and section_lines:
                        break
                    section_lines.append(line)

            if section_lines:
                return f"Memory `{value}` (from {mem_file.name}):\n\n" + "\n".join(section_lines)

        except OSError:
            continue

    return f"[Memory entry '{value}' not found. Add it with /remember {value}]"


def _resolve_skill(value: str, cwd: Path) -> str:
    """Inject a skill definition."""
    # Skills are stored in ~/.bog-agents/skills/ or .bog-agents/skills/
    search_dirs = [
        Path.home() / ".bog-agents" / "skills",
        cwd / ".bog-agents" / "skills",
    ]
    for skills_dir in search_dirs:
        if not skills_dir.is_dir():
            continue
        for suffix in (".md", ".txt", ".py"):
            skill_file = skills_dir / f"{value}{suffix}"
            if skill_file.is_file():
                try:
                    content = skill_file.read_text(encoding="utf-8", errors="replace")
                    return f"Skill `{value}`:\n\n{content}"
                except OSError:
                    continue

    return f"[Skill '{value}' not found in ~/.bog-agents/skills/ or .bog-agents/skills/]"


def _lang_hint(path: Path) -> str:
    """Return a markdown language hint for a file."""
    ext_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".tsx": "tsx", ".jsx": "jsx", ".rs": "rust", ".go": "go",
        ".java": "java", ".rb": "ruby", ".php": "php", ".swift": "swift",
        ".kt": "kotlin", ".cs": "csharp", ".cpp": "cpp", ".c": "c",
        ".h": "c", ".sh": "bash", ".yaml": "yaml", ".yml": "yaml",
        ".json": "json", ".toml": "toml", ".md": "markdown",
        ".html": "html", ".css": "css", ".sql": "sql", ".tf": "hcl",
    }
    return ext_map.get(path.suffix.lower(), "")


# ---------------------------------------------------------------------------
# Main resolution function
# ---------------------------------------------------------------------------


def resolve_mentions(
    text: str,
    *,
    cwd: Path | None = None,
) -> MentionResolution:
    """Parse and resolve all @-mentions in *text*.

    Each mention is resolved to its content and prepended to the message as
    a context block. The original @-mention tokens are left in the message
    text so the model can see what was referenced.

    Args:
        text: Raw message text from the user.
        cwd: Working directory for relative path resolution.

    Returns:
        MentionResolution with the augmented message and resolved tokens.
    """
    root = cwd or Path.cwd()
    tokens = parse_mentions(text)

    if not tokens:
        return MentionResolution(original=text, augmented=text, tokens=[])

    context_blocks: list[str] = []

    for token in tokens:
        try:
            match token.kind:
                case "file":
                    token.resolved = _resolve_file(token.value, root)
                case "folder":
                    token.resolved = _resolve_folder(token.value, root)
                case "symbol":
                    token.resolved = _resolve_symbol(token.value, root)
                case "url":
                    token.resolved = _resolve_url(token.value)
                case "memory":
                    token.resolved = _resolve_memory(token.value, root)
                case "skill":
                    token.resolved = _resolve_skill(token.value, root)
                case _:
                    token.error = f"Unknown mention type: {token.kind}"
        except Exception as exc:
            token.error = str(exc)
            token.resolved = f"[Error resolving {token.raw}: {exc}]"
            logger.warning("Failed to resolve mention %s: %s", token.raw, exc)

        if token.resolved:
            context_blocks.append(f"<!-- {token.raw} -->\n{token.resolved}")

    if context_blocks:
        context_preamble = "\n\n---\n\n".join(context_blocks)
        augmented = f"{context_preamble}\n\n---\n\n{text}"
    else:
        augmented = text

    return MentionResolution(
        original=text,
        augmented=augmented,
        tokens=tokens,
        context_blocks=context_blocks,
    )


def has_mentions(text: str) -> bool:
    """Return True if *text* contains any @-mentions.

    Args:
        text: Message text to check.
    """
    return bool(_MENTION_RE.search(text))


def get_mention_summary(resolution: MentionResolution) -> str:
    """Return a brief summary of what was injected.

    Args:
        resolution: Resolved mention result.

    Returns:
        Human-readable summary string (e.g. "Injected: auth.py (3.2KB), README.md").
    """
    if not resolution.tokens:
        return ""

    parts: list[str] = []
    for token in resolution.tokens:
        if token.error:
            parts.append(f"{token.raw} (error)")
        elif token.resolved:
            size = len(token.resolved)
            if size > 1024:
                parts.append(f"{token.raw} ({size // 1024}KB)")
            else:
                parts.append(f"{token.raw} ({size}B)")

    return "Injected: " + ", ".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# Autocomplete suggestions for the input widget
# ---------------------------------------------------------------------------


def get_mention_type_suggestions() -> list[tuple[str, str]]:
    """Return autocomplete suggestions for @-mention type prefixes.

    Returns:
        List of (completion, description) tuples.
    """
    return [
        ("@file:", "Inject file contents"),
        ("@folder:", "Inject directory listing"),
        ("@symbol:", "Inject symbol definition from repo map"),
        ("@url:", "Fetch and inject webpage content"),
        ("@memory:", "Inject a memory entry"),
        ("@skill:", "Inject a skill definition"),
    ]
