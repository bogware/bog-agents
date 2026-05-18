# Changelog

All notable changes to bog-agents (SDK), bog-agents-cli, and
bog-agents-daemon are documented here. The three packages are released
together with synchronised version numbers. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — Wave Y (release-readiness)

### Added

- **Audit-trail strict-hook safety net.** When `on_entry_recorded` is
  wired with `strict_hooks=False`, the middleware emits a one-time
  warning so compliance contexts (FINRA, SOC 2) don't silently retain
  in-memory entries that never flushed to durable storage. Default
  remains `False` for backwards compatibility.
- **`MessageStore` opt-in JSONL persistence.** Pass `persist_path=` to
  the constructor and every `append()` mirrors to disk line-by-line.
  `MessageStore.replay_from_persist()` rebuilds the transcript after
  a crash or session restart. Off by default for back-compat; on for
  daemon and crash-recovery paths.
- **`stop_all_preview_servers` tool.** New `browser_agent` tool that
  terminates every preview server the middleware is tracking — useful
  before starting a fresh set when the per-instance cap has been hit.
- **Structured OAuth observability.** `oauth_mcp.py` now logs token
  load (with seconds-remaining), token expiry, token issuance (with
  `expires_in`, `has_refresh_token`, `scopes`), and exchange failures
  at `INFO` level. Token values are never logged.
- **`__all__` declarations** on the headline middleware modules
  (filesystem, cost_tracker, summarization, subagents, worktree,
  safe_tools, memory, expert_rules, provider_retry, checkpointing,
  plan_mode, patch_tool_calls, thinking, intelligent_compaction,
  context_packing, adaptive_context). IDE autocomplete and
  `from foo import *` users now see a stable public surface.

### Changed

- **`start_preview_server` capped at 5 simultaneous servers** per
  middleware instance. Refuses duplicate ports. Stale exited
  processes are reaped on each call.
- **First-run setup wizard refuses to run when stdin is not a TTY.**
  In non-interactive contexts (`-p`, `-n`, piped stdin, daemon, CI),
  the wizard previously hung waiting for input; now it raises a
  `ModelConfigError` that names the relevant env vars (`ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `GOOGLE_API_KEY`) and points at the interactive
  wizard as the alternative.
- **Daemon `JobRun.dispatch_errors` capped at 20 entries** with an
  `(overflow)` summary so a wide-fanout outage doesn't blow up the
  run record JSON.
- **CHANGELOG.md restructured** to follow Keep-a-Changelog conventions
  with explicit `Added` / `Changed` / `Fixed` / `Removed` /
  `Documentation` / `Security` sections.

### Documentation

- `CLAUDE.md` brought up to date with current middleware count, the
  Wave W tool-bundle pattern, and the canonical-order test contract.
- READMEs refreshed across the repo root, `libs/bog-agents/`,
  `libs/cli/`, and `libs/daemon/` so a fresh OSS user can install and
  run with current accurate info.
- Console-script entry points (`bog-agents` canonical,
  `bog-agents-cli` alias) documented in `libs/cli/pyproject.toml`.

## [0.8.7] — 2026-05-17 — Wave X (production-readiness hardening)

### Security

- **`merge_worktree` ref injection.** Validate `source_branch` /
  `target_branch` via `_validate_git_ref` and pass them after a `--`
  separator so a model can't disguise a flag as a branch name.
- **`start_preview_server` command parsing.** Replaced naive
  `command.split()` with `shlex.split`; refuses shell metacharacters
  (`|`, `>`, `<`, `` ` ``, `$(`, `;`, `&`) with an actionable redirect
  to the shell-execute tool; pipes stdin to `DEVNULL` so interactive
  prompts can't block the agent forever.

### Fixed

- **Daemon per-target dispatch failures.** Output dispatch failures
  (email, webhook, Slack) were logged-and-forgotten. Captured on
  `JobRun.dispatch_errors` so operators can tell from the runs table
  that delivery failed even when the agent completed.

### Documentation

- `CLAUDE.md` STUB-middleware paragraph removed (Wave V deleted the
  modules). Added Wave W tool-bundle and ordering-test sections.
