# Cross-platform notes

This file documents the Windows-specific quirks bog-agents-cli handles
internally, and the few that still bleed into user-visible workflows
(usually because the OS / shell mangles arg parsing before bog-agents
ever sees it).

## Things bog-agents-cli handles for you

### Shell subprocess UTF-8 decoding

`LocalShellBackend.execute()` passes `encoding='utf-8', errors='replace'`
to `subprocess.run()` so the reader thread never crashes on output
emitted by `npx`/`vitest`/`tsc`/`ripgrep`/etc. (cp1252 default would
die on `✓`, ANSI escape with `\xa0`, box-drawing chars, etc.).

If you write a wrapper that runs commands directly via `subprocess`
and you see `UnicodeDecodeError: 'charmap' codec can't decode byte
0x9d`, mirror this pattern.

### Process inspection (PID alive checks)

`os.kill(pid, 0)` is unreliable on Windows — it raises
`OSError [WinError 87]` *and* CPython sometimes propagates this as
`SystemError: returned a result with an exception set` (a known
C-level quirk). Use `bog_agents_cli._proc.is_running(pid)` which
delegates to `tasklist` on Windows.

`signal.SIGKILL` doesn't exist on Windows. Use
`bog_agents_cli._proc.terminate(pid, force=True)` which falls back to
SIGTERM (mapped to `TerminateProcess` by CPython on Windows).

### `--webhook-path` MSYS mangling

When running under Git Bash / MSYS, `--webhook-path /hooks/foo` is
rewritten to something like `C:/Program Files/Git/hooks/foo` *before*
argparse sees it. The CLI detects this exact mangle (only when the
prefix matches a known Git install) and recovers the intended path.
You shouldn't have to set `MSYS_NO_PATHCONV=1` for normal usage.

### Concurrent `--serve` / `langgraph dev` startup

Three CLIs invoking `-n` at the same instant used to all race for
port 2024. The CLI now always allocates a fresh ephemeral port via
`_find_free_port` for the default case, so concurrent invocations no
longer step on each other.

### Daemon binary discovery without PATH

`bog-agents-cli daemon start` falls back to the directory containing
`sys.executable` when `bog-agents-daemon` isn't on PATH (common when
the package is editable-installed via `uv pip install -e ...`).

### Daemon spawn env propagation

`subprocess.Popen` for the daemon forwards `env=os.environ.copy()` so
provider keys (`ANTHROPIC_API_KEY`, etc.) reach the child. The .exe
shim + `start_new_session=True` combination on Windows previously
dropped the env in some configurations.

## Things you might still need to know about

### Downstream consumers reading `--json` output

If you pipe `bog-agents-cli ... --json` into Python on Windows, set
`PYTHONIOENCODING=utf-8` for your downstream `print()` calls — the
agent's response field can contain unicode (`≤`, `≥`, `→`) and
Windows defaults stdout to cp1252.

### Path arguments that LOOK like POSIX paths under Git Bash

The CLI auto-corrects `--webhook-path /foo`, but other tools / args
that happen to take a POSIX-style path may still get mangled by MSYS.
The blanket workaround is `MSYS_NO_PATHCONV=1 bog-agents-cli ...` or
prefix the path with `//` (MSYS treats double-slash as opt-out).

### Daemon job persistence after a hard kill

Daemon job records are written through `os.fsync(f.fileno())` before
the atomic-rename, so a crash *between* the create and OS flush won't
lose the config. If your filesystem doesn't support fsync (some
network drives), this falls through silently — set
`BOG_DAEMON_DATA_DIR` to a local volume.
