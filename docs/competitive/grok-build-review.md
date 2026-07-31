# Competitive review: SpaceXAI Grok Build (`grok`)

**Reviewed:** 2026-07-31 · **Source:** <https://github.com/draxios/grok-build> (a
periodic sync of SpaceXAI's monorepo) · **License:** Apache 2.0 (© SpaceXAI).

Grok Build is SpaceXAI's terminal coding agent — a full-screen Rust TUI with an
agent runtime, ~60 crates, and a complete 24-document user guide. This report
captures what is worth learning from it for the bog-agents **1.0** push.

## License / reuse verdict

- **Apache 2.0 is permissive.** We can reuse code with attribution, but because
  grok is **Rust** and bog is **Python**, we are gleaning *ideas* (not
  copyrightable) and reimplementing — there is effectively no license burden.
- Grok itself is a portfolio of borrowed ideas. Its `THIRD-PARTY-NOTICES`
  records tool implementations **ported from two competitors we can also draw
  from directly**:
  - **openai/codex** (Apache 2.0): `apply_patch`, `grep_files`, `list_dir`, `read_file`
  - **sst/opencode** (MIT — the friendliest license): `bash`, `edit`, `glob`,
    `grep`, `read`, `skill`, `todowrite`, `write`
- It bundles **ripgrep** (always) plus optional **ugrep** and **bfs** (fast
  breadth-first `find`), self-extracted to `~/.grok/vendor/`. We already do
  managed ripgrep; extending the pattern is cheap.

## What bog already ships or has planned (we are not behind)

| Grok feature | bog equivalent |
|---|---|
| `/dream` memory consolidation | **Dreamscape** (dormancy-triggered dream + consolidation) |
| Plan mode | `/plan` + `PlanModeMiddleware` |
| Subagents / `run_task` | `SubAgent` + `task()` tool |
| Cost tracking | `CostTrackerMiddleware` (+ CTX-3 pricing fix) |
| Skills | `SkillsMiddleware` + skill trust store |
| MCP + OAuth | `mcp_oauth.py` (RFC-9728, PKCE) |
| Project rules (AGENTS.md) | AGENTS.md cascade |
| Themes | `theme.py` (`bog` palette + `/theme`) |
| Marketplace | MCP marketplace + skills |
| Self-update | `/update` (pip/uv/pipx aware) |
| Per-command OS sandbox | **#22** bubblewrap/seatbelt + egress-allowlist proxy |
| Permission gate | HITL + `SafeToolsMiddleware` + `_DANGEROUS_PATTERNS` |
| `auto` mode | operator mode + `/auto` |
| Agent Dashboard | **ROADMAP #11** (Mission Control) — planned |
| Per-line LOC attribution | **ROADMAP #44** (bog blame) — planned |
| Cost caps / teams / evidence / best-of-N | shipped in 0.10/0.9.11 |

So this exercise is about **sharpening**, not catching up.

---

## Findings by theme (the durable detail)

### A. Agent execution model

- **Single leader process per machine.** One "leader" holds the live agent +
  all session state; every UI (TUI, IDE-over-ACP, headless, WebSocket) is a thin
  client attaching over a Unix socket. Coordinated by an `flock` on
  `~/.grok/leader.lock`. State (running turns, queue, subagents) survives client
  disconnect; an IDE and a terminal can drive the **same** turn. Careful
  lifecycle: version-directional eviction (newer client evicts older leader),
  `/proc/locks`-verified zombie kill (refuses on macOS/BSD where it can't prove
  the holder), reconnect backoff. Leader mode is refused under any non-`off`
  sandbox profile.
- **Editable, versioned prompt queue.** Prompts typed while a turn runs line up
  in a shared queue with `id` + monotonic `version` (stale edits no-op) +
  `owner`/`last_editor` attribution; a pure, unit-tested `combine` predicate
  merges consecutive plain prompts into one turn.
- **Subagent capability lattice (fail-closed).** A spawn request is *intersected*
  with the role ceiling: `All` = identity, `ReadOnly` absorbing, and
  `ReadWrite ∩ Execute → ReadOnly` (collapses to the safe floor). Precedence:
  explicit arg > role default > persona default > parent inheritance. Persona
  file errors are fatal (fail-closed); a missing role prompt only warns.
- **`resume_from` + fork-context normalization.** A finished subagent can seed a
  new one; forked parent history is compressed to `[System(placeholder),
  User(background)]` — last 3 turns verbatim, older turns summarized (counts +
  tools used), stripping what the child regenerates. The task prompt is placed
  **last** for maximum recency. Model is deliberately *not* a resume-identity
  gate (type + persona are).
