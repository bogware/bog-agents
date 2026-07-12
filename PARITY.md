# bog-agents ⇄ deepagents 0.6.12 — Implementation-Ready Parity Report

> **STATUS (2026-07-11): Waves 1–3 SHIPPED** on `chore/resiliency-hardening`
> (commits 3e0ef8b → 38e7930). bog-agents is now a source-level drop-in for the
> deepagents 0.6.12 public API and co-installable with it. Full SDK unit suite
> green at 2521 passed / 136 skipped / 2 xfailed; daemon 141 passed; CLI
> SDK-integration surface 280 passed. Delivered: co-install dep floors,
> SystemPromptConfig + full export surface, backend rewrite (FileData v2, delete,
> overwrite), the two permission-bypass security fixes, middleware interop
> surface, built-in harness+provider profiles (unbreaks OpenRouter/Codex/Nemotron),
> Bedrock prompt caching, and reachable video-frame read. Wave 4 (satellites —
> QuickJS interpreter, LangGraph Platform deploy CLI, Talon, eval product, and the
> deepagents-code feature ports) is deliberately DEFERRED and should be argued on
> value before starting. The two SECURITY findings in §1 are both fixed and pinned.
>
> Scope note: the confirmed-gap list contains the same gap discovered independently by several dimension auditors under different ids (e.g. `system-prompt-config` / `system-prompt-config-missing`, `recursion-limit-9999` / `recursion-limit-200`, `fs-delete-tool-missing` / `backend-delete` / `delete-tool-missing`, `private-state-field-names` / `subagent-state-schema-and-private-keys` / `subagent-private-state-keys`). This report **merges** those into canonical items and lists the alias ids so nothing is double-implemented.

---

## 1. VERDICT

bog-agents is **not** a drop-in for deepagents 0.6.12 today, but the distance is smaller than the raw gap count suggests: the *architecture* is at parity (same `AgentMiddleware` base, same LangGraph `create_agent` delegation, byte-identical `_messages_delta_reducer` / `DeepAgentState` / `_excluded_middleware` / profile registry / `BASE_AGENT_PROMPT`), and roughly half the confirmed gaps are one-file wiring or one-string prose fixes. The headline breakages are: (a) **bog forked before the deepagents 0.5.0 backend rewrite** — no `LsResult`/`ReadResult`/`GrepResult`/`GlobResult`/`DeleteResult`, no v2 `FileData` (`content: str` + `encoding`), no `delete()` anywhere, `StateBackend`/`StoreBackend` constructors incompatible — which is the single largest and most-coupled wave; (b) **the profile registry ships zero built-in profiles** (`_builtin_profiles.py:148` literally says "This port ships none"), so Anthropic/Codex/Nemotron prompt tuning and the OpenRouter Azure-ignore fix are all absent, and three `HarnessProfile` fields (`general_purpose_subagent`, `tool_description_overrides` on built-ins, per-subagent profile resolution) are parsed but never consumed; (c) **a pile of small but genuinely fatal interop breaks** — `SystemPromptConfig` raises `TypeError`, `FsToolName` doesn't exist, `recursion_limit` is hard-clamped at 1000 (deepagents uses 9,999), user middleware colliding with a built-in name is silently *dropped* instead of replacing it, and `wcmatch<7.0` in `pyproject.toml` makes bog and deepagents **literally un-co-installable in one venv**.

Two gaps are security-relevant and should not wait: filesystem permission rules are **never applied to `ls`/`glob`/`grep` results** (a `deny /secrets/**` rule is trivially bypassed by `grep(pattern="API_KEY")` with no path), and the symlinked-skill-dir refusal exists only on the **sync** `_list_skills` path — the async path used by `ainvoke` has no check at all. On the other side of the ledger, bog is materially *ahead* on ~90 middleware, `FeatureConfig`/`AgentBuilder`/guardrails, `LangSmithMiddleware` (11 tools + OTEL, no upstream counterpart), the daemon, and a far larger CLI — none of that is at risk here, and six previously-suspected gaps were **refuted** (see Appendix).

Net: **Waves 1–3 (SDK parity) is a focused ~3–4 week body of work concentrated in ~20 SDK files**, of which the backend rewrite (Wave 1C) is over half. Wave 4 (satellites: quickjs code-interpreter, deploy CLI, Talon, eval product) is optional-by-value and should be argued separately, not bundled.

---

## 2. WAVES

### Wave 1A — Interop-breaking, core API (graph.py surface) — *do first, unblocks everything*

| id (aliases) | title | one-line fix | effort |
|---|---|---|---|
| `langchain-version-floor-lag` | `wcmatch>=6.0,<7.0` **conflicts** with deepagents' `>=10.1` — pip cannot co-install | bump to `wcmatch>=10.1,<11.0`; raise langchain/core/anthropic floors to 1.3.12 / 1.4.9 / 1.4.8; `uv lock` | S |
| `system-prompt-config` (+`system-prompt-config-missing` ×2) | `SystemPromptConfig` prefix/base/suffix dict → `TypeError` on bog | add `SystemPromptConfig` TypedDict + `_normalize_system_prompt` + `_assemble_prompt_parts`; assembly order prefix→base→suffix→profile-suffix; `base: None` drops base | M |
| `recursion-limit` (`recursion-limit-9999`, `recursion-limit-200`) + `langsmith-metadata-key` | 200-step default, hard clamp at 1000 (upstream 9,999); metadata key `versions` not `lc_versions` | drop the 1000 clamp; `create_deep_agent(max_turns=9_999)` keyword-only; rename to `lc_versions` | S |
| `fs-tool-name-export` (+`missing-public-exports`) | `from deepagents import FsToolName` → ImportError | define `FsToolName` Literal in `middleware/filesystem.py`; register in both `_LAZY_IMPORTS` + `deepagents.py.__all__` | S |
| `user-middleware-replace-semantics` (+`user-middleware-cannot-override-builtin`) | user middleware colliding with a built-in `.name` is **dropped** (keep-first dedup); upstream **replaces in place** | add `_apply_custom_middleware(base, custom, *, core_names)`; use for main stack, GP stack, subagent specs | M |
| `gp-subagent-profile-ignored` (+`gp-subagent-profile-field-ignored`) | `GeneralPurposeSubagentProfile(enabled=False)` is a fully-typed no-op — `task` tool cannot be disabled | consume `_profile.general_purpose_subagent` in `graph.py`; gate GP stack construction + `SubAgentMiddleware` install | M |
| `profile-overrides-not-applied-to-builtins` (+`subagent-harness-profile-not-per-model`) | `tool_description_overrides` never reach built-in tools; subagents inherit the *parent's* profile; profile prompt never reaches subagents | pass `custom_tool_descriptions=` / `task_description=`; resolve `_subagent_profile` per subagent model; apply `_apply_profile_prompt` to subagent + GP prompts | M |
| `shim-signature-types` | `subagents: list[...]` (not `Sequence`), `response_format: ResponseFormat` (no bare-class/dict) → typed callers fail `ty`/`mypy` | annotation-only widening in `deepagents.py` + `graph.py` | S |
| `model-matches-spec-drops-provider` | `model_matches_spec("openai:gpt-5", anthropic_model_named_gpt_5)` → `True` | compare provider via `_normalize_provider` + `_PROVIDER_ALIASES`; add `is_bedrock_model` | S |
| `plugin-entrypoint-group-renamed` | third-party `deepagents.harness_profiles` plugins silently never load | also enumerate the legacy entry-point groups (legacy first, native wins on collision); de-dupe; log | S |

