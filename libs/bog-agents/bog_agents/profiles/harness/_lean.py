"""Built-in `lean` harness profile (ROADMAP #54).

The default harness spends roughly 8k tokens per turn before the user's own
words: an ~8 KB base prompt plus twelve tool schemas, two of which (`task`,
`write_todos`) carry multi-kilobyte descriptions. `lean` is the published
low-overhead point on that curve — a short base prompt, one-to-two-sentence
tool descriptions (argument schemas keep their own field docs, so nothing the
model needs to *call* a tool is lost), and no todo list. It is keyed by name
rather than by model spec: select it with
`create_agent(config=FeatureConfig(harness_profile="lean"))` or the CLI's
`--mini`, and it merges on top of whatever model-specific profile applies.

Measured with `bog_agents.token_audit`; the smoke-test baseline
(`tests/unit_tests/smoke_tests/test_harness_overhead.py`) pins both numbers so
a regression fails CI.
"""

from __future__ import annotations

from bog_agents.profiles.harness.harness_profiles import (
    HarnessProfile,
    _register_harness_profile_impl,
)

LEAN_PROFILE_KEY = "lean"
"""Registry key of the built-in lean profile."""

LEAN_BASE_PROMPT = """\
You are a Bog Agents coding agent working in the user's repository with the tools you are given.
Read before you edit, prefer small targeted edits, run the checks that prove a change, and report what you did and what you did not verify.
Ask only when the task cannot proceed safely without an answer. Never claim a result you did not observe."""
"""Three-sentence base prompt that replaces the default ~8 KB one."""

LEAN_TOOL_DESCRIPTIONS: dict[str, str] = {
    "task": (
        "Delegate a self-contained task to a subagent and receive its final report. "
        "Give complete instructions; `subagent_type` is `general-purpose` unless a specialised subagent is configured."
    ),
    "execute": "Run a non-interactive shell command in the workspace and return its output. Set a timeout for long commands.",
    "read_file": "Read a file, optionally a line range via `offset` and `limit`. Read before editing.",
    "read_many_files": "Read several files in one call.",
    "write_file": "Create or overwrite a file with the given content.",
    "edit_file": "Replace an exact string in a file; `old_string` must be unique unless `replace_all` is set.",
    "multi_edit_file": "Apply several exact string replacements to one file atomically.",
    "delete": "Delete a file or directory in the workspace.",
    "ls": "List a directory.",
    "glob": "Find files by glob pattern.",
    "grep": "Search file contents with a regular expression and return matching lines with their paths.",
}
"""Short descriptions for the built-in tools; argument schemas are untouched."""

LEAN_EXCLUDED_MIDDLEWARE: frozenset[str] = frozenset({"TodoListMiddleware"})
"""Always-present middleware the lean profile drops (an excluded name that never matches is an error, so only those)."""


def build_lean_profile() -> HarnessProfile:
    """The lean profile as a fresh `HarnessProfile` (handy for layering or tests)."""
    return HarnessProfile(
        base_system_prompt=LEAN_BASE_PROMPT,
        tool_description_overrides=dict(LEAN_TOOL_DESCRIPTIONS),
        excluded_middleware=LEAN_EXCLUDED_MIDDLEWARE,
    )


def register() -> None:
    """Register the built-in lean harness profile under `"lean"`."""
    _register_harness_profile_impl(LEAN_PROFILE_KEY, build_lean_profile())