- **Plan mode as a persisted 4-state machine** (`Inactive → Pending → Active →
  ExitPending`) enforced **beneath** yolo — `Active` rejects edits to any file
  but `plan.md` in *every* permission mode, including always-approve. Each
  subagent starts with a fresh `Inactive` tracker.
- **Synthetic turns / background wake.** Long commands, `monitor` streams,
  `/loop` schedules, and background subagents can re-enter the agent on their own
  as a `synthetic` turn injected **into the same active turn**. A status line
  counts live background work; a silent successful wake leaves no trace, but a
  silent **failure** always emits a "Turn failed" line.
- **Lifecycle "contributor" model.** Extensions plug in as data-only
  contributors (`TurnInputContributor`, `TurnLifecycleContributor`,
  `SessionLifecycleContributor`, `CommandContributor`) that receive per-hook
  inputs and never own loop control — capabilities injected at install time.
- **Headless output formats** incl. `streaming-messages-json` **wire-compatible
  with Anthropic's Messages `stream-json`** so Claude-Code-style consumers work
  unchanged. Cost accounting is disjoint (uncached input / cache-read /
  cache-creation), uses integer `total_cost_usd_ticks` (1 USD = 10^10), and when
  any call lacks cost it fires `cost_is_partial` and **omits every cost float**
  rather than reporting a fake total.
- **Session persistence: JSONL is the source of truth, SQLite FTS5 is a
  rebuildable index.** Per session: `updates.jsonl` (authoritative ACP stream),
  `chat_history.jsonl`, `summary.json`, `rewind_points.jsonl` (per-prompt file
  snapshots → `/rewind`). IDs are UUIDv7. The journal detects network
  filesystems via `statfs` magic and switches SQLite WAL→TRUNCATE + per-host DB
  files to dodge a real WAL-over-NFS SIGBUS.

### B. Tools, terminal (PTY), sandbox, permissions

- **Tool set:** `read_file`, `list_dir`, `grep` (ripgrep), `search_replace`
  (anchored edit, `apply_patch` alt dialect), `run_terminal_cmd`, `web_search`,
  `web_fetch` (with an SSRF guard), `todo_write`, `run_task` (subagents),
  `monitor`, background get/wait/kill, `scheduler_*` (backs `/loop`),
  `update_goal`, `workflow` (Rhai scripts with an `agent_budget` cap over
  children), `lsp`, plan-mode enter/exit, `ask_user_question` (structured
  interview), `image_gen`/`image_edit`/`image_to_video`, `deploy_app`.
- **Multi-namespace tool "dialects"** (`grok_build`, `_concise`, `_hashline`,
  `codex`, `opencode`, `mcp`): the same op ships in several wire schemas so one
  agent can impersonate the tool contract another expects; a `CanonicalToolMeta`
  envelope joins equivalents by `label`.
- **`ptyctl` — headless PTY controller.** Spawns any program in a real PTY,
  renders its live screen through an `alacritty_terminal` grid, and exposes
  send-keys / read-screen / wait over CLI + HTTP/WebSocket. Vim-notation keys
  (`<Esc>:wq<CR>`, `<C-c>`). Event-driven waits (`Text/Regex/Gone/StableMs`) via
  a `watch` generation counter (never polls); timeouts return a full screen
  snapshot. This is how it drives `vim`/`top`/REPLs — a "terminal robot."
- **OS sandbox = `nono` (Landlock/Seatbelt) + seccomp, applied once to the whole
  process.** Filesystem confinement via Landlock/Seatbelt; a hand-built seccomp
  BPF filter in the child's `pre_exec` blocks `connect/bind/sendto/...` (network
  cut); an anti-escape filter denies `unshare`/`setns`/`clone3`/`CLONE_NEW*`. For
  `deny` paths it **bwrap-rebinds a `chmod 000` placeholder** so denied files
  can't be `mv`'d out and read (`mv secret x && cat x` closed). Shell-env policy
  strips `*KEY*`/`*SECRET*`/`*TOKEN*` from child env. "Requested but unapplied" =
  fail-closed.
- **`monitor`** — a streaming, self-throttling command watcher (each stdout line
  → a mid-turn notification), debounced + token-bucket rate-limited, auto-kills
  after 30s of continuous flood. Default/max timeout 10h; `persistent` for the
  session.
- **Permission pipeline (5 stages, `deny > ask > allow`):** PreToolUse hooks
  (deny-only, fail-open) → merged rules by severity → remembered grants →
  built-in read-only auto-approvals → mode policy. `deny`/`ask` check **every**
  chained segment (`&&`/`||`/`;`/`|`); `allow` checks the whole string only;
  wrappers (`timeout`/`env`/`nice`) are peeled.