### Wave 1B — Interop-breaking, middleware surface

| id (aliases) | title | one-line fix | effort |
|---|---|---|---|
| `fs-permission-result-filtering` | **SECURITY**: deny rules never filter `ls`/`glob`/`grep` *results* — `grep(pattern="API_KEY")` returns `/secrets/**` hits | add `_filter_*_by_permission` helpers to `permissions.py`; thread `_permissions=` into `FilesystemMiddleware` at all 3 construction sites | M |
| `subagent-state-and-private-keys` (`subagent-state-schema-and-private-keys`, `private-state-field-names`, `subagent-private-state-keys`, `subagent-state-schema-not-forwarded`) | `state_schema` not forwarded to declarative subagents; `PrivateStateAttr` fields (rubric/summarization/skills) leak both ways across the subagent boundary | new `private_state_field_names()`; `SubAgentMiddleware(state_schema=, private_state_keys=)` + rebuilding setter; graph.py collects keys after the full stack is assembled | M |
| `subagents-ctor-and-helpers` (+`subagent-ls-agent-type-tag`) | no public `create_sub_agent`; `SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY` absent so `SubAgent.response_format` is **dropped**; no LangSmith `ls_agent_type="subagent"` tracing context | add the constant + `create_sub_agent` + `_get_subagent_response_format` + `_subagent_tracing_context`; route the legacy raw-spec branch through `create_sub_agent` | M |
| `patch-tool-calls-stale` | never patches `invalid_tool_calls` → dangling-`tool_use` provider rejection; rewrites full history every turn; no null-id guard | iterate `(*tool_calls, *invalid_tool_calls)`; early-`return None` when nothing dangling; skip `id is None` | S |
| `fs-delete` (`fs-delete-tool-missing`, `backend-delete`, `delete-tool-missing`, `composite-delete-coerce-truncated` part) | no `delete` tool and no backend delete anywhere; agents can only `rm` via `execute`, bypassing permissions | add `DeleteResult` + optional `delete/adelete` on `BackendProtocol` + all 5 backends; `delete` tool + `_find_delete_deny_patterns` (recursive-delete deny bypass) + HITL/SafeTools gating | M |
| `fs-middleware-ctor-drift` | `FilesystemMiddleware(tools=…)`, `human_message_token_limit_before_evict=`, `_permissions=` → `TypeError` | add the 3 keyword-only params; `_build_fs_tools_section` regenerates the prompt from the visible tool set | M |
| `summarization-ctor-and-media` | no `DEEPAGENTS_DEFAULT_SUMMARY_PROMPT`, no dict `TriggerClause`, no inline-media offload; `create_summarization_middleware` rejects `summary_prompt`/`trim_tokens_to_summarize`/`token_counter` | port the constants + `TriggerClause` normalization + `_rewrite_data_url_blocks`; widen the factory | L |
| `skills-ctor-and-errors` (`skills-source-labels-and-prompt-param`, `skills-load-errors-and-sources`, `skills-load-errors-missing`) | `sources` rejects `(path,label)` tuples; no `system_prompt=`; no `skills_load_errors` state key; `~/.claude/skills` renders as `**Skills Skills**` | add `SkillSource` alias + `_derive_source_label` + `system_prompt` param + `skills_load_errors` + escaped `<skill_load_warnings>` block | M |
| `memory-html-comments-and-cache` | `MemoryMiddleware(add_cache_control=…, system_prompt=…)` → `TypeError`; HTML comments in AGENTS.md injected raw | add both keyword-only params + `_strip_html_comments` | S |
| `middleware-init-exports` | 10 of 11 rubric/summarization public symbols unimportable from `bog_agents.middleware` | add 10 `_LAZY_IMPORTS` entries + `deepagents.py` re-exports (all symbols already exist in `rubric.py`) | S |
| `acp-set-config-option` | ACP: no `session/set_config_option`, no `config_options`, `new_session` arg-slot drift, `mcp_servers` dropped | port `_build_config_options` / `set_config_option` / `_normalize_new_session_args`; store `_session_mcp_servers` | S |

### Wave 1C — Interop-breaking, **backend rewrite** (one coordinated wave — do NOT piecemeal)

> bog forked before deepagents 0.5.0. These five gaps are facets of one change and must land together or the backends will be internally inconsistent.

| id | title | one-line fix | effort |
|---|---|---|---|
| `protocol-result-dataclasses` | `ls_info`/`grep_raw`/`glob_info` + `read -> str` vs upstream `ls`/`grep`/`glob`/`read -> Result` dataclasses; no grep timeouts, no `truncated` flag | add `LsResult`/`ReadResult`/`GrepResult`/`GlobResult`/`DeleteResult`/`FileData`/`FileFormat` + new method names with upstream's override-detection forwarding; keep legacy names as delegating shims | L |
| `filedata-v2-binary` | `FileData.content` is still `list[str]`; no `encoding`, no base64/binary, no `file_format` knob; `StoreBackend` **hard-rejects** upstream v2 items with `ValueError` | v2 `FileData`; `_normalize_content` (tolerate legacy list + `DeprecationWarning`); `_to_legacy_file_data` for `file_format="v1"`; base64 binary read path + `MAX_BINARY_BYTES` | L |
| `statebackend-config-key-writes` | `StateBackend()` (no args) → `TypeError`; no read-your-writes; **`write` to an existing path errors on bog and overwrites upstream** | make `runtime` optional; resolve via `get_config()` + `CONFIG_KEY_READ/SEND`; align `write` to overwrite | L |
| `storebackend-ctor-and-namespace-runtime` | `StoreBackend(store=…)` → `TypeError`; `NamespaceFactory` takes `BackendContext` not `Runtime` | optional `runtime`, add `store=` kwarg + `get_store()` fallback; `_NamespaceRuntimeCompat` proxy so both old and new factories work | M |
| `utils-helper-parity` | missing ~12 helpers; **two live bugs**: `_python_search` glob `*.py` misses nested files, and Windows backslash rel-paths never match | add `compile_grep_include_glob`, `compile_recursive_glob`, `to_posix_path`, `_get_backend_read_file_type`, `slice_read_response`, `regex_literal_hint`, `_glob_anchor`/`_paths_overlap` (move from `permissions.py`) | M |
| `composite-delete-coerce-truncated` | composite: no delete routing, no `truncated` propagation, `glob_info(pattern, "/src")` **leaks other routes** | fix the glob scoping bug (Step A is standalone, ship it early); add delete routing + truncation OR-ing | M |

