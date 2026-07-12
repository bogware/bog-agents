# bog-agents-cli — Improvement Plan (Adversarially-Verified Audit Synthesis)

## 1. VERDICT

bog-agents-cli is a mature, feature-dense Textual TUI with real differentiators dcode lacks (Dreamscape, Operator/Butcher/JTBD prompt-routing, Expert Mode rule engine, a resiliency-hardened SDK), but it is **not yet world-class on the fundamentals that a coding-agent CLI is judged by**: remote-MCP authentication is entirely non-functional, reasoning-effort control actively harms modern models, and onboarding assumes tooling is already on PATH. The headline dcode features worth porting cluster into three epics — **MCP OAuth** (the whole `mcp_auth.py` stack: wiring `auth=` into connections, device flow, loopback callback, `/mcp login`, 401-challenge detection), **native reasoning-effort** (`reasoning_effort.py` mapping `/effort` to each provider's real API instead of a `max_tokens=1024`/`temperature` hack), and **goal/rubric persistence** (durable agent-visible objective + acceptance criteria gated by the existing grader). The single clearest onboarding gap is **managed ripgrep** (checksum-verified auto-install so `rg` search runs at full speed on a fresh Windows/CI box), and the cheapest quality wins are **native effort**, **`${VAR}` header interpolation**, and **external-editor compose (ctrl+x)**. The headline CLI *weaknesses* are security-shaped: the primary human-trust surface (`approval.py`) and the MCP viewer render attacker-influenceable strings as **unescaped Rich markup** — an injection + crash-the-HITL-dialog bug — and a malformed HITL reject is silently dropped in headless mode, wedging unattended runs. Underlying all of it, `app.py` has grown to **17,111 lines / 321 methods**, so every new surface must land in `commands/*.py` + controller modules, not the god class.

---

## 2. PORT PLAN

> Note: two audit entries (`ux-effort-native-reasoning`, `effort-native-params`) describe the **same** `reasoning_effort.py` port — treated as one item below. The MCP-OAuth entries form one interdependent epic; `mcp-auth-wiring` is load-bearing for all of them.

### PORT NOW (this pass) — ranked by value ÷ effort

| # | Feature | Value | Effort | Fit |
|---|---------|-------|--------|-----|
| 1 | MCP `${VAR}` header interpolation | med | **S** | clean |
| 2 | Native reasoning-effort (`/effort`) | **high** | M | needs-adapt |
| 3 | External-editor compose (ctrl+x) | med | **S** | clean |
| 4 | Sanitize control chars at display sinks | med | **S** | clean |
| 5 | MCP OAuth epic (wiring → login → device flow → provider registry → loopback → 401-detect) | **high** | L (staged) | mixed |
| 6 | Goal/rubric persistence + generation | **high** | L | needs-adapt |
| 7 | Managed ripgrep auto-install | **high** | L | clean |
| 8 | Env-var registry → config manifest | **high** | M→L (staged) | needs-adapt |

**1. MCP `${VAR}` header interpolation** (`mcp-header-env-interpolation`, S, clean)
- Adds: the simplest authenticated-remote-MCP path — `Authorization: Bearer ${GITHUB_TOKEN}` in `.mcp.json` resolved from vault then env, instead of committing a raw token.
- Files: EDIT `libs/cli/bog_agents_cli/mcp_tools.py` — add `_interpolate_headers(headers, server_name)` scanning `${VAR}` (optionally `${VAR:-default}`), resolve via `vars_store.get_var` → `os.environ`, raise actionable `RuntimeError` on unset; call at the `conn["headers"] = ...` site (~line 471–472). Use `${VAR}` (Claude-Code parity), NOT the install-time `{{VAR}}` templating. TEST: new/extended `tests/unit_tests/test_mcp_tools.py`.

**2. Native reasoning-effort** (`ux-effort-native-reasoning` + `effort-native-params`, M, high value)
- Adds: `/effort` steers each provider's real knob (Anthropic `output_config.effort`, OpenAI `reasoning.effort`, Gemini `thinking_level`, Fireworks/xAI `reasoning_effort`), version-gated per model, with `none/xhigh` levels — replacing the current hack that caps output at 1024 tokens and bumps temperature (actively truncates reasoning models).
- Files: CREATE `libs/cli/bog_agents_cli/reasoning_effort.py` (port near-verbatim onto `ModelSpec`; drop dcode's `openai_codex` provider entry; keep the import-time assert that config keys == provider vocab). EDIT `configurable_model.py` (replace `_EFFORT_LEVEL_SETTINGS` usage ~lines 213–224 with `model_params_for_effort`; keep max_tokens/temperature only as the non-reasoning fallback — never cap reasoning models). EDIT `app.py:_handle_effort_command` (~7791) to accept/validate the expanded vocab against `supported_efforts_for_model`. EDIT `commands/config.py` /effort help + `argument_hint`. Optional CREATE `widgets/effort_selector.py` (ModalScreen). TEST: `tests/unit_tests/test_reasoning_effort.py` (per-model supported sets, per-provider translation, round-trip).
- The receiving plumbing already exists (`CLIContext.model_params` → `configurable_model.py` `**model_params`), so params take effect immediately.

**3. External-editor compose** (`ux-external-editor`, S, clean)
- Adds: ctrl+x pops the input buffer into `$VISUAL`/`$EDITOR` and drops the result back — real relief for long prompts.
- Files: CREATE `editor.py` (port verbatim; rename temp prefix to `bog-agents-edit-`, keep `encoding="utf-8"`). EDIT `app.py` BINDINGS (~768) add `Binding("ctrl+x","open_editor",...,priority=True)` — **priority is required**, TextArea binds ctrl+x to cut — plus `action_open_editor` using `with self.suspend():`. EDIT `ui.py` `show_help()` (help-drift test — same commit). TEST: `tests/unit_tests/test_editor.py`.

**4. Sanitize control chars** (`sanitize-control-chars`, S, clean)
- Adds: closes the outbound ANSI/OSC/DCS + C0/C1 injection gap on displayed untrusted text (MCP errors, subagent output, `non_interactive` raw stdout).
- Files: promote the regex logic in `widgets/chat_input.py` into a public `sanitize_control_chars(text)` in `unicode_security.py`; apply at `non_interactive.py:145` (highest priority — raw `sys.stdout.write`), `tool_display.py:93/159/190`, `mcp_viewer.py`, `local_context.py:66–79`. TEST: mirror dcode's cases (CSI color, OSC 52, DCS, raw 0x9B, NUL).

**5. MCP OAuth epic** (L, staged — this is the marquee capability gap)
Stage in dependency order:
- **5a — Wiring (`mcp-auth-wiring`, P1, load-bearing):** EDIT `mcp_tools.py:_load_tools_from_config` remote branch (~473) to set `conn["auth"] = _resolve_mcp_auth(server_name, server_config)` when non-None (both SSE/StreamableHttp accept `auth: httpx.Auth`). Minimal impl revives the orphaned `oauth_mcp.py` (currently dead code — imported by nothing but its tests). Without this, every OAuth feature below is inert.
- **5b — 401 detection (`mcp-401-challenge-detection`, clean):** CREATE `mcp_auth.py` with the pure httpx functions (`find_oauth_challenge`, `_oauth_resource_challenge`, `find_reauth_required`, `format_login_failure`, `MCPReauthRequiredError`); turn the current opaque 15s startup timeout (P0-D) into an actionable "requires OAuth" message. Drop all SDK provider machinery.
- **5c — `/mcp login` (`mcp-login-command`, P1):** register `login` subcommand spec in `commands/config.py`; thin `_handle_mcp_command` branch delegating to a new `mcp_login_controller.py` (expert_controller pattern — testable without TUI). Loopback callback in a `@work` worker so the event loop isn't blocked.
- **5d — Loopback callback server (`mcp-loopback-callback-server`, clean):** stdlib `ThreadingHTTPServer` on `127.0.0.1:port`, single-use, styled success/error HTML, `threading.Event` capture, headless paste-back fallback. Persist the bound `loopback_port` back into config so the DCR redirect_uri stays stable.
- **5e — GitHub device flow (`mcp-github-device-flow`, clean):** CREATE `mcp_device_flow.py` (RFC 8628 poll loop; `authorization_pending`/`slow_down`) mapping onto bog's `oauth_mcp.OAuthToken`; GitHub constants (`api.githubcopilot.com`). Collapse dcode's provider file into one module.
- **5f — Provider registry (`mcp-oauth-provider-registry`, S, clean):** CREATE `mcp_providers/` (`base.py` ABC, `github.py`, `slack.py`, `_registry.py` with `GenericProvider` last) so a new authenticated server is one small module.
- Reuse throughout: `oauth_mcp.save_token`/`load_stored_token` (already atomic `0600`), `atomic_write_text` + `_secure_owner_only`, `encoding="utf-8"`. Thread the existing-but-unused `state` guard into `exchange_code_for_token(expected_state=…)`.

**6. Goal/rubric persistence** (`goal-tools-persistent` P1 + `goal-rubric-generation`, L, high)
- Adds: durable, agent-visible GOAL + acceptance criteria re-read each turn, `get_goal`/`get_rubric`/`update_goal` tools, completion **gated by the existing grader** — squarely serves bog's long-session positioning.
- SDK: CREATE `libs/bog-agents/bog_agents/middleware/goal_tools.py` (`GoalToolsMiddleware`, persistent `GoalState` channels, three tools via the **bundle** pattern per CLAUDE.md, `GOAL_TOOLS_SYSTEM_PROMPT` in `before_model`); **reuse `RubricMiddleware`'s grader** for the completion gate, don't fork it; register lazily in `middleware/__init__.py` `_LAZY_IMPORTS` (no top-level import); if ordering matters add `requires` ClassVar + update `test_middleware_canonical_order.py` same commit. TEST `tests/unit_tests/middleware/test_goal_tools.py`.
- CLI: CREATE `goal_controller.py` + `goal_rubric.py` (mirror `jtbd.py`'s injected-invoke pattern for one-shot criteria drafting w/ regenerate gate); `/goal` `/rubric` specs in `commands/general.py` (NOT autocomplete.py); thin handlers on `app.py`; wire channels into `_build_cli_context`; headless twin in `headless_commands.py`.

**7. Managed ripgrep** (`managed-ripgrep`, L, clean, high onboarding value)
- Adds: on a fresh Windows/CI box with no `rg`, search runs at full speed instead of the slow Python fallback, zero user action.
- Files: CREATE `managed_tools.py` (pure logic, no Textual) — `prepend_managed_bin_to_path()`, `ensure_ripgrep()`, `_install_ripgrep_sync()` with a **SHA-256-pinned** release table (win .zip / macOS+linux .tar.gz), atomic extract, `0755`. **Gate before any download:** `[tools].auto_install` (default true) + reuse the resiliency work's air-gapped/offline signal — never hang startup on a sealed box; never install unverified (checksum mismatch → `None`, fall through to existing warning). EDIT `main.py` (call prepend before `check_optional_tools`; `@work` install in TUI; keep `format_tool_warning_*` as fallback). EDIT `doctor.py` (~256, managed vs system). TEST `test_managed_tools.py` (mismatch rejects, missing-platform → None, idempotent prepend, auto_install=false / offline short-circuit).

**8. Env-var registry → config manifest** (`env-var-registry` M → `config-manifest` L, staged)
- Adds: the systematic fix for bog's recurring registry-drift bug class (P0-G/P0-H) — one source of truth for every `BOG_AGENTS_*` var and config key.
- Stage 1 (M): CREATE `_env_vars.py` centralizing the 130 scattered `BOG_AGENTS_*` literals (24 files: `app.py`×21, `config.py`×14, `dreamscape/config.py`×13, `agent.py`×12, etc.) + shared `is_env_truthy`/`classify_env_bool`; CREATE `tests/unit_tests/test_env_vars.py` (drift tests: no bare literals, no stale entries, sorted — `encoding="utf-8"` per P0-H). Land registry+failing test first, then the literal sweep to green (two reviewable commits).
- Stage 2 (L): CREATE `config_manifest.py` (`ConfigOption` frozen dataclass w/ `__post_init__` validation, `resolve_scalar` env→toml→default, prefix `BOG_AGENTS_` at `~/.bog-agents/config.toml`; drop dcode's `[interpreter]`/QuickJS section). Derive credential options from `PROVIDER_API_KEY_ENV` + `api_keys` metadata (stays in sync with P0-G). EDIT `headless_commands.py:_cmd_config` (currently a 3-line stub) into real `config`/`config get`/`config show`. The `_env_vars.py` centralization is the gating cost — without it the drift test can only assert a hand-listed set.

### DEFER / SKIP (doesn't fit bog yet)

| Feature | Why defer |
|---------|-----------|
| **Spec-compliant MCP OAuth full** (`mcp-spec-oauth-full`, L) | Requires adopting `mcp.client.auth.OAuthClientProvider` + `TokenStorage` — a separate SDK-provider migration. Ship the minimal homegrown wiring (5a–5f) first; DCR/RFC-9728 discovery/cross-process refresh-lock is the *next* MCP roadmap item, not this pass. |
| **Full token-storage hardening** (`mcp-token-storage-hardening`, P3) | `FileTokenStorage` is coupled to the SDK provider bog doesn't use. Do only the cheap slice now: stamp a schema `version` into `tokens.json` in `oauth_mcp.py` (`{"version":1,"servers":{…}}`) with a fail-closed load guard. Defer expiry sidecar / refresh-lock / per-server files / path-safety until the SDK-provider migration. |
| **ChatGPT/Codex sign-in** (`chatgpt-codex-signin`, L) | Requires bumping `langchain-openai>=1.3.1` and reaching into `chatgpt_oauth` internals; niche (subscription-vs-API-key) vs. the MCP-auth gap that blocks a whole feature class. Revisit after the MCP epic. |
| **Theme system** (`ux-theme-system`, L) | The load-bearing adaptation is surgery on `app.tcss` (remove hardcoded `$primary: #hex` so 54 downstream `$var` refs resolve from the active theme) — real regression risk. High visible value but sequence it after the correctness/auth work. |
| **Skill trust store** (`skill-trust-store`, L) | Deliberately *relaxes* the P1-8 symlink-refusal security posture (adds an allowlist). Worth doing but needs careful HITL design + a security review; not a same-pass drop-in. |
| **Cwd-switch on resume** (`ux-cwd-switch-on-resume`, M) | "switch" can't be a bare `os.chdir` — `self._cwd` is construction-frozen and roots the agent server/filesystem backend; a real re-root is invasive. Ship the *prompt* + stay/abort now if desired, degrade "switch" to a relaunch hint until in-place re-root is built. |
| **Update shadow/throttle** (`update-shadow-and-throttle`, M) | Genuine polish (shadowed-binary hint after uv upgrade, notification throttle, release-age) and bog has the `InstallMethod` scaffolding — but lower urgency than auth/effort. Good early-next-pass item; verify `[project.scripts]` names before hardcoding the shadow check. |

---

## 3. FIX-NOW LIST (this pass)

**SEC-1 — Approval dialog renders unescaped Rich markup (HIGH, P1)** — `widgets/approval.py:292/298/212`
Tool header (`f"[bold]{i+1}. {tool_name}[/bold]"`), description (`f"[dim]{description}[/dim]"`), and the title Static (line 212) are interpolated straight into Rich markup for every non-minimal tool and all MCP tools. Description text is attacker-influenceable (raw file_path, model query, URL, server-supplied MCP name/desc). A value with `[bold red]…[/bold red]` restyles/hides the exact text the human approves; a bare `[/dim]`/`[/]` raises `MarkupError` and **crashes the HITL dialog** (denies approve/reject). Fix: wrap all three in `escape_markup(strip_dangerous_unicode(str(...)))`, mirroring the already-correct minimal path at `approval.py:179`. Regression test: `[/dim]` doesn't raise; `[bold red]evil[/bold red]` renders literally.

**SEC-2 — MCP viewer renders server-controlled strings unescaped (MED, P2)** — `widgets/mcp_viewer.py:44/65/74/87–89/276–279`
`f"  {name} [dim]{description}[/dim]"` and the server header pass fully-untrusted MCP strings to `Static` (markup-parsed). Same crash + injection + trojan-source (bidi/zero-width) exposure as SEC-1. Fix: `rich.markup.escape` at each site (or once at the `MCPToolInfo` boundary, `mcp_tools.py:590`) + strip C0/C1 and bidi-override codepoints. dcode uses `Content.assemble` tuples here specifically because they're never markup-parsed.

**TEST-1 — Escaping fix + tests together (MED, P2)** — `tests/unit_tests/test_approval.py:124`, `test_mcp_viewer.py`
The only escaping test covers the already-safe minimal path; the vulnerable non-minimal description/header path and the MCP viewer have zero injection tests. Land the SEC-1/SEC-2 escaping fixes **with** pilot tests (run **without** `--disable-socket` per the Windows quirk): mount `ApprovalMenu` for `write_file`/`web_search` with description containing `[bold red]x[/bold red]`, bare `[/dim]`, and U+202E — assert mounts without `MarkupError` and plain text preserves the literals. Equivalent `MCPViewerScreen` test. Don't add tests around still-broken code.

**HITL — Malformed reject silently dropped in headless mode (MED, P2)** — `non_interactive.py:944` (recorded at 305–310)
`_process_interrupts()` records a fail-closed reject into `state.hitl_response` but does NOT set `interrupt_occurred`, so either the resume loop never runs (graph left paused; run returns as if complete) or the next iteration's `state.hitl_response.clear()` (line 944) wipes it → `Command(resume={})`. A malformed tool-approval request **wedges** the unattended run instead of being rejected. Fix: add `StreamState.malformed_rejects: dict` (default_factory); write the reject there AND set `interrupt_occurred = True`; in `_run_agent_loop` after the `clear()`+`_process_hitl_interrupts` step, `state.hitl_response.update(state.malformed_rejects); state.malformed_rejects.clear()` (delivered once). Two tests in `test_non_interactive.py`: single malformed → loop issues a `Command(resume=…)` with a reject decision for that id; malformed + valid co-occur → resume dict has decisions for **both**.

**Git branch cache never invalidated (MED, P2)** — `textual_adapter.py:356`
`_get_git_branch()` memoizes per-cwd forever; `_build_stream_config()` stamps `metadata["git_branch"]` on every checkpoint. After a mid-session `git checkout` or WorktreeMiddleware branch, every subsequent checkpoint is tagged with the **original** branch → `/threads` filter, listing, and rewind metadata misattribute threads. Fix: convert to a short TTL (store `(monotonic_ts, branch)`, re-run `git rev-parse --abbrev-ref HEAD` after ~2–3s) — covers shell-tool branch switches that never hit the `/branch` handlers — AND clear the cwd entry in both `/branch` handlers (`app.py` ~7946/~7966) for instant status-bar accuracy. Test alongside `test_textual_adapter.py:198–216`.

**ARCH-1 ratchet (cheap half only, P2)** — `app.py:757`
`BogAgentsApp` is 17,111 lines / 321 methods / 122 handlers and *growing* (+2,358 lines since the deferred refactor snapshot). Do NOT reopen the deferred 3-PR mixin extraction (standing user decision, MEMORY 2026-05-07). Do land the actionable mitigation: a **line-count ratchet test** in `libs/cli/tests` pinning ~17,111 as a ceiling (analogous to the help-drift / canonical-order pins), forcing new surface into `commands/*.py` + controllers.

---

## 4. CLI IMPROVEMENT ROADMAP (NEXT PASS)

**Theme: Input ergonomics**
- External-editor compose (ctrl+x) — if not landed this pass (`editor.py`, `app.py` binding w/ `priority=True`, `ui.py` help).
- Native reasoning-effort selector modal (`widgets/effort_selector.py`) marking current + provider-default level, if the `/effort` command shipped without the interactive picker.

**Theme: Tool rendering & display safety**
- Complete the control-char sanitization sweep across every display sink (`tool_display.py` path branches, `local_context.py`, subagent panels) if only the highest-priority sinks landed.
- MCP viewer: adopt `Content.assemble` tuple rendering (dcode's injection-safe pattern) rather than escape-at-interpolation, as a defense-in-depth follow-up to SEC-2.

**Theme: Onboarding & doctor**
- Managed ripgrep (if deferred) — `managed_tools.py` + `main.py`/`doctor.py` wiring, with `[tools].auto_install` + offline gate.
- `doctor.py` should report managed-vs-system `rg`, and (from update-shadow work) shadowed-binary detection after uv upgrade + release-age display + notification throttling (`update_manager.py`).
- Theme system (`theme.py`, `widgets/theme_selector.py`, `/theme`) — visible personalization gap; sequence after the `app.tcss` `$var` de-hardcoding.

**Theme: MCP auth completion**
- Spec-compliant OAuth: RFC 9728 discovery + Dynamic Client Registration + cross-process refresh-lock (adopt `mcp.client.auth.OAuthClientProvider`) — the "full" tier once the minimal wiring proves out.
- Token-storage hardening beyond the version-envelope: expiry sidecar (cold-start refresh), per-server files, refresh-lock, `client_info`/metadata persistence, `server_name` path-safety.
- Slack provider loopback (port 3118) + team-param capability; ChatGPT/Codex subscription sign-in.

**Theme: Goal/session UX**
- Goal-review Textual panel (`widgets/goal_review.py`) showing objective + rubric + live status, if the SDK/CLI goal-tools shipped without the panel.
- Cwd-switch-on-resume prompt (`widgets/cwd_switch.py`) with an actual in-place re-root of the agent server (reuse the existing rebuild path), not the relaunch-hint degrade.
- Skill trust store (`skill_trust.py` + `/skills trust` family) relaxing P1-8 with resolve-to-self re-verification — after security review.

**Theme: Architecture & god-class**
- Config manifest Stage 2 + wire `settings_screen.py` as the third consumer of `get_config_options()` so runtime / headless / `/settings` share one source of truth.
- With the line-count ratchet in place, begin *opportunistic* extraction: every new `_handle_*_command` lands in a controller module (expert_controller pattern), never on `BogAgentsApp`. The deferred 3-PR mixin split stays deferred until the user reopens it.

**Theme: Tests**
- Extend the injection-test pattern (SEC-1/SEC-2) to any remaining Static-mount sites that interpolate model/server strings.
- Drift tests once `_env_vars.py` lands (no bare `BOG_AGENTS_*` literals; sorted; no stale entries).
- Per-provider effort-translation + supported-set gating tests as new model versions ship.

---

## 5. RISKS

- **Windows async-socket quirk (MEMORY):** the local CLI suite errors all async tests under `--disable-socket`. The SEC-1/SEC-2/TEST-1 widget-mount tests and any TUI-touching test **must run without** `--disable-socket`; compare against the stashed baseline and run new tests without the flag. Don't add `@pytest.mark.asyncio` (asyncio_mode="auto").
- **God-class edit hazards (`app.py`, 17,111 lines):** every port here touches `app.py` (effort handler ~7791, MCP handler, BINDINGS ~768, resume ~16748). Keep handlers thin — delegate to controller modules (`mcp_login_controller`, `goal_controller`, `reasoning_effort`) so logic stays testable without the TUI. Land the line-count ratchet first so these additions don't silently blow past the ceiling.
- **Registry-sync invariants (P0-G/P0-H):** the config-manifest/env-registry work must derive credential options from `PROVIDER_API_KEY_ENV` + `api_keys` metadata (the import-time sync assertion) rather than re-listing keys — otherwise it reintroduces the exact drift it aims to kill. New model providers still require the 4-step registration (model_config → api_keys metadata → pyproject → test).
- **Public-command-surface stability:** new slash commands go in `commands/*.py` (NOT `autocomplete.py` — `command_registry` aggregates); register headless twins in `headless_commands.py` for informational/config commands (`/goal`, `config`, `/mcp` status). Preserve existing handler signatures (`async def _handle_<name>_command(self, command: str) -> None`).
- **Help-drift test:** `ui.show_help()` is hand-maintained with an argparse drift-detection test — any new binding (ctrl+x) or command must update `show_help()` in the **same commit** or the test fails.
- **Middleware ordering (SDK goal-tools):** `graph.py` order is load-bearing and locked by `test_middleware_canonical_order.py`; if `GoalToolsMiddleware` needs ordering vs `RubricMiddleware`, declare `requires` and update the canonical-order test in the same commit. Register via `_LAZY_IMPORTS` only — never a top-level import.
- **Secret-file conventions (Windows):** every OAuth token / state file (`tokens.json`, `chatgpt-auth.json`, loopback config, skill-trust store) must use `atomic_write_text` + `vars_store._secure_owner_only` (bare `chmod` is a Windows no-op) and `encoding="utf-8"` — a single smart quote in a config decodes through cp1252 and crashes the CLI. Never log `OAuthToken` repr.
- **MCP OAuth "unwired" trap:** `oauth_mcp.py` is currently dead code — the loopback server / device flow alone won't make MCP OAuth usable without the `conn["auth"]=` connection-path injection (5a). Land the wiring first or the rest is inert. Verify actual `[project.scripts]` entry-point names and `api.githubcopilot.com` host matching before hardcoding.
- **Managed-ripgrep startup hazard:** the auto-installer must honor `auto_install=false` and the air-gapped/offline signal, and must **never** install an unverified binary (checksum mismatch → return None, fall through to the existing warning) — a network hang on a sealed box at startup is worse than the slow Python search fallback it replaces.