- `.gitignore` extended to cover `.workday-*`, `*.daemon.log`, and
  `.drive-artifacts/`.

## [0.8.6] — Wave W (drive runner + audit pass)

### Added

- **`bog-agents drive` — non-interactive scripted TUI runner.** YAML
  grammar describes slash commands, typed prompts, modal interactions,
  approval responses, snapshots, and assertions. The runner boots a
  real `BogAgentsApp` under Textual's `Pilot` and emits a JSONL
  transcript. 14 action types. New CLI flags: `--drive`,
  `--drive-stdin`, `--drive-var`, `--drive-artifacts`,
  `--drive-output`, `--drive-stop-on-failure`.
- **Deterministic chat-model shims.** `fake:<text>`, `replay:<jsonl>`,
  `record:<fixture>:<provider>` model schemes for CI-friendly scripted
  runs without network access.
- **Snapshot capture.** SVG (Textual `export_screenshot`) + text grid
  side by side, diff-friendly artifacts for PR review.
- **Tool-bundle pattern** (`bog_agents.tools.bundles`). Free-function
  factories return `list[BaseTool]` — the right shape for "middleware
  whose only job is delivering tools". `git_tools_bundle`,
  `multi_edit_tool`, `read_many_files_tool` exposed publicly.
  `GitToolsMiddleware` refactored to a thin shim that delegates.
- **Canonical middleware-ordering test.** Locks in the load-bearing
  middleware sequence in `graph.py` so a future refactor that
  reorders blocks fails CI rather than silently shifting
  cost-accounting or caching semantics.

### Fixed

- Server log file handle leaked when `subprocess.Popen` failed.
- Audit-trail hook exception silently dropped entries; added
  `strict_hooks` flag + `hook_failure_count` counter.
- Stream chunk timeout now logs the configured value at startup and
  reports actual elapsed time on the error.
- Checkpointing `git` failures now log at warning level and
  self-disable on critical failure so users aren't fooled into
  thinking undo works when it doesn't.
- Vault `KeyringError` warns once per process so a misconfigured
  keychain surfaces visibly.

### Changed

- `/record stop` now writes both the existing replay YAML and a
  drive-compatible script alongside it.
- `/replay run` drives each user message individually instead of
  building a single prose prompt.
- Removed the unwired `build_replay_prompt` API.

## [0.8.5] — Wave V (STUB cleanup)

### Removed

- **17 STUB middleware modules deleted.** Sixteen vertical-market
  scaffolds (`financial_data`, `due_diligence`, `earnings_analysis`,
  `tax_optimization`, `portfolio_analysis`, `market_sentiment`,
  `peer_comparison`, `meeting_prep`, `regulatory_alerts`,
  `regulatory_impact`, `scenario_engine`, `client_knowledge_base`,
  `client_reports`, `firm_deployment`, `agent_teams`,
  `multi_agent_orchestrator`) plus the demo `sso_auth` module. ~7,900
  lines net deletion.
- Z3 prover removed from `/prove` — heuristic prover is the only path.

### Changed

- `/causal` renamed to `/trace-mind` (legacy alias preserved).
- Lint policy consolidated; user-facing-string rules (`TRY003`,
  `EM101`, `EM102`) hoisted to global ignores.

## [0.8.4] — Wave U (architect-audit fixes)

Six surgical fixes (U1–U6) addressing edge cases in the model picker,
Bedrock inference-profile detection, and the HITL approval flow.

## [0.8.3] — Wave T (postmortem feedback loop)

- **Postmortem → dreamscape proposer feedback loop.** Every postmortem
  emits a structured signal the dreamscape proposer consumes to draft
  new expert rules, closing the trace-to-policy circle.

## [0.8.2] — Wave S (TraceFile v1)

- **TraceFile v1.** Ed25519-signed, Merkle-chained open trace format
  for cross-vendor observability. Includes a Claude Code adapter.

## [0.8.1] — Wave R (compliance auditor)

