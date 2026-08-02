"""Single source of truth for tunable timeouts and bounds.

Names rather than magic literals at use sites. Every timeout in the
codebase should pull from here so:

1. There's one place to grep when you ask "how long does X wait?"
2. Future tuning can happen without touching every callsite.
3. Documentation about behaviour stays in one place.

The values are deliberately conservative — *short enough to fail loud,
long enough to forgive a slow disk or a paused VPN*.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Subprocess timeouts (seconds)
# ---------------------------------------------------------------------------

# Short, read-only git probes (rev-parse, ls-files). On a healthy system
# these are sub-second; 10s gives plenty of room for slow disks / WSL.
GIT_PROBE_TIMEOUT_S: float = 10.0

# Git operations that may write (add / commit). Hooks run inside this
# budget too — most pre-commit hooks finish in <5s. 30s is a deliberate
# compromise; longer hooks should run async outside the auto-commit path.
GIT_WRITE_TIMEOUT_S: float = 30.0

# Clipboard helpers (pngpaste, xclip, wl-paste). These should be
# essentially instant; if they're not, the clipboard helper is broken.
CLIPBOARD_PROBE_TIMEOUT_S: float = 5.0

# `gh` / `az` / similar third-party CLI probes used to detect what's
# installed. The probe itself is just `--version`.
TOOL_VERSION_PROBE_TIMEOUT_S: float = 10.0

# Generic shell command run inside QA / Peat / agent paths. 60s is the
# default per-step budget; users can override via the step's own
# `timeout_s` field for long-running tests.
DEFAULT_SHELL_STEP_TIMEOUT_S: float = 60.0

# Default for `LocalShellBackend.auto_background_after` (Tier-1 #1). A
# foreground shell command that has not finished after this many seconds is
# moved to the background as a pollable task instead of being killed at the
# (much larger) tool timeout. Configurable via the `BOG_AGENTS_SHELL_AUTO_BACKGROUND_AFTER`
# env var or `runtime.shell_auto_background_after`; `off`/`none`/`0` disables.
DEFAULT_SHELL_AUTO_BACKGROUND_AFTER_S: float = 60.0


# ---------------------------------------------------------------------------
# Network / agent timeouts (seconds)
# ---------------------------------------------------------------------------

# Bound the langgraph subprocess startup + first client handshake. If
# the server isn't up by then, something is broken (port collision,
# missing deps, infinite import loop) and we'd rather surface it than
# hang the CLI.
SERVER_STARTUP_TIMEOUT_S: float = 45.0

# Generic HTTP request timeout for the QA executor's http step kind.
HTTP_STEP_TIMEOUT_S: float = 60.0

# How long we wait for a single Haiku risk-eval / preflight call before
# treating it as a failure. Haiku is fast; 30s is generous.
HAIKU_CALL_TIMEOUT_S: float = 30.0


# ---------------------------------------------------------------------------
# UI / interactive timeouts (seconds)
# ---------------------------------------------------------------------------

# How long we wait for the user to respond to an AskUserMenu prompt
# during preflight. Prevents a hung TTY from blocking the run forever.
PREFLIGHT_INPUT_TIMEOUT_S: float = 30.0

# Default Peat job execution budget if the job's own ``timeout_s`` is
# not set.
DEFAULT_PEAT_JOB_TIMEOUT_S: float = 600.0

# Maximum time we'll wait for an in-flight interactive agent turn to
# finish before a scheduled Peat job gives up and skips its fire.
PEAT_TURN_WAIT_TIMEOUT_S: float = 300.0


# ---------------------------------------------------------------------------
# Scheduler tick interval (seconds)
# ---------------------------------------------------------------------------

# Production tick — every 30s the scheduler walks the jobs directory.
# Tests can pass a smaller value (down to 0.05s).
DEFAULT_SCHEDULER_TICK_S: float = 30.0


# ---------------------------------------------------------------------------
# Resource bounds (counts / bytes)
# ---------------------------------------------------------------------------

# Cap on the number of inbox notifications we keep on disk.
PEAT_INBOX_MAX_ENTRIES: int = 500

# Per-entry size cap inside the inbox. A misbehaving job could try to
# write a multi-MB summary; truncate it before it hits the file.
PEAT_INBOX_ENTRY_MAX_BYTES: int = 16 * 1024  # 16 KiB

# Cap on settings.json size. Anything larger than this is almost
# certainly malformed or malicious.
SETTINGS_FILE_MAX_BYTES: int = 1 * 1024 * 1024  # 1 MiB

# Cap on AGENTS.md memory source size — already enforced in the SDK's
# MemoryMiddleware. Mirrored here for grep-discoverability.
MEMORY_SOURCE_MAX_BYTES: int = 64 * 1024  # 64 KiB

# Cap on the number of AI message bytes we record in the replay session
# transcript. Long answers get truncated to keep recordings small.
REPLAY_AI_MESSAGE_MAX_BYTES: int = 2_000

# Cap on the number of replay session steps we'll persist. A pathological
# session could try to pin a million tool calls in one recording.
REPLAY_MAX_STEPS: int = 5_000

# Cap on the number of bytes captured from each shell step's combined
# stdout+stderr. Output beyond this is truncated.
SHELL_OUTPUT_MAX_BYTES: int = 8_000


# ---------------------------------------------------------------------------
# Retry policy defaults
# ---------------------------------------------------------------------------

# Default for the SDK retry middleware around provider calls. Three
# attempts (1 + 2 retries) with an exponential delay starting at 1s.
PROVIDER_RETRY_ATTEMPTS: int = 3
PROVIDER_RETRY_INITIAL_DELAY_S: float = 1.0
PROVIDER_RETRY_MAX_DELAY_S: float = 16.0
PROVIDER_RETRY_BACKOFF_FACTOR: float = 2.0
