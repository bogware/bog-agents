"""Autocomplete system for @ mentions and / commands.

This is a custom implementation that handles trigger-based completion
for slash commands (/) and file mentions (@).
"""

from __future__ import annotations

import shutil

# S404: subprocess is required for git ls-files to get project file list
import subprocess  # noqa: S404
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol

from bog_agents_cli.command_registry import get_slash_commands
from bog_agents_cli.project_utils import find_project_root


def _get_git_executable() -> str | None:
    """Get full path to git executable using shutil.which().

    Returns:
        Full path to git executable, or None if not found.
    """
    return shutil.which("git")


if TYPE_CHECKING:
    from textual import events


class CompletionResult(StrEnum):
    """Result of handling a key event in the completion system."""

    IGNORED = "ignored"  # Key not handled, let default behavior proceed
    HANDLED = "handled"  # Key handled, prevent default
    SUBMIT = "submit"  # Key triggers submission (e.g., Enter on slash command)


class CompletionView(Protocol):
    """Protocol for views that can display completion suggestions."""

    def render_completion_suggestions(
        self, suggestions: list[tuple[str, str]], selected_index: int
    ) -> None:
        """Render the completion suggestions popup.

        Args:
            suggestions: List of (label, description) tuples
            selected_index: Index of currently selected item
        """
        ...

    def clear_completion_suggestions(self) -> None:
        """Hide/clear the completion suggestions popup."""
        ...

    def replace_completion_range(self, start: int, end: int, replacement: str) -> None:
        """Replace text in the input from start to end with replacement.

        Args:
            start: Start index in the input text
            end: End index in the input text
            replacement: Text to insert
        """
        ...


class CompletionController(Protocol):
    """Protocol for completion controllers."""

    def can_handle(self, text: str, cursor_index: int) -> bool:
        """Check if this controller can handle the current input state."""
        ...

    def on_text_changed(self, text: str, cursor_index: int) -> None:
        """Called when input text changes."""
        ...

    def on_key(
        self, event: events.Key, text: str, cursor_index: int
    ) -> CompletionResult:
        """Handle a key event. Returns how the event was handled."""
        ...

    def reset(self) -> None:
        """Reset/clear the completion state."""
        ...


# ============================================================================
# Slash Command Completion
# ============================================================================

SLASH_COMMANDS: list[tuple[str, str, str]] = get_slash_commands()
"""Built-in slash commands: (name, description, hidden_keywords).

Hidden keywords are space-separated terms that participate in fuzzy matching
but are never displayed to the user.
"""

MAX_SUGGESTIONS = 15
"""UI cap so the completion popup doesn't get unwieldy."""

_MIN_SLASH_FUZZY_SCORE = 25
"""Minimum score for slash-command fuzzy matches."""

_MIN_DESC_SEARCH_LEN = 2
"""Minimum query length to search command descriptions (avoids single-char noise)."""


def fuzzy_score(query: str, candidate: str) -> int:
    """Compute a fuzzy match score for query against candidate.

    Returns 0 if no match, higher is better.

    Algorithm:

    - Exact prefix match: score 1000 + (100 - len difference)
    - All chars of query appear in candidate in order: score = 500 + consecutive_bonus
    - Each char found: +10; consecutive chars: +20 bonus per consecutive pair
    - Case-insensitive matching

    Args:
        query: The search term typed by the user.
        candidate: The command name or description to score against.

    Returns:
        Integer score; 0 means no match, higher is a better match.
    """
    if not query:
        return 0

    q = query.lower()
    c = candidate.lower()

    # Exact prefix match — highest tier
    if c.startswith(q):
        return 1000 + max(0, 100 - (len(c) - len(q)))

    # Sequential character matching (fuzzy)
    # Check that all chars of query appear in candidate in order
    ci = 0
    qi = 0
    first_match = -1
    while qi < len(q) and ci < len(c):
        if q[qi] == c[ci]:
            if first_match < 0:
                first_match = ci
            qi += 1
        ci += 1

    if qi < len(q):
        # Not all query chars found in order — no match
        return 0

    # All chars matched — compute bonus
    char_score = len(q) * 10

    # Consecutive bonus: scan through candidate tracking consecutive matched positions
    matched_positions: list[int] = []
    ci = 0
    for ch in q:
        while ci < len(c) and c[ci] != ch:
            ci += 1
        if ci < len(c):
            matched_positions.append(ci)
            ci += 1

    consecutive_bonus = 0
    for i in range(1, len(matched_positions)):
        if matched_positions[i] == matched_positions[i - 1] + 1:
            consecutive_bonus += 20

    return 500 + char_score + consecutive_bonus