- **`/compliance` slash command + daemon cron trigger** emit
  HMAC-SHA-256 sealed reports.

## [0.8.0] — 2026-05-03

Major feature release. Several large feature drops shipped in successive
commits since 0.7.4 are consolidated here.

### Added — CLI

- **/peat — personal assistant + scheduler.** Long-lived in-process
  sub-agent with a hand-crafted persona (override via
  `~/.bog-agents/settings.json` `peat:` section). Subcommands: chat,
  `schedule "<cron>|<task>"`, `jobs`, `run`, `inbox`, `research <topic>`,
  `digest [--days N]`, `config`. Hybrid tool surface — full set when
  interactive, restricted (no shell, no destructive ops, write-only into
  the peat/ tree) when scheduled. Jobs persist to
  `~/.bog-agents/peat/jobs/<id>.yaml`; results buffer to
  `~/.bog-agents/peat/inbox.json` while the CLI is closed.
- **/qa — adaptive QA harness.** Acceptance-criteria ingestion from
  `--from-file`, `--from-json`, `--from-jira <TICKET>` (via MCP), or
  inline. Hybrid step model (agent / shell / http / mcp) with verdicts
  (`exit_code`, `status`, `contains`, `not_contains`, `regex`,
  `json_path`). Artifact outputs: markdown, JSON, stdout, jira-comment.
  Plans saved to `<project>/.bog-agents/qa-plans/`.
- **/record + /replay rewrite to YAML.** Auto-variabilizer detects Jira
  IDs, GitHub repo URLs, plain URLs, and UUIDs and turns repeated
  literals into a single `${var}` placeholder. Recordings save to
  `~/.bog-agents/replays/<id>.yaml`; legacy JSON loader retained for
  back-compat (no auto-migration).
- **Vault — session-only secret store.** `SecretStr` with redacted
  repr/str/format. Optional read-only OS-keychain bridge via the
  `keyring` library. Nothing written to disk; nothing exported to env.
- **Vars subsystem.** Typed slots (`string`, `secret`, `enum`, `int`,
  `bool`) with `${name}` substitution. Shared by /replay and /qa; CLI
  `--var key=value` overrides; missing values prompt the user via the
  AskUserMenu widget.
- Plus eight 0.8.0-track features that landed earlier (commits
  `13685c8`, `ad9bdab`, `393a2f2`, `2e42689`): personas, jury, race,
  full daemon CLI, standing orders, recipes, bug finder, skill flywheel,
  agent-md cascade, skill cascade, apply/plan models, token warnings,
  docker sandbox, telephone, always-ask, hooks, MCP catalog, auto mode
  (smart approval rule engine + Haiku risk eval).

### Added — SDK

- Subagent runtime validation in `create_agent()` — typo'd `name` /
  `description` / `system_prompt` fail fast instead of surfacing as
  KeyError later.
- `FileOperationError` Literal extended with `"parent_not_found"`.

### Changed

- **`FilesystemBackend` `virtual_mode` default flipped to `True`** —
  secure-by-default. `False` is deprecated and emits a
  `DeprecationWarning`. `LocalShellBackend` follows the same default.
- **`MemoryMiddleware`** caps each AGENTS.md source at 64 KiB and
  neutralizes `</agent_memory>` close-tags (prompt-injection defense).
- **CLI Python 3.14 classifier removed** until the wider stack supports
  it.

### Fixed

- `haiku_risk_eval` and `haiku_preflight_check` retry with a
  `fallback_model` when the primary returns `NotFoundError` (Haiku
  pinned-snapshot expiry guard).
- `non_interactive`: `server_session()` startup is now bounded by an
  `asyncio.timeout(45)` via `AsyncExitStack` (timeout covers boot only,
  not the agent loop).
- `non_interactive`: expanded RemoteException taxonomy with actionable
  messages for connection / model-not-found / auth / rate-limit errors.
- `preflight`: input prompt wrapped in `asyncio.wait_for(timeout=30)` so
  a hung TTY can't block the run forever.
