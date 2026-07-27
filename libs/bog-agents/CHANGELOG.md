# Changelog

## [0.9.11](https://github.com/bogware/bog-agents/compare/bog-agents==0.9.10...bog-agents==0.9.11) (2026-07-27)


### Features

* **sdk,cli:** make the OS sandbox reachable via .bog-agents/sandbox.toml ([#22](https://github.com/bogware/bog-agents/issues/22)) ([7ee356a](https://github.com/bogware/bog-agents/commit/7ee356ac169d4da4f4a941825f9871845acca3d1))
* **sdk:** deepagents 0.7.0b2 co-installable parity ([66cca89](https://github.com/bogware/bog-agents/commit/66cca89622d389540716139ff7e7800c2abbb163))
* **sdk:** evidence bundles — proof-of-work on every autonomous change ([#29](https://github.com/bogware/bog-agents/issues/29)) ([716b2c9](https://github.com/bogware/bog-agents/commit/716b2c9e35630cf6fef5fb45d87ac5d9f4ad94d7))
* **sdk:** governed agent teams — claimable task ledger + mailboxes + coordinator ([#21](https://github.com/bogware/bog-agents/issues/21)) ([8114225](https://github.com/bogware/bog-agents/commit/81142258f7ccd1fe88141692f9b0f8d81d5f9e9e))
* **sdk:** harden serve as a real surface (SDK-CORE-1/4/5/6) ([f45c2b9](https://github.com/bogware/bog-agents/commit/f45c2b959891ce7820cd78ca1ba752a171dd2b82))
* **sdk:** operator-pinned RBAC and air-gap policy the model can't lift ([d7c5271](https://github.com/bogware/bog-agents/commit/d7c5271ba1c1bcd1aadc2fa75d116bbf7dcbd6ff))
* **sdk:** per-agent cost ledger + runaway caps, CTX-3 pricing fix ([#25](https://github.com/bogware/bog-agents/issues/25)) ([132cbae](https://github.com/bogware/bog-agents/commit/132cbae5f40722e278735b8e51c5f3011a6bb6c9))
* **sdk:** wire OS sandbox into LocalShellBackend + allowlist egress proxy ([#22](https://github.com/bogware/bog-agents/issues/22)) ([0dce459](https://github.com/bogware/bog-agents/commit/0dce459cfc20c420a942cbcb09c76935ba4b8182))
* wire the declarative sandbox spec end-to-end ([#27](https://github.com/bogware/bog-agents/issues/27)) ([7eafe90](https://github.com/bogware/bog-agents/commit/7eafe9097796a531afb6328a570e090ee58eb533))


### Bug Fixes

* **cli:** silence the response beep by default + accept `none` for remote-read-timeout; refresh docs ([d24dab2](https://github.com/bogware/bog-agents/commit/d24dab27c21ca6f50a8cee50b98ac1fb49cc9ad0))
* **deps:** patch CVEs across shipped-lib lockfiles + widen dependabot (V3-9) ([6bf8cd8](https://github.com/bogware/bog-agents/commit/6bf8cd804c1a23b52918e54f1b91242dfce28e7f))
* **sdk:** builder feature-flag assembly + honest mcp/sandbox (SDK-CORE-2/7) ([fc79ee9](https://github.com/bogware/bog-agents/commit/fc79ee9b70e06e2bc33b90e12c027243fa9b88f8))
* **sdk:** don't crash the turn on missing git or a blocked dangerous command ([915b623](https://github.com/bogware/bog-agents/commit/915b6239433ec533f0842376c0de35c02f672c49))

## [0.9.10](https://github.com/bogware/bog-agents/compare/bog-agents==0.9.9...bog-agents==0.9.10) (2026-07-12)


### Features

* **sdk:** add GoalToolsMiddleware for persistent agent-visible goals ([70ffb43](https://github.com/bogware/bog-agents/commit/70ffb43cc879287fb9dd83c4f00a0929f53c2310))
* **sdk:** add pluggable symlink-trust checker hook to SkillsMiddleware ([6000c2f](https://github.com/bogware/bog-agents/commit/6000c2f1c0dc7fd12a47461c930528788e7293f0))
* **sdk:** complete deepagents backend rewrite (FileData v2, delete, overwrite) ([836cdea](https://github.com/bogware/bog-agents/commit/836cdea6a0a240fd738e949ee902c48a6364e0ae))
* **sdk:** deepagents 0.6.12 backend type foundation + dep floors ([3e0ef8b](https://github.com/bogware/bog-agents/commit/3e0ef8b2c3e6d112eef164cfd2e1468b2a27824f))
* **sdk:** deepagents drop-in core API (SystemPromptConfig, exports, profiles) ([674906a](https://github.com/bogware/bog-agents/commit/674906a72ce6272a3fe0bb375bb22215eff4f968))
* **sdk:** middleware interop surface + fix two permission-bypass vulns ([7687282](https://github.com/bogware/bog-agents/commit/768728284b2e54bfcc5fc549490395efca21d112))
* **sdk:** ship built-in model profiles + bedrock caching + video read ([38e7930](https://github.com/bogware/bog-agents/commit/38e793024c67a2fdd1f374698035090fc6e8585e))


### Bug Fixes

* **sdk:** activate no-op safety/feature middleware + harden serve ([bfc03f2](https://github.com/bogware/bog-agents/commit/bfc03f2946396e12670815d4d706f284bb3b8358))
* **sdk:** checkpointing works on fresh repos and never touches the user's git index ([d5a9df0](https://github.com/bogware/bog-agents/commit/d5a9df0194cdb6e42faba192da77b9907284c850))
* **sdk:** data-integrity + concurrency hardening (lost edits, atomic writes, DLP, worktree) ([2b28420](https://github.com/bogware/bog-agents/commit/2b2842084f8658ccb4a0fe105a2d9fd15ed35c33))
* **sdk:** don't follow a symlinked leaf on filesystem write/delete ([055bd81](https://github.com/bogware/bog-agents/commit/055bd818ef7e97e3bcad1f627837d76d1d33f826))
* **sdk:** resiliency/reliability/observability hardening across middleware and backends ([0999527](https://github.com/bogware/bog-agents/commit/0999527322336a872d878c7c562f958b7f039318))

## [0.9.9](https://github.com/bogware/bog-agents/compare/bog-agents==0.9.8...bog-agents==0.9.9) (2026-06-19)


### Features

* **sdk,cli:** document and release /update, PDF read_file, and review-cycle cleanup ([#151](https://github.com/bogware/bog-agents/issues/151)) ([30d05bb](https://github.com/bogware/bog-agents/commit/30d05bbdfa0b70d21367676a0d104db74527181b))

## [0.9.8](https://github.com/bogware/bog-agents/compare/bog-agents==0.9.7...bog-agents==0.9.8) (2026-06-18)


* **bog-agents:** Synchronize bog-agents-monorepo versions

## [0.9.7](https://github.com/bogware/bog-agents/compare/bog-agents==0.9.6...bog-agents==0.9.7) (2026-06-14)


### Features

* document 0.10 features + consolidate Dependabot into one grouped PR per package ([#134](https://github.com/bogware/bog-agents/issues/134)) ([4f00920](https://github.com/bogware/bog-agents/commit/4f009208c55270f5eb9c6d92628cab45670294b8))

## [0.9.6](https://github.com/bogware/bog-agents/compare/bog-agents==0.9.5...bog-agents==0.9.6) (2026-06-11)


* **bog-agents:** Synchronize bog-agents-monorepo versions

## [0.9.5](https://github.com/bogware/bog-agents/compare/bog-agents==0.9.4...bog-agents==0.9.5) (2026-06-10)


### Features

* **sdk,cli:** add street sweeper context-pruning with savings metrics ([#94](https://github.com/bogware/bog-agents/issues/94)) ([0170d2f](https://github.com/bogware/bog-agents/commit/0170d2f5df2e27ff291e7239ee98efab1b4d9f72))

## [0.9.4](https://github.com/bogware/bog-agents/compare/bog-agents==0.9.3...bog-agents==0.9.4) (2026-06-08)


### Features

* **sdk,cli:** deepagents parity, headless driving, provider resilience ([#91](https://github.com/bogware/bog-agents/issues/91)) ([165c5cb](https://github.com/bogware/bog-agents/commit/165c5cbc17a28fac8e15026914dfd7b0da3b02f2))

## [0.9.3](https://github.com/bogware/bog-agents/compare/bog-agents==0.9.2...bog-agents==0.9.3) (2026-05-23)


### Bug Fixes

* **sdk:** git_tools_bundle — rename `_runtime` → `runtime` for ToolRuntime injection ([#89](https://github.com/bogware/bog-agents/issues/89)) ([e000b36](https://github.com/bogware/bog-agents/commit/e000b364afdc8ed91d25f9406ecb0657cb1d5fc7))

## [0.9.2](https://github.com/bogware/bog-agents/compare/bog-agents==0.9.1...bog-agents==0.9.2) (2026-05-22)


* **bog-agents:** Synchronize bog-agents-monorepo versions

## [0.9.1](https://github.com/bogware/bog-agents/compare/bog-agents==0.9.0...bog-agents==0.9.1) (2026-05-20)


### Features

* **sdk,cli:** Bedrock seamless — auto inference-profile resolver, /bedrock fix + config, auto SSO refresh, docs ([#84](https://github.com/bogware/bog-agents/issues/84)) ([66f7338](https://github.com/bogware/bog-agents/commit/66f7338aa7b1283549ca7e876f6e452ec55d2847))

## [0.9.0](https://github.com/bogware/bog-agents/compare/bog-agents==0.8.7...bog-agents==0.9.0) (2026-05-19)


* force 0.9.0 release after PR [#82](https://github.com/bogware/bog-agents/issues/82) squash-merge ([64e7726](https://github.com/bogware/bog-agents/commit/64e772666d46d86c9d9e873e09a0aba85837b6a3))


### Features

* **cli:** scriptable TUI, compliance, and security sweep ([df94b67](https://github.com/bogware/bog-agents/commit/df94b67cc0aa6cdcd9b21aa896364532f3d9463e))

## [0.8.7](https://github.com/bogware/bog-agents/compare/bog-agents==0.8.6...bog-agents==0.8.7) (2026-05-16)


### Features

* dreamscape subsystem, nine killer slash commands, release-train enrichment, and reliability hardening ([#79](https://github.com/bogware/bog-agents/issues/79)) ([17a52ab](https://github.com/bogware/bog-agents/commit/17a52abcb8b20e8ebd3fdc5c2eb5f282b8e0026b))

## [0.8.6](https://github.com/bogware/bog-agents/compare/bog-agents==0.8.5...bog-agents==0.8.6) (2026-05-12)


* **bog-agents:** Synchronize bog-agents-monorepo versions

## [0.8.5](https://github.com/bogware/bog-agents/compare/bog-agents==0.8.4...bog-agents==0.8.5) (2026-05-10)


### Bug Fixes

* unsandboxed fs shell mode ([#73](https://github.com/bogware/bog-agents/issues/73)) ([40526cb](https://github.com/bogware/bog-agents/commit/40526cb69f708a866b16e5abc2ed39cf046f2714))

## [0.8.4](https://github.com/bogware/bog-agents/compare/bog-agents==0.8.3...bog-agents==0.8.4) (2026-05-08)


* **bog-agents:** Synchronize bog-agents-monorepo versions

## [0.8.3](https://github.com/bogware/bog-agents/compare/bog-agents==0.8.2...bog-agents==0.8.3) (2026-05-05)


* **bog-agents:** Synchronize bog-agents-monorepo versions

## [0.8.2](https://github.com/bogware/bog-agents/compare/bog-agents==0.8.1...bog-agents==0.8.2) (2026-05-04)


### Bug Fixes

* **cli,sdk:** kill ReadTimeouts on long turns + paste UX + ripgrep noise ([#65](https://github.com/bogware/bog-agents/issues/65)) ([b8f3498](https://github.com/bogware/bog-agents/commit/b8f349869dc0d6395a43a7edebdfe02385ede5c6))

## [0.8.1](https://github.com/bogware/bog-agents/compare/bog-agents==0.8.0...bog-agents==0.8.1) (2026-05-04)


### Features

* 0.7.0 - daemon/mcp/plugins/hardening ([#40](https://github.com/bogware/bog-agents/issues/40)) ([2427dfb](https://github.com/bogware/bog-agents/commit/2427dfbda3bffc17ea34f6e38de8d2634a57f86f))
* 0.8.0 — patient as still water ([#63](https://github.com/bogware/bog-agents/issues/63)) ([8b93798](https://github.com/bogware/bog-agents/commit/8b9379850e8c0360bb10dced6fe1dc83ebe9e11c))
* **cli:** add deeper slash support/ui fixes/etc ([#35](https://github.com/bogware/bog-agents/issues/35)) ([5a0dbe3](https://github.com/bogware/bog-agents/commit/5a0dbe39c99a9d905ae146adf12f51a732d5ac9a))
* **cli:** comprehensive docs, help screen, and rebrand sweep ([c053d58](https://github.com/bogware/bog-agents/commit/c053d589d1bbc69ef10aa82c6d6bbb294699ce87))
* **cli:** Comprehensive docs, help screen, fixes ([bd7c325](https://github.com/bogware/bog-agents/commit/bd7c325802bc4fd67a6b9c37e107830b39d90027))
* **cli:** Comprehensive docs, help screen, fixes ([bd7c325](https://github.com/bogware/bog-agents/commit/bd7c325802bc4fd67a6b9c37e107830b39d90027))
* prompts/pipelines/bugfixes ([#38](https://github.com/bogware/bog-agents/issues/38)) ([1191992](https://github.com/bogware/bog-agents/commit/1191992cca613282ae792063269159d729a08194))
* **sdk,cli:** add 17 killer features — middleware, serve API, CLI tools ([ab55111](https://github.com/bogware/bog-agents/commit/ab551118815438e538ae2dcbc1cdebcff84a49c5))
* **sdk,cli:** complete all 5 recommendations for production readiness ([de02cd6](https://github.com/bogware/bog-agents/commit/de02cd650ca927dec7c2769b1a0cfa9ca209f4a0))
* verify/call subcommands, shell reliability, cross-platform hardening, 12 CVEs closed ([#51](https://github.com/bogware/bog-agents/issues/51)) ([5f13fb4](https://github.com/bogware/bog-agents/commit/5f13fb4de5aa7cb50731b634796f0732a8a25f65))


### Bug Fixes

* 0.7.2 patch — memory/skills regression, ollama UX, partner resilience, Windows hardening ([#46](https://github.com/bogware/bog-agents/issues/46)) ([650975e](https://github.com/bogware/bog-agents/commit/650975e951997146a6299b8bcacb87e7c88389e5))
* 0.7.5 follow-up — resolve issue [#60](https://github.com/bogware/bog-agents/issues/60) (9 bug fixes) + dep-pin range ([#61](https://github.com/bogware/bog-agents/issues/61)) ([0a1c026](https://github.com/bogware/bog-agents/commit/0a1c0261dc58c55c20e2c5f89f3d95f00f4186db))
* **ci:** update uv from 0.5.25 to 0.8.17 to match lockfile format ([#33](https://github.com/bogware/bog-agents/issues/33)) ([59f2ada](https://github.com/bogware/bog-agents/commit/59f2adac2b54fa260bb15ca95b2df132e1e9014c))
* **cli,sdk:** fix lint errors and failing test ([a6d2f58](https://github.com/bogware/bog-agents/commit/a6d2f58931ccb823f8ed64c54b1c91f4d36f86e2))
* **cli:** resolve merge conflicts with main (v0.5.2) ([42aa364](https://github.com/bogware/bog-agents/commit/42aa3640935c5dbca45cc750ac4da29d33308025))
* **cli:** switch Bedrock provider from bedrock to bedrock_converse  ([#31](https://github.com/bogware/bog-agents/issues/31)) ([77bcca3](https://github.com/bogware/bog-agents/commit/77bcca31db3d58e98958184a8075295ea6c31b3b))
* **sdk,cli:** CTO review — fix 11 production readiness issues ([ed3be04](https://github.com/bogware/bog-agents/commit/ed3be044bf4f30f4ca6cbe47a38d4e47c5852920))
* **sdk,cli:** fix 3 runtime bugs found in hands-on testing ([3a002af](https://github.com/bogware/bog-agents/commit/3a002affe19eb907981ae246521669428299e453))
* **sdk:** add missing ty:ignore comments for starlette/uvicorn imports ([3405f28](https://github.com/bogware/bog-agents/commit/3405f28fc6089d9bf6451880316248bf9d36a53e))
* **sdk:** add missing ty:ignore comments for starlette/uvicorn imports ([3405f28](https://github.com/bogware/bog-agents/commit/3405f28fc6089d9bf6451880316248bf9d36a53e))
* **sdk:** add missing ty:ignore comments for starlette/uvicorn imports ([e5e4c72](https://github.com/bogware/bog-agents/commit/e5e4c7216809a04d99c1f5b11fea7bf01047fb75))
* **sdk:** architect review — fix 5 bugs, add lazy imports, add serve deps ([1a17d78](https://github.com/bogware/bog-agents/commit/1a17d78e6c43dcdc5bee6172aaa1cdc62397b927))
* **sdk:** implement real parallel execution in ParallelAgentsMiddleware ([6f6bdd2](https://github.com/bogware/bog-agents/commit/6f6bdd22995f953badfb0a4f2743021b09f0aabd))
* **sdk:** remove unused imports and dead code in middleware modules ([c638bb0](https://github.com/bogware/bog-agents/commit/c638bb0ba9620feda2843a231cd1ab917f429968))
* **sdk:** suppress ty unresolved-import errors for optional dependencies ([648a438](https://github.com/bogware/bog-agents/commit/648a4380bc316c0f239ecba87afad0042e74b7c1))

## [0.7.6](https://github.com/bogware/bog-agents/compare/bog-agents==0.7.5...bog-agents==0.7.6) (2026-05-02)


### Bug Fixes

* 0.7.5 follow-up — resolve issue [#60](https://github.com/bogware/bog-agents/issues/60) (9 bug fixes) + dep-pin range ([#61](https://github.com/bogware/bog-agents/issues/61)) ([0a1c026](https://github.com/bogware/bog-agents/commit/0a1c0261dc58c55c20e2c5f89f3d95f00f4186db))

## [0.7.5](https://github.com/bogware/bog-agents/compare/bog-agents==0.7.4...bog-agents==0.7.5) (2026-05-01)


* **bog-agents:** Synchronize bog-agents-monorepo versions

## [0.7.4](https://github.com/bogware/bog-agents/compare/bog-agents==0.7.3...bog-agents==0.7.4) (2026-04-30)


### Bug Fixes

* version-sync release alongside bog-agents-cli and bog-agents-daemon 0.7.4. No SDK code changes; published to keep linked versions in lockstep with the CLI's Bedrock auth_mode + auto-fallback fixes for [#54](https://github.com/bogware/bog-agents/issues/54) and the probe-cache fix for [#53](https://github.com/bogware/bog-agents/issues/53). ([fef8228](https://github.com/bogware/bog-agents/commit/fef82283e9fc07f5d286a26eea093e68d28cdb42))

## [0.7.3](https://github.com/bogware/bog-agents/compare/bog-agents==0.7.2...bog-agents==0.7.3) (2026-04-29)


### Features

* verify/call subcommands, shell reliability, cross-platform hardening, 12 CVEs closed ([#51](https://github.com/bogware/bog-agents/issues/51)) ([5f13fb4](https://github.com/bogware/bog-agents/commit/5f13fb4de5aa7cb50731b634796f0732a8a25f65))

## [0.7.2](https://github.com/bogware/bog-agents/compare/bog-agents==0.7.1...bog-agents==0.7.2) (2026-04-25)


### Bug Fixes

* 0.7.2 patch — memory/skills regression, ollama UX, partner resilience, Windows hardening ([#46](https://github.com/bogware/bog-agents/issues/46)) ([650975e](https://github.com/bogware/bog-agents/commit/650975e951997146a6299b8bcacb87e7c88389e5))

## [0.7.1](https://github.com/bogware/bog-agents/compare/bog-agents==0.7.0...bog-agents==0.7.1) (2026-04-20)


### Features

* 0.7.0 - daemon/mcp/plugins/hardening ([#40](https://github.com/bogware/bog-agents/issues/40)) ([2427dfb](https://github.com/bogware/bog-agents/commit/2427dfbda3bffc17ea34f6e38de8d2634a57f86f))
* **cli:** add deeper slash support/ui fixes/etc ([#35](https://github.com/bogware/bog-agents/issues/35)) ([5a0dbe3](https://github.com/bogware/bog-agents/commit/5a0dbe39c99a9d905ae146adf12f51a732d5ac9a))
* **cli:** comprehensive docs, help screen, and rebrand sweep ([c053d58](https://github.com/bogware/bog-agents/commit/c053d589d1bbc69ef10aa82c6d6bbb294699ce87))
* **cli:** Comprehensive docs, help screen, fixes ([bd7c325](https://github.com/bogware/bog-agents/commit/bd7c325802bc4fd67a6b9c37e107830b39d90027))
* **cli:** Comprehensive docs, help screen, fixes ([bd7c325](https://github.com/bogware/bog-agents/commit/bd7c325802bc4fd67a6b9c37e107830b39d90027))
* prompts/pipelines/bugfixes ([#38](https://github.com/bogware/bog-agents/issues/38)) ([1191992](https://github.com/bogware/bog-agents/commit/1191992cca613282ae792063269159d729a08194))
* **sdk,cli:** add 17 killer features — middleware, serve API, CLI tools ([ab55111](https://github.com/bogware/bog-agents/commit/ab551118815438e538ae2dcbc1cdebcff84a49c5))
* **sdk,cli:** complete all 5 recommendations for production readiness ([de02cd6](https://github.com/bogware/bog-agents/commit/de02cd650ca927dec7c2769b1a0cfa9ca209f4a0))


### Bug Fixes

* **ci:** update uv from 0.5.25 to 0.8.17 to match lockfile format ([#33](https://github.com/bogware/bog-agents/issues/33)) ([59f2ada](https://github.com/bogware/bog-agents/commit/59f2adac2b54fa260bb15ca95b2df132e1e9014c))
* **cli,sdk:** fix lint errors and failing test ([a6d2f58](https://github.com/bogware/bog-agents/commit/a6d2f58931ccb823f8ed64c54b1c91f4d36f86e2))
* **cli:** resolve merge conflicts with main (v0.5.2) ([42aa364](https://github.com/bogware/bog-agents/commit/42aa3640935c5dbca45cc750ac4da29d33308025))
* **cli:** switch Bedrock provider from bedrock to bedrock_converse  ([#31](https://github.com/bogware/bog-agents/issues/31)) ([77bcca3](https://github.com/bogware/bog-agents/commit/77bcca31db3d58e98958184a8075295ea6c31b3b))
* **sdk,cli:** CTO review — fix 11 production readiness issues ([ed3be04](https://github.com/bogware/bog-agents/commit/ed3be044bf4f30f4ca6cbe47a38d4e47c5852920))
* **sdk,cli:** fix 3 runtime bugs found in hands-on testing ([3a002af](https://github.com/bogware/bog-agents/commit/3a002affe19eb907981ae246521669428299e453))
* **sdk:** add missing ty:ignore comments for starlette/uvicorn imports ([3405f28](https://github.com/bogware/bog-agents/commit/3405f28fc6089d9bf6451880316248bf9d36a53e))
* **sdk:** add missing ty:ignore comments for starlette/uvicorn imports ([3405f28](https://github.com/bogware/bog-agents/commit/3405f28fc6089d9bf6451880316248bf9d36a53e))
* **sdk:** add missing ty:ignore comments for starlette/uvicorn imports ([e5e4c72](https://github.com/bogware/bog-agents/commit/e5e4c7216809a04d99c1f5b11fea7bf01047fb75))
* **sdk:** architect review — fix 5 bugs, add lazy imports, add serve deps ([1a17d78](https://github.com/bogware/bog-agents/commit/1a17d78e6c43dcdc5bee6172aaa1cdc62397b927))
* **sdk:** implement real parallel execution in ParallelAgentsMiddleware ([6f6bdd2](https://github.com/bogware/bog-agents/commit/6f6bdd22995f953badfb0a4f2743021b09f0aabd))
* **sdk:** remove unused imports and dead code in middleware modules ([c638bb0](https://github.com/bogware/bog-agents/commit/c638bb0ba9620feda2843a231cd1ab917f429968))
* **sdk:** suppress ty unresolved-import errors for optional dependencies ([648a438](https://github.com/bogware/bog-agents/commit/648a4380bc316c0f239ecba87afad0042e74b7c1))

## [0.6.5](https://github.com/bogware/bog-agents/compare/bog-agents==0.6.4...bog-agents==0.6.5) (2026-04-16)


### Features

* prompts/pipelines/bugfixes ([#38](https://github.com/bogware/bog-agents/issues/38)) ([1191992](https://github.com/bogware/bog-agents/commit/1191992cca613282ae792063269159d729a08194))

## [0.6.4](https://github.com/bogware/bog-agents/compare/bog-agents==0.6.3...bog-agents==0.6.4) (2026-04-14)


### Features

* **cli:** add deeper slash support/ui fixes/etc ([#35](https://github.com/bogware/bog-agents/issues/35)) ([5a0dbe3](https://github.com/bogware/bog-agents/commit/5a0dbe39c99a9d905ae146adf12f51a732d5ac9a))

## [0.6.3](https://github.com/bogware/bog-agents/compare/bog-agents==0.6.2...bog-agents==0.6.3) (2026-03-29)


### Bug Fixes

* **ci:** update uv from 0.5.25 to 0.8.17 to match lockfile format ([#33](https://github.com/bogware/bog-agents/issues/33)) ([59f2ada](https://github.com/bogware/bog-agents/commit/59f2adac2b54fa260bb15ca95b2df132e1e9014c))
* **cli:** switch Bedrock provider from bedrock to bedrock_converse  ([#31](https://github.com/bogware/bog-agents/issues/31)) ([77bcca3](https://github.com/bogware/bog-agents/commit/77bcca31db3d58e98958184a8075295ea6c31b3b))

## [0.6.0](https://github.com/bogware/bog-agents/compare/bog-agents==0.5.2...bog-agents==0.6.0) (2026-03-24)


### Features

* **sdk:** comprehensive docs, help screen, and rebrand sweep ([e5e4c72](https://github.com/bogware/bog-agents/commit/e5e4c72))


### Bug Fixes

* **sdk:** add missing ty:ignore comments for starlette/uvicorn imports ([e5e4c72](https://github.com/bogware/bog-agents/commit/e5e4c72))
* **cli,sdk:** fix lint errors and failing test ([a6d2f58](https://github.com/bogware/bog-agents/commit/a6d2f58))

## [0.5.2](https://github.com/bogware/bog-agents/compare/bog-agents==0.5.1...bog-agents==0.5.2) (2026-03-23)


### Features

* **sdk,cli:** add 17 killer features — middleware, serve API, CLI tools ([ab55111](https://github.com/bogware/bog-agents/commit/ab551118815438e538ae2dcbc1cdebcff84a49c5))
* **sdk,cli:** complete all 5 recommendations for production readiness ([de02cd6](https://github.com/bogware/bog-agents/commit/de02cd650ca927dec7c2769b1a0cfa9ca209f4a0))


### Bug Fixes

* **sdk,cli:** CTO review — fix 11 production readiness issues ([ed3be04](https://github.com/bogware/bog-agents/commit/ed3be044bf4f30f4ca6cbe47a38d4e47c5852920))
* **sdk,cli:** fix 3 runtime bugs found in hands-on testing ([3a002af](https://github.com/bogware/bog-agents/commit/3a002affe19eb907981ae246521669428299e453))
* **sdk:** architect review — fix 5 bugs, add lazy imports, add serve deps ([1a17d78](https://github.com/bogware/bog-agents/commit/1a17d78e6c43dcdc5bee6172aaa1cdc62397b927))
* **sdk:** implement real parallel execution in ParallelAgentsMiddleware ([6f6bdd2](https://github.com/bogware/bog-agents/commit/6f6bdd22995f953badfb0a4f2743021b09f0aabd))
* **sdk:** remove unused imports and dead code in middleware modules ([c638bb0](https://github.com/bogware/bog-agents/commit/c638bb0ba9620feda2843a231cd1ab917f429968))
* **sdk:** suppress ty unresolved-import errors for optional dependencies ([648a438](https://github.com/bogware/bog-agents/commit/648a4380bc316c0f239ecba87afad0042e74b7c1))

## [0.5.1](https://github.com/bogware/bog-agents/compare/bog-agents==0.5.0...bog-agents==0.5.1) (2026-03-23)


### Features

* **sdk,cli:** add 17 killer features — middleware, serve API, CLI tools ([ab55111](https://github.com/bogware/bog-agents/commit/ab551118815438e538ae2dcbc1cdebcff84a49c5))
* **sdk,cli:** complete all 5 recommendations for production readiness ([de02cd6](https://github.com/bogware/bog-agents/commit/de02cd650ca927dec7c2769b1a0cfa9ca209f4a0))


### Bug Fixes

* **sdk,cli:** CTO review — fix 11 production readiness issues ([ed3be04](https://github.com/bogware/bog-agents/commit/ed3be044bf4f30f4ca6cbe47a38d4e47c5852920))
* **sdk,cli:** fix 3 runtime bugs found in hands-on testing ([3a002af](https://github.com/bogware/bog-agents/commit/3a002affe19eb907981ae246521669428299e453))
* **sdk:** architect review — fix 5 bugs, add lazy imports, add serve deps ([1a17d78](https://github.com/bogware/bog-agents/commit/1a17d78e6c43dcdc5bee6172aaa1cdc62397b927))
* **sdk:** implement real parallel execution in ParallelAgentsMiddleware ([6f6bdd2](https://github.com/bogware/bog-agents/commit/6f6bdd22995f953badfb0a4f2743021b09f0aabd))
* **sdk:** remove unused imports and dead code in middleware modules ([c638bb0](https://github.com/bogware/bog-agents/commit/c638bb0ba9620feda2843a231cd1ab917f429968))
* **sdk:** suppress ty unresolved-import errors for optional dependencies ([648a438](https://github.com/bogware/bog-agents/commit/648a4380bc316c0f239ecba87afad0042e74b7c1))

## [0.5.0]

### Features

- HTTP serve module with REST API, SSE streaming, and thread management
- Three-tier auth for serve mode (env var, config, auto-generate for non-localhost)
- Serve dependencies available as pip extra: `pip install 'bog-agents[serve]'`
- 88+ middleware modules with lazy loading for fast imports
- Security audit middleware with self-detection exclusion
- Scheduled runs store with robust JSON loading
- Self-improving middleware with auto-session initialization

### Bug Fixes

- Fixed `ScheduledRunsStore._load()` crash on empty JSON files
- Fixed `SelfImprovingMiddleware` NoneType error when no session started
- Fixed security audit scanner detecting its own patterns as findings