class SlashCommandController:
    """Controller for / slash command completion."""

    def __init__(
        self,
        commands: list[tuple[str, str, str]],
        view: CompletionView,
    ) -> None:
        """Initialize the slash command controller.

        Args:
            commands: List of `(command, description, hidden_keywords)` tuples.
            view: View to render suggestions to.
        """
        self._commands = commands
        self._view = view
        self._suggestions: list[tuple[str, str]] = []
        self._selected_index = 0

    @staticmethod
    def can_handle(text: str, cursor_index: int) -> bool:  # noqa: ARG004  # Required by AutocompleteProvider interface
        """Handle input that starts with /.

        Returns:
            True if text starts with slash, indicating a command.
        """
        return text.startswith("/")

    def reset(self) -> None:
        """Clear suggestions."""
        if self._suggestions:
            self._suggestions.clear()
            self._selected_index = 0
            self._view.clear_completion_suggestions()

    @staticmethod
    def _score_command(search: str, cmd: str, desc: str, keywords: str = "") -> float:
        """Score a command against a search string. Higher = better match.

        Args:
            search: Lowercase search string (without leading `/`).
            cmd: Command name (e.g. `'/help'`).
            desc: Command description text.
            keywords: Space-separated hidden keywords for matching.

        Returns:
            Score value where higher indicates better match quality.
        """
        if not search:
            return 0.0
        name = cmd.lstrip("/").lower()
        lower_desc = desc.lower()
        # Prefix match on command name — highest priority
        if name.startswith(search):
            return 200.0
        # Substring match on command name
        if search in name:
            return 150.0
        # Hidden keyword match — treated like a word-boundary description match
        if keywords and len(search) >= _MIN_DESC_SEARCH_LEN:
            for kw in keywords.lower().split():
                if kw.startswith(search) or search in kw:
                    return 120.0
        # Substring match on description (require ≥2 chars to avoid single-letter noise)
        if len(search) >= _MIN_DESC_SEARCH_LEN and search in lower_desc:
            idx = lower_desc.find(search)
            # Word-boundary bonus: match at start of description or after a space
            if idx == 0 or lower_desc[idx - 1] == " ":
                return 110.0
            return 90.0
        # Fuzzy match via SequenceMatcher on name + desc
        name_ratio = SequenceMatcher(None, search, name).ratio()
        desc_ratio = SequenceMatcher(None, search, lower_desc).ratio()
        best = max(name_ratio * 60, desc_ratio * 30)
        return best if best >= _MIN_SLASH_FUZZY_SCORE else 0.0

    def on_text_changed(self, text: str, cursor_index: int) -> None:
        """Update suggestions when text changes."""
        if cursor_index < 0 or cursor_index > len(text):
            self.reset()
            return

        if not self.can_handle(text, cursor_index):
            self.reset()
            return

        # Get the search string (text after /)
        search = text[1:cursor_index].lower()

        if not search:
            # No search text — show all commands (display only cmd + desc)
            suggestions = [(cmd, desc) for cmd, desc, _ in self._commands][
                :MAX_SUGGESTIONS
            ]
        else:
            # Score and filter commands using fuzzy matching.
            # Use _score_command as primary scorer; fall back to fuzzy_score
            # (scaled to keep it below the existing tier thresholds) for
            # candidates that _score_command rates at 0.
            all_scored: list[tuple[float, str, str]] = []
            for cmd, desc, kw in self._commands:
                primary = self._score_command(search, cmd, desc, kw)
                if primary > 0:
                    all_scored.append((primary, cmd, desc))
                else:
                    # fuzzy_score operates on the bare command name (without /)
                    fs = fuzzy_score(search, cmd.lstrip("/"))
                    if fs > 0:
                        # Scale into the fuzzy tier (below 25, above 0)
                        scaled = min(fs / 100.0, _MIN_SLASH_FUZZY_SCORE - 1)
                        all_scored.append((scaled, cmd, desc))
            all_scored.sort(key=lambda x: -x[0])
            suggestions = [(cmd, desc) for _, cmd, desc in all_scored[:MAX_SUGGESTIONS]]

        if suggestions:
            self._suggestions = suggestions
            self._selected_index = 0
            self._view.render_completion_suggestions(
                self._suggestions, self._selected_index
            )
        else:
            self.reset()

    def on_key(
        self, event: events.Key, _text: str, cursor_index: int
    ) -> CompletionResult:
        """Handle key events for navigation and selection.

        Returns:
            CompletionResult indicating how the key was handled.
        """
        if not self._suggestions:
            return CompletionResult.IGNORED

        match event.key:
            case "tab":
                if self._apply_selected_completion(cursor_index):
                    return CompletionResult.HANDLED
                return CompletionResult.IGNORED
            case "enter":
                if self._apply_selected_completion(cursor_index):
                    return CompletionResult.SUBMIT
                return CompletionResult.HANDLED
            case "down":
                self._move_selection(1)
                return CompletionResult.HANDLED
            case "up":
                self._move_selection(-1)
                return CompletionResult.HANDLED
            case "escape":
                self.reset()
                return CompletionResult.HANDLED
            case _:
                return CompletionResult.IGNORED

    def _move_selection(self, delta: int) -> None:
        """Move selection up or down."""
        if not self._suggestions:
            return
        count = len(self._suggestions)
        self._selected_index = (self._selected_index + delta) % count
        self._view.render_completion_suggestions(
            self._suggestions, self._selected_index
        )

    def _apply_selected_completion(self, cursor_index: int) -> bool:
        """Apply the currently selected completion.

        Returns:
            True if completion was applied, False if no suggestions.
        """
        if not self._suggestions:
            return False

        command, _ = self._suggestions[self._selected_index]
        # Replace from start to cursor with the command
        self._view.replace_completion_range(0, cursor_index, command)
        self.reset()
        return True


