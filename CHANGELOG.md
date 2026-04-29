# Changelog

All notable changes to bog-agents (SDK), bog-agents-cli, and
bog-agents-daemon are documented here. The three packages are released
together with synchronised version numbers.

## [0.7.3] — 2026-04-29

This release lands ~20 fixes accumulated during multi-day validation
passes against the Oregon Trail testbed plus a meta-pass that drove
real feature work end-to-end. The headline is shell reliability: the
recurring "agent claims it can't run shell" failure that bit users
across passes 1-5 is fully resolved (root cause was Windows cp1252
decoding in the subprocess reader thread).

### Added

- **`bog-agents-cli verify`** subcommand. Auto-detects project type
  (Python, Node, Rust, Go, Java) from indicator files and runs the
  canonical `typecheck` / `lint` / `test` chain. Writes a
  `verification_summary.md` artifact the agent can read and quote.
  Per-project override via `.bog-agents/verify.sh` (POSIX) or
  `.bog-agents/verify.cmd` (Windows). `--json` envelope supported.
- **`bog-agents-cli call`** thin client for a long-lived `--serve`
  instance. POSTs to `/invoke` and prints the response. Eliminates the
  ~5-10s langgraph-dev startup tax per `-n` invocation. Supports
  `--thread <id>` to resume conversations, `--json` envelope.
- **Bundled subagent library**. Project-type-aware default subagents
  (`code-reviewer`, `test-author`, plus language-specific specialists
  like `react-ink-artist` and `refactorer`) ship for Python, Node,
  Rust, and Go. Loaded automatically when the project root has the
  matching indicator file. Users override by name with their own
  AGENTS.md.
- **Skill chaining**. SKILL.md frontmatter now supports `chain: [a, b]`
  to compose multiple skills into one prompt. Cycle-safe (raises on
  detected cycle), capped at 8 levels deep.
- **`/openapi.json`** on `--serve`. Hand-rolled OpenAPI 3.0 schema for
  every documented endpoint. Any standard OpenAPI client (Swagger UI,
  Stoplight, openapi-typescript) can introspect the API.
- **Cross-platform process helpers** (`bog_agents_cli._proc`).
  `is_running(pid)` uses `tasklist` on Windows to dodge the
  `os.kill(pid, 0)` → `OSError [WinError 87]` / `SystemError` quirk.
  `terminate(pid, force=...)` gates SIGKILL behind
  `hasattr(signal, 'SIGKILL')` so it's a no-op on Windows.
- `cross-platform-notes.md` — documents Windows quirks the CLI handles
  and the few that still bleed into user workflows.

### Fixed

- **#37 (CRITICAL):** Shell subprocess crashes on UTF-8 output (cp1252
  reader). `LocalShellBackend.execute()` now passes
  `encoding='utf-8', errors='replace'` so `npx`/`vitest`/`tsc`/
  `ripgrep` output never crashes the reader thread on Windows. This
  was the root cause of the recurring "agent can't run shell" pattern.
- **#36:** `--auto-approve` was silently ignored in `-n` mode. Now
  threaded through to `run_non_interactive` and forces
  `enable_shell=True` when set.
- **#34:** `daemon jobs create --output` now accepts `email` and
  `github_comment` (the daemon already implemented dispatchers for
  both; only the CLI argparse layer was stale). New flags:
  `--output-email-{to,from,smtp-host,smtp-port,smtp-user,smtp-password}`
  and `--output-github-{repo,issue}`.
- **#33:** Three concurrent `-n` CLI invocations no longer race for
  port 2024. `SDKServer.start()` always allocates a fresh ephemeral
  port via `_find_free_port` for the default case.
- **#29:** `daemon stop` no longer crashes on Windows. Prefers HTTP
  `/shutdown` first; falls back to SIGTERM (mapped to TerminateProcess
  on Windows) wrapped in proper exception handling. SIGKILL is gated
  behind `hasattr(signal, 'SIGKILL')`.
- **#28:** `-r THREAD_ID` now works in `-n` mode. Thread history is
  loaded into the LangGraph checkpointer before the agent runs.