- **`exec-risk` floor** — a static analyzer that forces a prompt for
  read-only-looking commands that can smuggle code execution: `git -c
  core.fsmonitor=` / `--work-tree` / `alias.x=!…`, `sort --compress-program=`
  (incl. long-option abbreviations), `cargo check`/`tee` excluded from read-only.
- **`auto` mode** — an LLM tool-call classifier with tree-sitter heuristic
  fast-paths; every verdict carries `provenance` (`Llm/Heuristic/Failure`) and on
  classifier failure the outcome is **fail-closed** (`Unavailable` → blocked).
- **bash tool:** auto-backgrounds a foreground command that blocks past 15s
  (moved, **not killed**); persistent shell state across calls via an fd-3/fd-4
  snapshot; cgroup `memory.high` OOM caps; process-tree teardown (`killpg` /
  Windows Job Object), owner-session-scoped.
- **Remembered grants** (`[ui] remember_tool_approvals`) are short-form,
  per-project, never in git, with a hard "dangerous list always re-prompts"
  floor (`rm`, `chmod`, `git push`, …).

### C. Extensibility

- **15-event lifecycle hook bus** (`SessionStart`, `UserPromptSubmit`,
  `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionDenied`, `Stop`,
  `StopFailure`, `Notification`, `SubagentStart/Stop`, `PreCompact`,
  `PostCompact`, `SessionEnd`) generated from one macro table; each event has a
  trait triple (gate: `Observe`/`Tool`(deny)/`Stop`(block); matcher:
  `Tested`/`Ignored`; hub-forward). **Ingests Claude Code and Cursor hook files
  unchanged** via alias tables. Fail-open: only explicit `deny`/`block` has
  teeth.
- **"Keep-working" Stop gates.** A `Stop` hook can emit `{"decision":"block",
  "reason":…}` to feed a reason back as a user message and loop — an in-harness
  "definition of done" (run tests before finishing), capped at **8
  continuations**. The Stop payload carries `backgroundTasks` + `sessionCrons` so
  a gate can tell "done" from "waiting on background work." Gates get a 600s
  timeout (they run suites); other events 5s.
- **Plugins = bundles of 6 component types** (skills, commands, agents, hooks,
  MCP, LSP) with a **two-axis security model**: *enable* (load declarative
  content) is orthogonal to *trust* (run code — hooks/MCP/LSP stay inert until
  trusted).
- **Git-repo marketplaces** with a display-only `plugin-index.json` catalog,
  rich install refs (`owner/repo@sha`), org-enforceable **SHA pinning**
  (`require_sha`), and **untrusted-catalog sanitization** (strips terminal-escape
  + Unicode bidi/zero-width spoofing).
- **Skills** as auto-invocable prompt packages, distinct from plugins along the
  invocation axis: `user-invocable` × `disable-model-invocation` → four
  behaviors (both / slash-only / model-only / hidden).
- **Hybrid RAG memory.** Markdown under `~/.grok/memory/` (global + per-workspace
  keyed to the git **origin** so clones/worktrees share it) indexed by SQLite
  FTS5 (BM25) + `vec0` (KNN). Search fuses vector (0.7) + BM25 (0.3), with
  **temporal decay** (session chunks only, 7-day half-life; curated memory
  exempt), per-source weights, and opt-in **MMR** re-ranking. `/dream`
  consolidates with a distributed lock.
- **Custom models:** three `api_backend`s (`chat_completions`/`responses`/
  `messages`) behind one `[model.*]` TOML; `env_http_headers` (secret, in-memory)
  vs `query_params` (persisted, non-secret) split; provider blocks inherited.
- **Layered org distribution:** `managed_config.toml` (merges) +
  `managed-settings.json` (protected, un-overridable) + `requirements.toml`;
  hooks-in-config with provenance labels; `allowedMcpServers` / `require_sha`
  allowlists; one cascading `trusted_folders.toml`.
- **Project rules:** loads *every* recognized filename (`AGENTS.md`, `CLAUDE.md`,
  `CLAUDE.local.md`, …) + `.grok/rules/*.md` (+ `.claude`/`.cursor`), with
  **deeper-directory-wins** precedence.

### D. UX / TUI / platform novelties