# ============================================================================
# Fuzzy File Completion (from project root)
# ============================================================================

# Constants for fuzzy file completion
_MAX_FALLBACK_FILES = 1000
"""Hard cap on files returned by the non-git glob fallback."""

_MIN_FUZZY_SCORE = 15
"""Minimum score to include in file-completion results."""

_MIN_FUZZY_RATIO = 0.4
"""SequenceMatcher threshold for filename-only fuzzy matches."""


def _get_project_files(root: Path) -> list[str]:
    """Get project files using git ls-files or fallback to glob.

    Returns:
        List of relative file paths from project root.
    """
    git_path = _get_git_executable()
    if git_path:
        try:
            # S603: git_path is validated via shutil.which(), args are hardcoded
            result = subprocess.run(  # noqa: S603
                [git_path, "ls-files"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                files = result.stdout.strip().split("\n")
                return [f for f in files if f]  # Filter empty strings
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    # Fallback: simple glob (limited depth to avoid slowness)
    files = []
    try:
        for pattern in ["*", "*/*", "*/*/*", "*/*/*/*"]:
            for p in root.glob(pattern):
                if p.is_file() and not any(part.startswith(".") for part in p.parts):
                    files.append(p.relative_to(root).as_posix())
                if len(files) >= _MAX_FALLBACK_FILES:
                    break
            if len(files) >= _MAX_FALLBACK_FILES:
                break
    except OSError:
        pass
    return files


def _fuzzy_score(query: str, candidate: str) -> float:
    """Score a candidate against query. Higher = better match.

    Returns:
        Score value where higher indicates better match quality.
    """
    query_lower = query.lower()
    # Normalize path separators for cross-platform support
    candidate_normalized = candidate.replace("\\", "/")
    candidate_lower = candidate_normalized.lower()

    # Extract filename for matching (prioritize filename over full path)
    filename = candidate_normalized.rsplit("/", 1)[-1].lower()
    filename_start = candidate_lower.rfind("/") + 1

    # Check filename first (higher priority)
    if query_lower in filename:
        idx = filename.find(query_lower)
        # Bonus for being at start of filename
        if idx == 0:
            return 150 + (1 / len(candidate))
        # Bonus for word boundary in filename
        if idx > 0 and filename[idx - 1] in "_-.":
            return 120 + (1 / len(candidate))
        return 100 + (1 / len(candidate))

    # Check full path
    if query_lower in candidate_lower:
        idx = candidate_lower.find(query_lower)
        # At start of filename
        if idx == filename_start:
            return 80 + (1 / len(candidate))
        # At word boundary in path
        if idx == 0 or candidate[idx - 1] in "/_-.":
            return 60 + (1 / len(candidate))
        return 40 + (1 / len(candidate))

    # Fuzzy match on filename only (more relevant)
    filename_ratio = SequenceMatcher(None, query_lower, filename).ratio()
    if filename_ratio > _MIN_FUZZY_RATIO:
        return filename_ratio * 30

    # Fallback: fuzzy on full path
    ratio = SequenceMatcher(None, query_lower, candidate_lower).ratio()
    return ratio * 15


def _is_dotpath(path: str) -> bool:
    """Check if path contains dotfiles/dotdirs (e.g., .github/...).

    Returns:
        True if path contains hidden directories or files.
    """
    return any(part.startswith(".") for part in path.split("/"))


def _path_depth(path: str) -> int:
    """Get depth of path (number of / separators).

    Returns:
        Number of path separators in the path.
    """
    return path.count("/")


def _fuzzy_search(
    query: str,
    candidates: list[str],
    limit: int = 10,
    *,
    include_dotfiles: bool = False,
) -> list[str]:
    """Return top matches sorted by score.

    Args:
        query: Search query
        candidates: List of file paths to search
        limit: Max results to return
        include_dotfiles: Whether to include dotfiles (default False)

    Returns:
        List of matching file paths sorted by relevance score.
    """
    # Filter dotfiles unless explicitly searching for them
    filtered = (
        candidates
        if include_dotfiles
        else [c for c in candidates if not _is_dotpath(c)]
    )

    if not query:
        # Empty query: show root-level files first, sorted by depth then name
        sorted_files = sorted(filtered, key=lambda p: (_path_depth(p), p.lower()))
        return sorted_files[:limit]

    scored = [
        (score, c)
        for c in filtered
        if (score := _fuzzy_score(query, c)) >= _MIN_FUZZY_SCORE
    ]
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:limit]]