### Wave 2 — Missing capability (not interop-breaking, real value)

| id | title | one-line fix | effort |
|---|---|---|---|
| `missing-anthropic-harness-profiles` | no Opus 4.7 / Sonnet 4.6 / Haiku 4.5 prompt tuning (`<use_parallel_tool_calls>`, `<investigate_before_answering>`, `<subagent_usage>`) | 3 new `profiles/harness/_anthropic_*.py` + register in `_builtin_profiles.py` Phase 1 | S |
| `missing-codex-harness-profile` | no OpenAI Codex suffix (Codex emits preambles + stops early without it) | `profiles/harness/_openai_codex.py` (3 model specs) | S |
| `missing-provider-profiles-openrouter-nvidia` | **OpenRouter + reasoning model is broken multi-turn** (Azure routing); no NVIDIA billing header; `openai` `use_responses_api` is hardcoded in `_models.py` instead of a profile (so it's un-overridable) | 3 new `profiles/provider/*.py`; **delete** the `_models.py:208` hardcode | S |
| `missing-nemotron-ultra-harness-profile` | Nemotron 3 Ultra emits text-form tool calls that are never parsed → model unusable as an agent | port the 1812-line `_nvidia_nemotron_3_ultra.py` (12 compat middleware); also extend `middleware/tool_call_parser.py` with `<function=…>` / bare-JSON formats | L |
| `bedrock-prompt-caching` | `BedrockPromptCachingMiddleware` never added → every Bedrock turn pays full input-token price | `_append_prompt_caching_middleware()` at the 3 append sites; optional `langchain_aws` import | S |
| `overflow-clip-missing` | on `ContextOverflowError`, bog retries with the *same* oversized tail → agent wedges permanently | new `middleware/_overflow_clip.py` + extract `middleware/_message_eviction.py`; hook into `summarization.py` | M |
| `human-message-eviction` | a 200k-token pasted payload goes straight to the model | `human_message_token_limit_before_evict=50000` + `TOO_LARGE_HUMAN_MSG` in `filesystem.py` | S |
| `context-hub-backend` (×2 dims) | no LangSmith-Hub-backed versioned agent filesystem | new `backends/context_hub.py` adapted to **bog's** protocol shapes (not upstream's) | M |
| `langsmith-sandbox-backend` | `from bog_agents.backends import LangSmithSandbox` → ImportError; CLI's `LangSmithBackend` lacks SDK-native read/write (ARG_MAX) + CRLF normalization | promote to `backends/langsmith.py`; CLI keeps `LangSmithBackend = LangSmithSandbox` alias | M |
| `basesandbox-parity` | no capture-offload, no ARG_MAX-safe edit, no binary read, no async overrides, missing `MAX_BINARY_BYTES`/`MAX_OUTPUT_BYTES`/`TRUNCATION_MSG` constants (import-time failure for ported subclasses) | port constants + `execute_with_offload` + `_edit_via_upload` + native async | L |
| `skill-trust-store-missing` | **SECURITY (async bypass)**: symlink refusal exists only in sync `_list_skills`; `_alist_skills` has none. Plus no trust store → symlinked skill repos silently don't load | (A) extract `_filter_skill_dirs`, call from both paths — **ship immediately**; (B) `libs/cli/.../skills/trust.py` + `skills trust` commands | L |
| `video-read-missing` | no video-frame `read_file` path | new `middleware/video_reader.py` + `[video]` extra | L |
| `no-api-deprecation-module` | no `_api/deprecation.py`; `model=None` never warns | port the module; decorate `get_default_model`; migrate the 2 hand-rolled `warnings.warn` sites | S |
| `route-host-path-prompt-missing` | with `CompositeBackend` + `execute`, the model writes shell commands against virtual paths that don't exist on the host | `_route_host_path_prompt(backend)` appended to the FS prompt | M |
| `builtin-remember-skill-missing` | no built-in `remember` skill (SDK-only agents get no memory-capture guidance) | `libs/cli/.../built_in_skills/remember/SKILL.md` from the existing `app.py:630` `REMEMBER_PROMPT` (de-dupe) | S |

### Wave 3 — Stale drift / prose quality (cheap, high signal-to-noise)