- `auto_mode`: settings-file reads capped at 1 MiB; ASK patterns are now
  evaluated *before* ALLOW patterns (ensures `echo foo > file.txt` is
  caught even though `echo` is in the allow-list); `haiku_risk_eval`
  fails closed (treats API errors as risky).
- `cmd_explain`: regex symbol search uses `re.escape()` and ripgrep `-F`
  (ReDoS hardening).
- `AgentBuilder`: `with_sandbox(allow_dangerous=True)` and `with_mcp(...)`
  no longer crash `.build()` — the values now flow through correctly
  (or are documented as backend-layer concerns).
- `graph.py:create_agent`: `backend = backend if backend is not None
  else (StateBackend)` — the parens were grouping, not a function call;
  fixed to use the bare class as a `BackendFactory`.
- Various subprocess calls hardened with explicit `timeout=` (clipboard
  helpers, `auto_commit`, `cmd_pr_review`).
- `oauth_mcp.py`: token files now written atomically with `0o600` mode
  set on the temp file *before* the rename — closes the brief
  world-readable window.
- `peat/inbox.json`: writes are now atomic (tempfile + rename).
- `SecretStr.__hash__` disabled to prevent dict-key/set-membership leaks.

### Internal

- New `peat/`, `qa/`, `vault.py`, `vars.py` packages with comprehensive
  unit-test coverage (200+ new tests).
- `PeatScheduler` started in `BogAgentsApp.on_mount`; cleanly stopped in
  `on_unmount`.
- Public `__init__.py` exports updated for all new feature modules.

### Known follow-ups (tracked, not blockers)

- Peat scheduled-job runner is currently a "needs-interactive-execution"
  notifier — full unattended langgraph dispatch is a follow-up.
- `SessionRecorder.record_tool_call` exists but isn't yet wired into the
  live message stream; `/record` currently captures user/AI text only.
- Eight `@pytest.mark.skip(reason="not implemented yet")` tests in
  `test_sandbox_factory.py` need ship-or-delete decision.
- No integration test runs `non_interactive` against a live langgraph
  server end-to-end.

## [0.7.4] — 2026-04-30

Targeted patch release closing two reported Bedrock issues plus a
catalog refresh against live AWS / Anthropic / Google docs.

### Fixed

- **GH #53 — Bedrock probe flooded the log with TokenRetrievalError
  tracebacks.** A single `bog-agents` invocation produced 20+
  identical 50-line stack traces when the AWS SSO session was expired,
  because the langchain auto-detect loop calls `_has_bedrock_credentials`
  many times back-to-back. New negative-result cache in
  `_check_bedrock_thorough` logs the first failure of each kind with
  its traceback and emits a one-liner thereafter; a successful probe
  clears the cache so a freshly-renewed SSO session is detected
  without restarting the process.

- **GH #54 — Bedrock failed entirely when SSO config was expired even
  though `~/.aws/credentials` had fresh static keys.** boto3 walks
  the credential chain in order — when `~/.aws/config` declares a
  `sso_session = X` the SSO provider short-circuits the lookup
  before ever reading `~/.aws/credentials`, so `TokenRetrievalError`
  was the only outcome. The new `auth_mode` setting controls which
  source(s) are tried; the default `'auto'` mode now catches the
  SSO failure and **automatically retries with a static-credentials-
  only session** (SSO providers explicitly removed from the chain).
  Users with both expired SSO and fresh static keys now Just Work.

### Added

- **`auth_mode` for Bedrock**, with five values:
  - `auto` (default) — try every source; auto-fall-back from expired
    SSO to static credentials.
  - `sso` — force the SSO path; fail loudly when expired.
  - `static` — use only `~/.aws/credentials` (or
    `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`).
  - `profile` — use the named AWS profile from the new `aws_profile`
    config key.
  - `iam` — force IAM instance/role credentials (EC2/ECS/Lambda).

  Configurable via `[models.providers.bedrock]` in
  `~/.bog-agents/config.toml`, or via `BOG_AGENTS_BEDROCK_AUTH_MODE`
  / `BOG_AGENTS_BEDROCK_PROFILE` env vars (env wins). New helper
  `save_bedrock_auth_mode(mode, profile)` for programmatic config.