- **#27:** Daemon subprocess now inherits the CLI's full env
  (`env=os.environ.copy()`) so `ANTHROPIC_API_KEY` and other provider
  keys reach the child on Windows under the .exe-shim +
  `start_new_session=True` combination.
- **#26:** `daemon status` and `daemon start` no longer crash on
  Windows when the PID file is stale. Routes through the new
  `_proc.is_running` helper.
- **#25:** `--auto-commit` no longer falls back to `git add -A` when
  the agent's edits aren't visible in the LangGraph stream namespace.
  Two complementary sources: stream-introspection (`StreamState.
  edited_paths` from `tool_call` blocks across all subagent
  namespaces) plus `git status --porcelain` diff between pre- and
  post-run snapshots. Output chrome shows the file count.
- **#24:** `--json` envelope no longer leaks chrome to stdout. Console
  is routed to stderr when `output_format == "json"`, mirroring the
  `--quiet` behavior.
- **#22, #23:** Daemon-driven jobs now have full filesystem access
  (LocalShellBackend rooted at the job's `working_dir`) and the file-
  output guard accepts paths under both `working_dir` and `cwd` on
  Windows. Skills + pipelines that read project files now actually
  work end-to-end.
- **#21:** `daemon jobs run` now polls `/jobs/{id}/runs` for terminal
  state instead of holding open a 30s HTTP request. The endpoint
  returns `202 Accepted` with a `running` placeholder; CLI polls until
  `completed|failed`. Long-running agent jobs no longer time out the
  HTTP layer.
- **#20:** `bog-agents-cli daemon start` falls back to the directory
  containing `sys.executable` when `bog-agents-daemon` isn't on PATH.
- **#19, #18:** Subagent / main-agent system prompt includes an
  explicit anti-fabrication rule (`DEFAULT_SUBAGENT_PROMPT` and the
  CLI's `system_prompt.md`). Agents now state explicitly "I could not
  run X because Y" instead of inventing tool output.
- **#17:** `--auto-commit` works in non-interactive mode. Was wired
  only through the TUI path before.
- **Webhook auth model:** `/webhooks/{path}` no longer requires the
  daemon-management bearer token. External services (GitHub, Slack,
  CI) authenticate via the per-trigger `webhook_secret` HMAC over
  `X-Hub-Signature-256` instead. Daemon-token requests still bypass
  HMAC for the local CLI test path.
- **`--webhook-path` MSYS mangling:** auto-detects when Git Bash
  rewrote `/hooks/foo` into `C:/Program Files/Git/hooks/foo` and
  recovers the intended path. No more `MSYS_NO_PATHCONV=1` workaround
  needed for normal usage.
- **Daemon job durability:** `_save_jobs_unlocked` and `save_run` now
  `os.fsync()` before atomic-rename. A hard daemon kill between write
  and OS flush no longer loses freshly-created jobs.
- **CLI/daemon process inspection** — `daemon_client.is_daemon_running`
  and `cmd_daemon._is_running` both delegate to `_proc.is_running`.

### Changed

- `--output` flag in `daemon jobs create` accepts seven targets
  (`log, stdout, file, slack, webhook, email, github_comment`) up
  from five.
- Default subagent prompt (`DEFAULT_SUBAGENT_PROMPT`) prefixed onto
  every custom subagent prompt by `graph.py`, including the anti-
  fabrication rule and shell-honesty guidance.
- `--serve`'s `/info` endpoint now lists `/openapi.json` in its
  endpoint list.

### Deprecated

- The async helper `_git_dirty_paths` in `non_interactive.py` is
  retained as an `asyncio.to_thread` wrapper around the new
  synchronous `_git_dirty_paths_sync`. The async-create-subprocess
  variant was unreliable on Windows (couldn't locate `git.exe`
  without an absolute path).

### Known limitations

- `--sandbox modal/daytona/runloop/langsmith` requires live cloud
  credentials and isn't covered by the in-tree test suite.
- `--acp` requires the optional `[acp]` install extra. The CLI emits
  a clean error message when it's missing.
- `bog-agents-cli call` requires a `--serve` instance running; the
  current path doesn't auto-spawn one. Use `bog-agents-cli --serve`
  in a separate terminal first.