| id | title | one-line fix | effort |
|---|---|---|---|
| `skills-prompt-read-limit-hint` | prompt lacks `limit=1000` hint → **the model reads only the first 100 lines of its own 399-line `skill-creator` skill** | add the hint to `SKILLS_SYSTEM_PROMPT` + `ENHANCED_SKILLS_SYSTEM_PROMPT` | S |
| `skills-allowed-tools-yaml-list` | YAML-list `allowed-tools:` silently dropped; comma-without-space also broken | extract `_parse_allowed_tools`; **2 existing tests lock in the bug and must be flipped** | S |
| `skill-load-failure-silent` | non-`file_not_found` SKILL.md errors swallowed with no log | `_skill_metadata_from_response` helper, warn on non-`file_not_found` | S |
| `memory-trust-and-verification-block` | no prompt-injection guard on `<agent_memory>` (AGENTS.md is attacker-reachable in any clone); "FIRST, IMMEDIATE action" wording makes the agent abandon the user's task | add the Trust-and-verification bullets; soften urgency; regenerate the prompt snapshot | S |
| `read-write-file-descriptions-stale` | `read_file` examples use a **non-existent** param name (`path`, not `file_path`); `write_file` typo "create the a new file" | doc-only fix; keep bog's create-only semantics (do NOT adopt upstream's overwrite wording) | S |
| `grep-description-literal-hardening` | model keeps emitting regex into a literal matcher and silently gets 0 results | template the description with the `NOT regex` / no-`\|`-alternation bullets + conditional `rg` fallback | S |
| `filesystem-prompt-static` | prompt always advertises 6 tools and names `/large_tool_results` even when `artifacts_root` is set | template it; render from the visible tool set + resolved artifacts root | M |
| `subagent-prompt-final-message` | subagents lost "the caller only sees your final assistant message" | append it to `DEFAULT_SUBAGENT_PROMPT` (keep bog's anti-fabrication block) | S |
| `task-tool-text-typos` | unbalanced `</example_agent_description>`, missing space in `` `task`tool `` | 3 string edits | S |
| `orphan-base-prompt-md` (=`dead-base-prompt-md`) | `bog_agents/base_prompt.md` is dead and already drifted (booby trap) | `git rm` it + a regression guard test | S |

### Wave 4 — Satellites / optional ports (argue value before committing)

| id | title | one-line fix | effort |
|---|---|---|---|
| `partners-quickjs-code-interpreter` | **biggest capability gap**: no JS REPL / programmatic tool calling / in-REPL subagent dispatch | new `libs/partners/quickjs/` (auto-registers via root Makefile glob). **Blocking spike: does `quickjs-rs` publish a Windows wheel?** | L |
| `deploy-cli` (`langgraph-platform-deploy`, `no-langsmith-deploy-cli`, `deploy-cli-missing`) | no `init`/`deploy`/`agents`/`mcp-servers`; no portable `agent.json` project format | **Recommend: port only the project format + loader** (`bog-agents --project ./dir`); defer the LangSmith-proprietary `/v1/deepagents/*` client | L |
| `evals-suite-and-datasets` + `no-langsmith-native-evals` | no `bog-agents-evals` CLI, no dataset corpus, no `evaluate()`/openevals bridge | `libs/harbor/bog_agents_harbor/evals/` + `bog_agents/evals/langsmith.py` | M |
| `harbor-langsmith-module-stale` | script-only fork; module-level `HEADERS` with a `None` api-key; `_extract_reward` throws on timed-out agents | extract `bog_agents_harbor/langsmith.py`; add `ensure_dataset`, key-resolution chain, `(reward, comment)` tuple | M |
| `dcode-event-bus` | no external event ingress into a live TUI session | `libs/cli/.../event_bus.py` (+ **Windows loopback-TCP fallback — `start_unix_server` doesn't exist on win32**) | M |
| `dcode-config-manifest` | config surface can drift from what the app reads (same class as P0-G) | `libs/cli/.../config_manifest.py` + `resolve_scalar` shared by TUI and headless | M |
| `dcode-provider-oauth-and-mcp-device-flow` | no ChatGPT/Codex sign-in; GitHub remote MCP **cannot authenticate at all** (needs RFC 8628 device flow) | `mcp_providers/` registry + `run_device_flow` in `oauth_mcp.py` | M |
| `dcode-goal-rubric` | no persistent agent-visible goal/rubric | `goal_state.py` / `goal_rubric.py` / `goal_tools.py` + `/goal` `/rubric` | S |
| `dcode-extras-and-managed-tools` | no `/install`; `rg` assumed on PATH (absent on a fresh Windows box) | `extras_info.py` + `managed_tools.py` (pinned checksum-verified ripgrep) | S |
| `talon-channels-runtime` | no inbound conversational channel host (WhatsApp/Telegram), agent-facing cron tools, voice | new `libs/talon/` reusing the daemon's `store.py` — **do not fork the scheduler** | L |
| `partners-modal-vercel` | **Modal and Runloop already exist (refuted)** — only Vercel is missing | `libs/cli/.../integrations/vercel.py` + factory registration | M |

---

## 3. FILE PLAN

Deduplicated across all waves. `libs/bog-agents/` = SDK.

### 3.1 CREATE — SDK

| file | contents |
|---|---|
| `bog_agents/_api/__init__.py` | empty / docstring |
| `bog_agents/_api/deprecation.py` | re-export `deprecated`, `LangChainDeprecationWarning`, `suppress_langchain_deprecation_warning`; `warn_deprecated()` (stacklevel fix); `reset_deprecation_dedupe()` |
| `bog_agents/middleware/_private_state.py` | `private_state_field_names(*schemas) -> frozenset[str]`, `_has_marker()` (must recurse — bog uses **both** `NotRequired[Annotated[…]]` and `Annotated[NotRequired[…]]`). **Do NOT reuse `_state.py`** — that name is taken by the unrelated `MiddlewareState` lock holder |
| `bog_agents/middleware/_message_eviction.py` | extract `_offload_tool_message_content` / `_aoffload_tool_message_content` / `_extract_text_from_message` / `_build_evicted_content` out of `filesystem.py` (they exist there inlined — see refuted `message-eviction-module`); `filesystem.py` delegates |
| `bog_agents/middleware/_overflow_clip.py` | `_clip_overflow_tail`, `_aclip_overflow_tail`, `_derive_overflow_clip_threshold_tokens`, `_find_tail_tool_message_batch`, `_build_tool_call_index`, `_slice_read_file_tm`, `_read_file_original_path` |
| `bog_agents/middleware/video_reader.py` | `extract_video_frames`, `VideoExtractionError`, `video_dependencies_available`, `MISSING_VIDEO_HINT`, `MAX_VIDEO_*` caps (name it `video_reader.py` to match the `pdf_reader.py` sibling) |
| `bog_agents/backends/context_hub.py` | `ContextHubBackend` on **bog's** protocol (`read -> str`, `ls_info`, `grep_raw`, `glob_info`), `_URL_COMMIT_SUFFIX_RE`, `get_linked_entries()`, `has_prior_commits()`; lazy `langsmith` import |
| `bog_agents/backends/langsmith.py` | `LangSmithSandbox(BaseSandbox)` — `enable_capture_offload=True`, SDK-native `write()` (ARG_MAX) via `_write_preflight`, SDK-native `read()` with **CRLF normalization**, base64 binary routing, size caps |
| `bog_agents/profiles/harness/_anthropic_opus_4_7.py` | `_SYSTEM_PROMPT_SUFFIX` (5 blocks) + `register()` → `anthropic:claude-opus-4-7` |
| `bog_agents/profiles/harness/_anthropic_sonnet_4_6.py` | 3 universal blocks → `anthropic:claude-sonnet-4-6` |
| `bog_agents/profiles/harness/_anthropic_haiku_4_5.py` | 3 universal blocks → `anthropic:claude-haiku-4-5` |
| `bog_agents/profiles/harness/_openai_codex.py` | `_CODEX_MODEL_SPECS` (gpt-5.1/5.2/5.3-codex) + suffix |
| `bog_agents/profiles/harness/_nvidia_nemotron_3_ultra.py` | 12 compat middleware + suffix + `read_file` override + `register()` across 8 specs |
| `bog_agents/profiles/provider/_openai.py` | `ProviderProfile(init_kwargs={"use_responses_api": True})` |
| `bog_agents/profiles/provider/_nvidia.py` | `X-BILLING-INVOKE-ORIGIN: BogAgents` header factory |
| `bog_agents/profiles/provider/_openrouter.py` | `check_openrouter_version()` (>=0.2.0), `openrouter_provider={"ignore":["azure"]}`, app_url/app_title, `BOG_AGENTS_OPENROUTER_ALLOW_AZURE` |
| `bog_agents/evals/langsmith.py` *(W4)* | `dataset_from_langsmith`, `push_report_to_langsmith` (lazy `langsmith` import) |

### 3.2 EDIT — SDK core

**`bog_agents/graph.py`** — the single most-touched file. Changes:
1. `SystemPromptConfig` TypedDict + `_PROMPT_SEPARATOR` + `_assemble_prompt_parts` + `_normalize_system_prompt`; replace the fixed assembly at **:1151-1169** with prefix→base→suffix→`_profile.system_prompt_suffix`; **split `_apply_profile_prompt`'s base-replacement from its suffix-append** so `base: None` doesn't also drop the profile suffix; decide `_PROVENANCE_LOOP_PROMPT` placement explicitly.
2. Widen `system_prompt` (**:331**) and `subagents`/`response_format` annotations (**:333/:336**).
3. `_apply_custom_middleware(base, custom, *, core_names)` next to `_dedup_middleware_by_name` (**:204**); replace `agents_middleware.extend(user_middleware)` (**:1087**), the subagent-spec extend (**:642**), and add GP inheritance (**:560-583**). Hoist `user_middleware` resolution above the GP block.
4. GP profile: import `GeneralPurposeSubagentProfile`; `gp_profile = _profile.general_purpose_subagent or …`; wrap **:559-593** in `if _build_gp:`; honor `description`/`system_prompt`; gate `SubAgentMiddleware` append (**:1078**) on `all_subagents`; fix the merge at **:680-686**. **Seed/relax the exclusion-coverage matched-sets when GP is suppressed** (this is the one place a naive patch breaks a green test).
5. Profile wiring: `FilesystemMiddleware(…, custom_tool_descriptions=_profile.tool_description_overrides)` at **:562/:629/:1077**; `SubAgentMiddleware(…, task_description=_profile.tool_description_overrides.get("task"))` at **:1078**; `_apply_profile_prompt` on GP + declarative subagent prompts (**:673**).
6. Per-subagent profile: capture the raw model spec string before `resolve_model` (**:615**), `_subagent_profile = _harness_profile_for_model(...)`, replace every `_profile` in the loop (:629/:641/:643/:646/:668), add **per-subagent** `_sub_matched_classes`/`_sub_matched_names` coverage sets.
7. `_create_bedrock_prompt_caching_middleware()` + `_append_prompt_caching_middleware()`; use at **:577/:646/:1119**; widen the `user_supplied_prompt_caching` guard (**:698**) with a duck-typed class-name check.
8. `recursion_limit`: drop the 1000 clamp (**:1192**); `"versions"` → `"lc_versions"` (**:1195**).
9. `SubAgentMiddleware(…, state_schema=state_schema)`; bind the instance; after the **fully-assembled** middleware list, `sub_agent_mw.private_state_keys = private_state_field_names(*state_schemas)`.
10. `@deprecated` on `get_default_model` (**:190**) + `_build_default_model()` twin (keep `resolve_model` so `BOG_AGENTS_MODEL_READ_TIMEOUT` isn't regressed); `warn_deprecated` on `model=None` (**:515-528**) and in `_resolve_feature_config` (**:304-319**).
11. Docstring updates at :382/:427/:432/:433/:482.

**`bog_agents/__init__.py`** — `_LAZY_IMPORTS` += `SystemPromptConfig`, `FsToolName`, `DEFAULT_MAX_TURNS`, `DeleteResult`, `FileData`, `FileFormat`, `SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY`, `create_sub_agent`. **No top-level imports** (lazy-load convention).

**`bog_agents/deepagents.py`** — re-export `SystemPromptConfig`, `FsToolName`, `create_sub_agent`, `SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY`, the 10 rubric symbols, `DEEPAGENTS_DEFAULT_SUMMARY_PROMPT`, `TriggerClause`, `SummarizationMiddleware`, `create_summarization_middleware`, `SkillSource`, `MEMORY_SYSTEM_PROMPT`, `DeleteResult`/`DeleteSchema`, `LsResult`/`ReadResult`/`GrepResult`/`GlobResult`/`FileData`/`FileFormat`, `StateBackend`/`StoreBackend`/`CompositeBackend`/`BaseSandbox`/`LangSmithSandbox`, `MAX_BINARY_BYTES`/`MAX_OUTPUT_BYTES`/`TRUNCATION_MSG`. Add `max_turns: int = 9_999` keyword-only to `create_deep_agent` and forward it. Widen `system_prompt`/`subagents`/`response_format`/`skills` annotations.

**`bog_agents/_models.py`** — `_PROVIDER_ALIASES`, `_BEDROCK_PROVIDERS`, `_BEDROCK_MODEL_CLASSES`, `_normalize_provider`, `is_bedrock_model`; rewrite `model_matches_spec` (**:310**) to compare provider; **delete the hardcoded `use_responses_api` at :208-209** (it makes the documented profile override permanently dead); tighten `get_model_provider`'s bare-`except`.

### 3.3 EDIT — SDK middleware

| file | changes |
|---|---|
| `middleware/filesystem.py` | `FsToolName` + `_FS_TOOL_ORDER` + `_ALL_FS_TOOL_NAMES` + `_FS_TOOL_DESCRIPTION_LINES` + `_build_fs_tools_section`; template `FILESYSTEM_SYSTEM_PROMPT` (`{tool_header}`, `{tool_descriptions}`, `{large_tool_results_prefix}`) but keep the module constant pre-rendered; `__init__` += `tools=`, `human_message_token_limit_before_evict=50000`, `_permissions=`; `custom_tool_descriptions: Mapping`; `DeleteSchema` + `DELETE_TOOL_DESCRIPTION` + `_create_delete_tool` (registered after `edit_file`); add `"delete"` to `_WRITE_CLASS_TOOL_NAMES`; `TOO_LARGE_HUMAN_MSG` + `_build_truncated_human_message` + `_check_eviction_needed`/`_apply_eviction_and_truncate` in **both** `wrap_model_call` and `awrap_model_call`; result-filter calls in `ls`/`glob`/`grep` (sync+async) + defense-in-depth deny checks in read/write/edit; `_route_host_path_prompt`; `_get_read_file_type` dispatch + video branch + `_move_media_results_after_tool_results`; `_resolve_artifacts_root` shared with `_artifact_path`; grep/read/write description rewrites; `regex_literal_hint` on zero-match grep; `GLOB_TIMEOUT` 20→10 decision; `max_execute_timeout` 7200→3600 decision |
| `middleware/permissions.py` | `_filter_paths_by_permission`, `_filter_file_infos_by_permission`, `_filter_grep_matches_by_permission`, `_apply_permissions_to_ls_results`, `_apply_permissions_to_glob_results` (interrupt-mode entries pass through **unfiltered**); `"delete": "write"` in `_DEFAULT_FS_TOOL_OPS` + `_FS_TOOL_PATH_ARGS`; `_wildcard_delete_overlap` + `_find_delete_deny_patterns`; re-bind `_glob_anchor`/`_paths_overlap`/`to_posix_path` from `backends/utils.py` |
| `middleware/subagents.py` | `SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY` (**identical literal** — it's a wire key); `create_sub_agent()` (public); `_get_subagent_response_format`; `_subagent_tracing_context()` wrapping both `task` and `atask` invocations (`ls_agent_type="subagent"`, **splat the existing context through**); `SubAgentMiddleware.__init__` += `state_schema=`, `private_state_keys=` + a **rebuilding** `private_state_keys` setter; filter private keys at both **:443** and **:457**; forward `state_schema` into the `create_agent(…)` calls in `_get_subagents` and `_get_subagents_legacy`; route the raw-spec branch through `create_sub_agent` (fixes the dropped `response_format`); `DEFAULT_SUBAGENT_PROMPT` += "only sees your final assistant message"; fix `` `task`tool `` and `</example_agent_descriptions>`; blank lines before 4 lists in `TASK_SYSTEM_PROMPT`; rewrite the stale `_EXCLUDED_STATE_KEYS` comment |
| `middleware/async_subagents.py` | honor spec `response_format`; accept `private_state_keys`; wrap invocations in `_subagent_tracing_context` |
| `middleware/summarization.py` | `_MEDIA_REFERENCE_SUMMARY_PROMPT` + `DEEPAGENTS_DEFAULT_SUMMARY_PROMPT` (+ `BOG_DEFAULT_SUMMARY_PROMPT` alias); `TriggerClause` + local dict-clause normalization + fix `SummarizationToolMiddleware`'s eligibility check (**:1390**); `_token_counter_accepts_tools` (collapse the 3 try/except probes); media offload (`_rewrite_data_url_blocks` → `upload_files`, `_OFFLOAD_FAILED_PLACEHOLDER`, must survive `StateBackend`'s `NotImplementedError`); overflow-clip hook (`overflow_triggered` → `_clip_overflow_tail` → `messages` state write with **original ids preserved**); `_large_tool_results_prefix` ctor param; widen `create_summarization_middleware` (**:1092**) |
| `middleware/patch_tool_calls.py` | rewrite `before_agent`: iterate `(*tool_calls, *invalid_tool_calls)`, precomputed `answered_ids`, early `return None`, `id is None` skip, distinct "malformed or truncated" message |
| `middleware/skills.py` | `SkillSource` + `_validate_tuple_source` + `_source_path` + `_derive_source_label` (`built_in_skills`→`Built-in`, bare `skills` leaf→climb to parent); `__init__(… sources: Sequence[SkillSource], system_prompt: str \| None = SKILLS_SYSTEM_PROMPT)` + `self.source_labels`; `skills_load_errors` on `SkillsState`/`SkillsStateUpdate`; `_list_skills_with_errors`/`_alist_skills_with_errors` (bog has **no** `LsResult.error` — synthesize via try/except around `ls_info`); `_format_skills_load_warnings` (html-escape + json-encode + caps + untrusted-diagnostics preamble) + a `{skills_load_warnings}` slot; `_parse_allowed_tools` (YAML list + comma-no-space); `_skill_metadata_from_response` (warn on non-`file_not_found`); **`_filter_skill_dirs` called from BOTH `_list_skills` and `_alist_skills`** (closes the async symlink bypass); `limit=1000` prompt hint |
| `middleware/enhanced_skills.py` | accept `Sequence[SkillSource]`; `limit=1000` hint in `ENHANCED_SKILLS_SYSTEM_PROMPT` |
| `middleware/memory.py` | `_HTML_COMMENT_RE` + `_strip_html_comments` (applied **after** `_decode_and_bound`, so a comment can't hide a forged `</agent_memory>`); `__init__` += `add_cache_control=False`, `system_prompt=MEMORY_SYSTEM_PROMPT`; `_format_agent_memory(contents, template)`; `modify_request` identity short-circuit; `MEMORY_SYSTEM_PROMPT` += Trust-and-verification block, soften "FIRST, IMMEDIATE action", restore the blank line before `</agent_memory>`; export `MEMORY_SYSTEM_PROMPT` |
| `middleware/rubric.py` | none (symbols already exist) — export-only |
| `middleware/__init__.py` | `_LAZY_IMPORTS` += `FsToolName`, `SkillSource`, `MEMORY_SYSTEM_PROMPT`, `DEEPAGENTS_DEFAULT_SUMMARY_PROMPT`, `TriggerClause`, `create_summarization_middleware`, `create_sub_agent`, `SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY`, and the 10 rubric symbols (`GRADER_SYSTEM_PROMPT`, `RUBRIC_GRADER_MESSAGE_SOURCE`, `CriterionEval`, `CriterionFail`, `CriterionPass`, `GraderResponse`, `GraderVerdict`, `RubricEvaluation`, `RubricResult`, `RubricState`) |
| `middleware/tool_call_parser.py` | `<function=NAME>` / `<function><name>` / bare-JSON formats (needed by Nemotron) |

### 3.4 EDIT — SDK backends (Wave 1C)

| file | changes |
|---|---|
| `backends/protocol.py` | `LsResult`, `ReadResult`, `GrepResult(truncated)`, `GlobResult(truncated)`, `DeleteResult(error, path, **files_update**)`, `ExecuteOffloadResult`, `FileData` (v2), `FileFormat`; `FILE_NOT_FOUND`/`PERMISSION_DENIED`/`IS_DIRECTORY`/`INVALID_PATH`, `DEFAULT_GREP_TIMEOUT=15`, `ASYNC_GREP_TIMEOUT=35`; new `ls`/`grep`/`glob`/`delete`/`adelete` methods with **override-detection forwarding** to the legacy names; `_supports_delete`, `_resolve_backend`; widen `glob_info(path: str \| None = None)` |
| `backends/utils.py` | `_normalize_content` (tolerate legacy `list[str]` + DeprecationWarning) and **rewrite `file_data_to_string`/`create_file_data`/`update_file_data` to v2 str content**; `_to_legacy_file_data`; `slice_read_response`; `compile_grep_include_glob`, `compile_recursive_glob`; `to_posix_path`, `_glob_anchor`, `_paths_overlap` (moved in from `permissions.py`); `_EXTENSION_TO_FILE_TYPE`, `_get_file_type`, `_get_backend_read_file_type`, `MAX_BINARY_BYTES`, `MAX_VIDEO_INPUT_BYTES`; `_looks_like_regex`/`regex_literal_hint`; `_relative_to_root`; **fix `enumerate(file_data["content"])` in `_grep_search_files`/`grep_matches_from_files`** (would iterate characters on v2) and `file_data["modified_at"]` → `.get(...)` |
| `backends/state.py` | optional `runtime`; `_get_config`/`_read_files(fresh=True)`/`_send_files_update` via `CONFIG_KEY_READ`/`CONFIG_KEY_SEND`; `file_format="v2"` + `_prepare_for_storage`; **`write` overwrites instead of erroring**; `delete` (exact key + `path + "/"` prefix, `None` sentinels); implement `upload_files`; fix `len("\n".join(...))` size computations |
| `backends/store.py` | optional `runtime` + `store=` kwarg + `get_store()` fallback; `_NamespaceRuntimeCompat`; retype `NamespaceFactory` to take `Runtime`; **rewrite `_convert_store_item_to_file_data` to accept `str` and `list[str]` and optional timestamps** (currently `raise ValueError` on v2); `file_format`; `delete`/`adelete`; align `write` to overwrite; fix size computations |
| `backends/filesystem.py` | `delete`/`adelete` (`shutil.rmtree`/`unlink` through `_resolve_path`); `ls`/`grep`/`glob` Result twins; binary base64 read path; `DEFAULT_GREP_TIMEOUT` instead of hardcoded 30; **bound `_python_search` and fix its two glob bugs** (`*.py` misses nested files; Windows backslash never matches); `truncated` reporting |
| `backends/composite.py` | Result-returning twins that merge `error`/`truncated` across routes; `delete`/`adelete` with route remap + unsupported-backend error; **fix `glob_info` route leakage** for a non-root `path` |
| `backends/sandbox.py` | `MAX_BINARY_BYTES`/`MAX_OUTPUT_BYTES`/`TRUNCATION_MSG`; `enable_capture_offload` + `execute_with_offload`/`aexecute_with_offload`; `_write_preflight`; `_edit_inline` vs `_edit_via_upload` (`_EDIT_INLINE_MAX_BYTES=50_000`); `delete`; binary read; native async overrides (refactor command construction into `_build_*_cmd`/`_parse_*_output` first) |
| `backends/__init__.py` | export `LsResult`, `ReadResult`, `GrepResult`, `GlobResult`, `DeleteResult`, `FileData`, `FileFormat`, `FileInfo`, `GrepMatch`, `BaseSandbox`, `ContextHubBackend`, `LangSmithSandbox`, `MAX_BINARY_BYTES`, `MAX_OUTPUT_BYTES`, `TRUNCATION_MSG`, `DEFAULT_GREP_TIMEOUT`, `ASYNC_GREP_TIMEOUT`, `supports_delete` |

### 3.5 EDIT — SDK profiles / packaging

- `bog_agents/profiles/_builtin_profiles.py` — Phase 1: import + `register()` the 3 Anthropic, Codex, Nemotron harness modules and the 3 provider modules (before `_invoke_profile_plugins`, inside the existing try/rollback); add `_LEGACY_PROVIDER_PROFILE_GROUP = "deepagents.provider_profiles"` / `_LEGACY_HARNESS_PROFILE_GROUP = "deepagents.harness_profiles"` and invoke them **first** (native wins on collision) with a de-dupe guard + `logger.info` breadcrumb; rewrite the module docstring and the `_BOOTSTRAP_HARNESS_KEYS` docstring ("This port ships none" becomes false).
- `bog_agents/profiles/harness/harness_profiles.py` — normalize the provider half of the lookup key with `_normalize_provider` (:1296-1319); docstring at :792-795 becomes true once the subagent/GP prompt overlay lands.
- **DELETE** `bog_agents/base_prompt.md`.
- `libs/bog-agents/pyproject.toml` — **`wcmatch>=10.1,<11.0`** (the co-install blocker); `langchain>=1.3.12`, `langchain-core>=1.4.9`, `langchain-anthropic>=1.4.8`; add `packaging>=24.0`; new extras `video`, `hub`/`langsmith`, `langsmith-sandbox`, `evals` (openevals); confirm `bog_agents._api` is packaged. Regenerate `libs/bog-agents/uv.lock`; `make lock-check` at root.

### 3.6 EDIT/CREATE — satellites

- `libs/acp/bog_agents_acp/server.py` — `_build_config_options`, `set_config_option`, `_normalize_new_session_args`/`_is_additional_directories`/`_is_mcp_servers`, `models=` ctor param, `_session_mcp_servers`/`_session_additional_directories`, `AgentSessionContext.model`, **move the checkpointer guard out of the `if self._agent is None` block** (latent bug), hoist cwd into `_reset_agent`. CREATE `bog_agents_acp/_version.py`. EDIT `libs/acp/pyproject.toml` (`agent-client-protocol>=0.10.1`) + `uv.lock`. Add `tests/test_model_switching.py`. **Side-finding to fold in: `_handle_interrupts` auto-approves allowlisted `execute` with no dangerous-metacharacter guard — `git status; rm -rf /` auto-approves.**
- `libs/cli/bog_agents_cli/integrations/langsmith.py` — delete the local backend body, `from bog_agents.backends import LangSmithSandbox`, keep `LangSmithBackend = LangSmithSandbox`.
- `libs/cli/bog_agents_cli/agent.py` — emit labelled skill-source tuples (fixes multiple `**Skills Skills**` headings); auto-enable the tool-call parser for `nvidia`, not just `ollama`.
- `libs/cli/bog_agents_cli/skills/{trust.py,load.py,commands.py}` + `commands/config.py` + `headless_commands.py` — skill trust store, `load_skill_content(allowed_roots=)`, `skills trust list|revoke|clear`.
- `libs/cli/bog_agents_cli/built_in_skills/remember/SKILL.md` (CREATE) + de-dupe `app.py:630 REMEMBER_PROMPT`.
- Wave 4 only: `libs/cli/bog_agents_cli/{deploy/,event_bus.py,config_manifest.py,config_controller.py,install_controller.py,extras_info.py,managed_tools.py,goal_*.py,auth_controller.py,mcp_providers/,integrations/vercel.py}`; `libs/partners/quickjs/`; `libs/talon/`; `libs/harbor/bog_agents_harbor/{langsmith.py,evals/}` + `datasets/`. Each needs its `main.py` argparse registration **and** `ui.show_help()` (hand-maintained, drift-tested against argparse).

---

## 4. RISKS

**Tests that WILL fail and must be updated in the same commit** (these are the tripwires, not surprises):

1. `tests/unit_tests/test_middleware_canonical_order.py` — breaks in **three** ways: (a) `names[-1] == "AnthropicPromptCachingMiddleware"` becomes wrong once Bedrock caching appends a second tail entry (rewrite as "the tail is `[Anthropic]` or `[Anthropic, Bedrock]`"); (b) the GP-disabled stack omits `SubAgentMiddleware`; (c) `_apply_custom_middleware` must **preserve** positions — add an assertion that a same-named override sits at the built-in's original index. Everything else (Summarization < PromptCaching, StreetSweeper, Memory) stays valid.
2. `tests/unit_tests/test_graph_upstream_parity.py:70-71` — currently **asserts the bugs** (`recursion_limit == 200`, `metadata["versions"]`).
3. `tests/unit_tests/middleware/test_skills_middleware.py:440,458` — currently **assert `allowed_tools == []`** for YAML lists, i.e. they lock in the drop-on-the-floor bug.
4. `tests/unit_tests/test_middleware.py::TestPatchToolCallsMiddleware::test_no_missing_tool_calls` (:1650) — asserts `is not None`, locking in the always-rewrite bug.
5. `tests/unit_tests/backends/test_state_backend*.py`, `test_store_backend*.py`, `test_composite_backend*.py` — assert `files_update` on Write/EditResult and the write-to-existing **error**; both flip in Wave 1C.
6. `tests/unit_tests/smoke_tests/snapshots/system_prompt_*.md` — regenerate for the memory Trust block, the FS prompt templating, and the grep/read/write description rewrites.
7. `tests/unit_tests/test_lazy_import_health.py` — asserts `<30` modules loaded on `import bog_agents.middleware`. **Any `TYPE_CHECKING`-guard slip on the 10 new rubric exports, or a top-level `langchain_aws`/`langsmith`/`av` import, blows this.**
8. `tests/unit_tests/profiles/test_profiles.py` — may assert the built-in registry is empty; `_has_any_harness_profile()` (`harness_profiles.py:1031`) subtracts `_BOOTSTRAP_HARNESS_KEYS`, so adding built-ins must **not** flip the log level at :1329 — assert this.

**Behavior changes that are not bugs but are real** (land them deliberately, call them out in the commit):

- **`StateBackend.write` flips from error-on-exists to overwrite.** This is upstream-correct but is the single most user-visible semantic change in the plan. `StoreBackend.write` must flip in the same commit or the two backends disagree.
- **`max_turns` default 200 → 9,999 on the `create_deep_agent` shim only.** Do **not** change `create_agent`'s native default (existing bog users rely on the safety cap); the fix is to remove the *hard clamp* at 1000 and default the shim.
- **`SKILLS_SYSTEM_PROMPT` gains a `{skills_load_warnings}` slot** and the new ctor validation *rejects* custom templates lacking it. Any downstream custom skills prompt breaks.
- **`FILESYSTEM_SYSTEM_PROMPT` becomes a template.** Grep the monorepo (`libs/cli`, `libs/daemon`, `libs/acp`) for verbatim imports/assertions before converting it.
- **wcmatch 6 → 10 changes `BRACE|GLOBSTAR` semantics** and `middleware/permissions.py:198` uses `globmatch` for allow/deny path rules. A silent semantics drift here is **security-relevant**. Gate the bump on `pytest tests/unit_tests -k "glob or permission or filesystem"` before merging anything else.
- **`base: None` + provenance.** If `_PROVENANCE_LOOP_PROMPT` is folded into the base, `system_prompt={"base": None}` silently un-prompts bound provenance tools. Append it as its own part.

**CLAUDE.md invariants to honor:**
- **Public API Stability** — every new param on `FilesystemMiddleware`, `SubAgentMiddleware`, `SkillsMiddleware`, `MemoryMiddleware`, `SummarizationMiddleware`, `StoreBackend`, `create_deep_agent` must be **keyword-only with a default that preserves today's behavior**. No positional reordering.
- **Lazy loading** — append to `_LAZY_IMPORTS`; never add a top-level `from … import …` in `bog_agents/__init__.py` or `middleware/__init__.py`.
- **`encoding="utf-8"`** on every `read_text`/`write_text` (skill trust store, deploy state, config manifest); secret-bearing files go through `io_utils.atomic_write_text` + `vars_store._secure_owner_only`.
- **`asyncio_mode = "auto"`** — no `@pytest.mark.asyncio`. And per MEMORY.md, run new CLI async tests **without** `--disable-socket` (Windows socket quirk).
- **Street Sweeper invariant** — the human-message eviction must not change message **count or order**, only text, or it desynchronizes `SummarizationMiddleware`'s cutoff indices and `AnthropicPromptCachingMiddleware`'s stable prefix.

**Open decisions an implementer must make before coding (do not let these be made implicitly):**
1. **`read()` return shape.** Adding `read -> ReadResult` in place breaks every bog backend and middleware. Recommended: keep `read -> str`, add `read_file/aread_file -> ReadResult` returning raw `FileData`, and make the str form a thin formatter. Decide *before* touching `protocol.py`.
2. **`DeleteResult` must carry `files_update`** (bog's `StateBackend` returns updates; upstream's sends them out-of-band via `CONFIG_KEY_SEND`). Do not copy upstream's dataclass verbatim — unless Wave 1C's `statebackend-config-key-writes` lands first, in which case reconcile.
3. **`private_state_field_names` home** — two fix drafts disagree (`middleware/_private_state.py` vs. appending to `middleware/_state.py`). `_state.py` is already taken by the unrelated `MiddlewareState` lock holder; **use `_private_state.py`**.
4. **`FsToolName` vocabulary** — bog has `multi_edit_file` and `read_many_files` that upstream doesn't. Ship a **superset** alias so upstream-typed lists still check.
5. **Subagent prompt overlay ordering** — bog prepends `DEFAULT_SUBAGENT_PROMPT` (anti-fabrication) which upstream lacks, and `base_system_prompt` *replaces*. Overlay `DEFAULT_SUBAGENT_PROMPT` as the base so a profile can swap the harness preamble, rather than nuking it. Document the choice in a comment.
6. **quickjs on Windows** — blocking spike: does `quickjs-rs` publish a wheel for the dev target? If not, Wave 4's biggest item needs a WASM/pure-Python engine behind the same `_repl.py` interface.

---

## Appendix — REFUTED (do NOT redo)

| id | why it's already fine |
|---|---|
| `message-eviction-module` | The eviction capability exists in full, **inlined** in `middleware/filesystem.py` (`TOO_LARGE_TOOL_MSG` :438, `_process_large_message` :1362, `_aprocess_large_message` :1421, incl. artifact/status preservation and return-original-on-write-failure). Only the *extraction* is needed, and only as a prerequisite for `_overflow_clip` — it is not a capability gap. |
| `p13-store-route-isolation` | `StoreBackend` already accepts `namespace=` (`store.py:106`) with `_validate_namespace`. The xfail'd test **passes** with distinct namespace factories and zero production changes. Upstream does not fix this structurally either. The xfail reason strings and MEMORY.md's "needs composite.py + migration" note are both **factually wrong** — correct disposition is to delete the two xfail markers. |
| `create-agent-default-state-schema` | `DeepAgentState` + `DeltaChannel` messages reducer are byte-identical, and `deepagents.py:155` already defaults `state_schema=DeepAgentState`. Upstream exports no `create_agent`, so bog's native default cannot be "incompatible" with anything. |
| `langsmith-sandbox-backend` *(the "doesn't exist" framing)* | A working `LangSmithBackend` + `LangSmithProvider` **exist and are wired** (`libs/cli/.../integrations/langsmith.py`, `sandbox_factory.py:178`, `pyproject.toml:122`). ARG_MAX is already handled generically via heredoc-to-stdin (`backends/sandbox.py:51`). The *real* residual gap is name/location + CRLF normalization + capture-offload — captured as `langsmith-sandbox-backend` in Wave 2. |
| `no-eval-dataset-corpus` | `libs/bog-agents/tests/evals/` exists with 8 LangSmith-marked eval modules, `Makefile:38` already sets `LANGSMITH_TEST_SUITE=bog-agents-evals`, `evals/scorers.py:76` has `LLMJudge`, and `libs/harbor/Makefile` runs Terminal-Bench 2.0 from the Harbor registry. Only the *eval product* (CLI, radar, vendored corpus, tau3/BFCL adapters) is missing — Wave 4. |
| `partners-runloop-empty` | Full Runloop backend + provider live in `libs/cli/.../integrations/runloop.py` (registered at `sandbox_factory.py:186`, dep at `pyproject.toml:125`). The `libs/partners/runloop/` directory contains **zero git-tracked files** — it's untracked `__pycache__` dirt in a dirty tree. Same for Modal. **Only Vercel is actually missing.** |