class FuzzyFileController:
    """Controller for @ file completion with fuzzy matching from project root."""

    def __init__(
        self,
        view: CompletionView,
        cwd: Path | None = None,
    ) -> None:
        """Initialize the fuzzy file controller.

        Args:
            view: View to render suggestions to
            cwd: Starting directory to find project root from
        """
        self._view = view
        self._cwd = cwd or Path.cwd()
        self._project_root = find_project_root(self._cwd) or self._cwd
        self._suggestions: list[tuple[str, str]] = []
        self._selected_index = 0
        self._file_cache: list[str] | None = None

    def _get_files(self) -> list[str]:
        """Get cached file list or refresh.

        Returns:
            List of project file paths.
        """
        if self._file_cache is None:
            self._file_cache = _get_project_files(self._project_root)
        return self._file_cache

    def refresh_cache(self) -> None:
        """Force refresh of file cache."""
        self._file_cache = None

    # Typed mention prefixes shown when user types bare @
    _MENTION_TYPES: ClassVar[list[tuple[str, str]]] = [
        ("@file:", "Inject file contents"),
        ("@folder:", "Inject directory listing"),
        ("@repo:", "Inject repo context from workspace.toml"),
        ("@search:", "Hybrid codebase search"),
        ("@symbol:", "Inject symbol from repo map"),
        ("@url:", "Fetch and inject webpage"),
        ("@memory:", "Inject a memory entry"),
        ("@skill:", "Inject a skill definition"),
    ]

    @staticmethod
    def can_handle(text: str, cursor_index: int) -> bool:
        """Handle input that contains @ not followed by space.

        Returns:
            True if cursor is after @ and within a mention context.
        """
        if cursor_index <= 0 or cursor_index > len(text):
            return False

        before_cursor = text[:cursor_index]
        if "@" not in before_cursor:
            return False

        at_index = before_cursor.rfind("@")
        if cursor_index <= at_index:
            return False

        # Fragment from @ to cursor must not contain spaces
        fragment = before_cursor[at_index:cursor_index]
        return bool(fragment) and " " not in fragment

    def reset(self) -> None:
        """Clear suggestions."""
        if self._suggestions:
            self._suggestions.clear()
            self._selected_index = 0
            self._view.clear_completion_suggestions()

    def on_text_changed(self, text: str, cursor_index: int) -> None:
        """Update suggestions when text changes."""
        if not self.can_handle(text, cursor_index):
            self.reset()
            return

        before_cursor = text[:cursor_index]
        at_index = before_cursor.rfind("@")
        search = before_cursor[at_index + 1 :]

        suggestions = self._get_mention_suggestions(search)

        if suggestions:
            self._suggestions = suggestions
            self._selected_index = 0
            self._view.render_completion_suggestions(
                self._suggestions, self._selected_index
            )
        else:
            self.reset()

    def _get_mention_suggestions(self, search: str) -> list[tuple[str, str]]:
        """Get suggestions for the fragment after @.

        Handles three cases:
        - bare ``@`` or ``@partial`` — show mention type prefixes
        - ``@file:query`` — show fuzzy file matches
        - ``@folder:query`` — show fuzzy folder matches

        Returns:
            List of (label, description) tuples.
        """
        # @file:<query> → fuzzy file completions
        if search.startswith("file:"):
            query = search[5:]
            return self._get_file_suggestions(query, prefix="@file:")

        # @folder:<query> → fuzzy folder completions
        if search.startswith("folder:"):
            query = search[7:]
            return self._get_folder_suggestions(query)

        # Bare @ or @<partial-type> — offer the type prefixes
        # Only show if no colon yet (user hasn't chosen a type)
        if ":" not in search:
            matches = [
                (label, desc)
                for label, desc in self._MENTION_TYPES
                if label[1:].startswith(search.lower())  # e.g. "fi" matches "file:"
            ]
            return matches or []

        # Unknown prefix (e.g. @symbol:, @url:, @memory:, @skill:) — no completion
        return []

    def _get_file_suggestions(
        self, query: str, *, prefix: str = "@"
    ) -> list[tuple[str, str]]:
        """Return fuzzy file suggestions.

        Returns:
            List of (label, type_hint) tuples for matching files.
        """
        files = self._get_files()
        include_dots = query.startswith(".")
        matches = _fuzzy_search(
            query, files, limit=MAX_SUGGESTIONS, include_dotfiles=include_dots
        )

        suggestions: list[tuple[str, str]] = []
        for path in matches:
            ext = Path(path).suffix.lower()
            type_hint = ext[1:] if ext else "file"
            suggestions.append((f"{prefix}{path}", type_hint))
        return suggestions

    def _get_folder_suggestions(self, query: str) -> list[tuple[str, str]]:
        """Return folder suggestions relative to project root.

        Returns:
            List of (label, description) tuples for matching directories.
        """
        try:
            dirs: list[str] = []
            for p in sorted(self._project_root.rglob("*")):
                if p.is_dir() and not any(
                    part.startswith(".")
                    for part in p.relative_to(self._project_root).parts
                ):
                    rel = p.relative_to(self._project_root).as_posix()
                    dirs.append(rel)
                if len(dirs) >= 500:
                    break

            include_dots = query.startswith(".")
            matches = _fuzzy_search(
                query, dirs, limit=MAX_SUGGESTIONS, include_dotfiles=include_dots
            )
            return [(f"@folder:{d}", "dir") for d in matches]
        except OSError:
            return []

    def _get_fuzzy_suggestions(self, search: str) -> list[tuple[str, str]]:
        """Legacy: get fuzzy file suggestions (bare @ path).

        Returns:
            List of (label, type_hint) tuples for matching files.
        """
        return self._get_file_suggestions(search)

    def on_key(
        self, event: events.Key, text: str, cursor_index: int
    ) -> CompletionResult:
        """Handle key events for navigation and selection.

        Returns:
            CompletionResult indicating how the key was handled.
        """
        if not self._suggestions:
            return CompletionResult.IGNORED

        match event.key:
            case "tab" | "enter":
                if self._apply_selected_completion(text, cursor_index):
                    return CompletionResult.HANDLED
                return CompletionResult.IGNORED
            case "down":
                self._move_selection(1)
                return CompletionResult.HANDLED
            case "up":
                self._move_selection(-1)
                return CompletionResult.HANDLED
            case "escape":
                self.reset()
                return CompletionResult.HANDLED
            case _:
                return CompletionResult.IGNORED

    def _move_selection(self, delta: int) -> None:
        """Move selection up or down."""
        if not self._suggestions:
            return
        count = len(self._suggestions)
        self._selected_index = (self._selected_index + delta) % count
        self._view.render_completion_suggestions(
            self._suggestions, self._selected_index
        )

    def _apply_selected_completion(self, text: str, cursor_index: int) -> bool:
        """Apply the currently selected completion.

        Returns:
            True if completion was applied, False if no suggestions or invalid state.
        """
        if not self._suggestions:
            return False

        label, _ = self._suggestions[self._selected_index]
        before_cursor = text[:cursor_index]
        at_index = before_cursor.rfind("@")

        if at_index < 0:
            return False

        # Replace from @ to cursor with the completion
        self._view.replace_completion_range(at_index, cursor_index, label)
        self.reset()
        return True