- **Voice dictation** — mic → xAI streaming STT → live transcript in the
  composer (`Ctrl+Space`/`F8`/`/voice`), toggle vs hold-to-talk. Out-of-process
  mic capture (re-exec'd subprocess) so the TUI never pays the audio stack's
  memory cost; connect/capture racing so the first word isn't clipped.
- **In-terminal Mermaid** — offline pure-Rust render (`mermaid-to-svg` →
  `resvg`/`tiny-skia`, bundled font, no resolvers), out-of-process with a
  killable timeout, theme-aware and lazy.
- **Kitty-protocol images + video** in scrollback — transmit-once/place-only,
  z-index layering, source-cropping at the viewport edge, cell-aspect
  correction, 64 MB byte cache; honest capability gating (Kitty/Ghostty/WezTerm
  only, Windows/ConPTY disabled).
- **System power / sleep-wake awareness** (`xai-system-power`) — vetoes OS
  suspend briefly to finish a token refresh (one-time-use OIDC), distinguishes
  macOS **dark wake**, `hold_awake()` RAII assertion during turns.
- **Per-hunk change tracking** with agent-vs-human LOC attribution
  (`AgentEdit`/`ExternalEditOnAgentFile`/`External`) written as signed-delta
  JSONL so a plain `SUM` is correct.
- **Agent Dashboard** (`Ctrl+\`) — mission-control for many concurrent
  sessions/forks, grouped by state, peek/reply/dispatch/attach, typed search
  (`a:name` / `s:state` / `#text`).
- **Premium self-update** — parallel byte-range download, atomic rename +
  smoke-test, relative symlinks (Docker-safe), multi-CDN fallback, hard version
  floors/ceilings (`required_minimum_version`).
- **Theming follows OS light/dark live** (macOS/Linux XDG portal/Windows +
  OSC-11 over SSH), terminal-native notifications (OSC 9/99/777, focus-gated),
  cursor color as a session indicator (OSC 12).
- **Terminal-brand-adaptive keybindings** — fingerprints VS Code/Cursor/
  Ghostty/WezTerm/Kitty and negotiates the best chord + paste path.

### Cross-cutting design signatures worth internalizing

1. **Out-of-process isolation** for anything risky or memory-heavy (mic,
   mermaid, update smoke-test) with real killable timeouts.
2. **Capability negotiation** (graphics/color/keyboard/notification protocols)
   with honest fallbacks — never assume a modern terminal.
3. **Fail-safe defaults**: sandbox "requested-but-unapplied" = closed; hooks
   fail-open but only explicit deny has teeth; cost omitted rather than
   partial.
4. **Compat-as-a-feature**: ingest Claude Code + Cursor config unchanged.
5. **Table/macro-generated models** (hook events) so parse/serialize/gate never
   drift.

---

## The 1.0 shortlist (prioritized, mapped to bog status)

### Tier 1 — robustness & security gaps to close for 1.0

| # | Idea | bog status | Effort |
|---|---|---|---|
| 1 | **Persistent shell state + auto-background-on-timeout** in `LocalShellBackend` | we kill on timeout, no state across calls | M |
| 2 | **`exec-risk` hardening** of the dangerous-command gate (`git -c` retargeting, `sort --compress-program`, wrapper peeling) | partial (`_DANGEROUS_PATTERNS`) | M (security) |
| 3 | **Lifecycle hook bus (multi-event) + "keep-working" Stop gates** | narrow hooks today | M–H |
| 4 | **Session full-text search + `/rewind`** (JSONL truth + SQLite FTS + snapshots) | `/threads`, no search/rewind | M |
| 5 | **Multi-vendor config compat** (ingest Claude/Cursor hooks/skills/rules) | AGENTS.md cascade only | L–M |

### Tier 2 — standout differentiators (pick 1–2 headliners)

| # | Idea | bog status | Effort |
|---|---|---|---|
| 6 | **PTY harness for interactive TUIs** (drive vim/top/REPLs) | none | M–H (unique) |
| 7 | **Voice dictation** (streaming STT) | none | M |
| 8 | **Hybrid local-RAG memory** (FTS5 + vector KNN + decay + MMR over Markdown) | simpler file memory + semantic index | M–H |
| 9 | **Rich rendering** (in-terminal Mermaid + Kitty image/video) | Textual-limited | M |

### Tier 3 — architectural bets (post-1.0 moat)

| # | Idea | Effort |
|---|---|---|
| 10 | **Single leader process** (thin clients share one live agent) | XL |
| 11 | **Deepen sandbox to process-level Landlock + seccomp** (+ bwrap read-deny) | L (security) |

### Tier 4 — cheap polish

- Cost honesty (omit-if-partial + integer ticks).
- Subagent capability lattice (intersect vs role ceiling).
- `monitor` streaming watcher primitive.
- Theme follows OS light/dark live.
- Skill invocation matrix (`user-invocable` × `disable-model-invocation`).
- Headless Anthropic `stream-json` wire-compat.

## Recommendation

Ship **Tier 1** (the difference between "impressive demo" and "trustworthy
1.0") plus **one Tier-2 headliner**. Bank Tier 3 as the post-1.0 moat.

**In progress (this branch):** Tier 1 (#1–#5) then Tier 2 #8 (hybrid memory),
then reassess.