- **Provider catalog refreshed against live docs (2026-04-30):**
  - **Anthropic:** Claude Opus 4.7, Sonnet 4.6, Haiku 4.5 (current);
    Opus 4.6, Sonnet 4.5, Opus 4.5, Opus 4.1 (legacy still available).
  - **Bedrock:** added inference-profile-prefixed IDs
    (`us.anthropic.claude-opus-4-7`, `us.anthropic.claude-sonnet-4-6`,
    etc.) plus base IDs for Anthropic, Amazon Nova
    (Premier/Pro/Lite/Micro), Meta Llama 4 Maverick + Scout + 3.3 70B,
    and Mistral Large 3 / Pixtral Large.
  - **Google:** Gemini 2.5 Pro / Flash / Flash-Lite GA, plus the
    Gemini 3 preview family (`gemini-3.1-pro-preview`,
    `gemini-3-flash-preview`, `gemini-3.1-flash-lite-preview`).

### Tests

- 5 new tests in `TestBedrockProbeCache` and `TestBedrockAuthMode`:
  - probe-cache classification (sso-expired, no-credentials)
  - 5x failure → 1 traceback (regression for #53)
  - successful probe clears the cache
  - default auth mode is `auto`
  - env var overrides config
  - `save_bedrock_auth_mode` round-trip
  - `save_bedrock_auth_mode` rejects invalid modes
  - **auto-fallback from expired SSO to static creds** (regression for #54)

Full CI matrix (Windows variant): SDK 1226 passed, CLI 2685 passed,
daemon 95 passed. Lint + ty clean.



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

### Security

This release closes 12 known CVEs in the dependency graph by raising
the minimum-version floor on direct deps and adding defensive
constraints on transitive deps. After the bumps, `pip-audit` reports
**zero known vulnerabilities** across all three package venvs.

| Package | Old | New | Advisory |
|---|---|---|---|
| `langchain-core` | 1.2.18 | 1.2.28 | CVE-2026-40087 |
| `langchain-openai` | 1.1.8 | 1.1.14 | GHSA-r7w7-9xr2-qq2r |
| `langsmith` | 0.7.7 | 0.7.31 | GHSA-rr7j-v2q5-chgv |
| `pillow` | 10.0.0 | 12.2.0 | CVE-2026-40192 |
| `python-dotenv` | 1.0.0 | 1.2.2 | CVE-2026-28684 |
| `requests` | 2.0.0 | 2.33.0 | CVE-2026-25645 |
| `pyjwt` | (transitive) | 2.12.0 | CVE-2026-32597 (auth-validation) |
| `cryptography` | (transitive) | 46.0.7 | CVE-2026-34073 + CVE-2026-39892 (TLS) |
| `python-multipart` | (transitive) | 0.0.26 | CVE-2026-40347 (DoS) |
| `pygments` | (transitive) | 2.20.0 | CVE-2026-4539 (ReDoS) |
| `pyasn1` | (transitive) | 0.6.3 | CVE-2026-30922 (DoS) |
| `pytest` (test-only) | 8.3.4 | 9.0.3 | CVE-2025-71176 |

In addition: daemon API responses now redact `smtp_password`,
`github_token`, and `webhook_secret` to `'***'` (the on-disk
`jobs.json` retains them — the daemon needs them to authenticate, but
they're never echoed back through HTTP).

Token-file ACLs on Windows: `chmod(0o600)` only flips the read-only
bit on Windows, but the daemon now also runs `icacls /inheritance:r
/grant <user>:F` best-effort to drop inherited ACEs on the token file.

### Known limitations

- `--sandbox modal/daytona/runloop/langsmith` requires live cloud
  credentials and isn't covered by the in-tree test suite.
- `--acp` requires the optional `[acp]` install extra. The CLI emits
  a clean error message when it's missing.
- `bog-agents-cli call` requires a `--serve` instance running; the
  current path doesn't auto-spawn one. Use `bog-agents-cli --serve`
  in a separate terminal first.