# Keep old name as alias for backwards compatibility
PathCompletionController = FuzzyFileController


# ============================================================================
# Multi-Completion Manager
# ============================================================================


class MultiCompletionManager:
    """Manages multiple completion controllers, delegating to the active one."""

    def __init__(self, controllers: list[CompletionController]) -> None:
        """Initialize with a list of controllers.

        Args:
            controllers: List of completion controllers (checked in order)
        """
        self._controllers = controllers
        self._active: CompletionController | None = None

    def on_text_changed(self, text: str, cursor_index: int) -> None:
        """Handle text change, activating the appropriate controller."""
        # Find the first controller that can handle this input
        candidate = None
        for controller in self._controllers:
            if controller.can_handle(text, cursor_index):
                candidate = controller
                break

        # No controller can handle - reset if we had one active
        if candidate is None:
            if self._active is not None:
                self._active.reset()
                self._active = None
            return

        # Switch to new controller if different
        if candidate is not self._active:
            if self._active is not None:
                self._active.reset()
            self._active = candidate

        # Let the active controller process the change
        candidate.on_text_changed(text, cursor_index)

    def on_key(
        self, event: events.Key, text: str, cursor_index: int
    ) -> CompletionResult:
        """Handle key event, delegating to active controller.

        Returns:
            CompletionResult from active controller, or IGNORED if none active.
        """
        if self._active is None:
            return CompletionResult.IGNORED
        return self._active.on_key(event, text, cursor_index)

    def reset(self) -> None:
        """Reset all controllers."""
        if self._active is not None:
            self._active.reset()
            self._active = None
