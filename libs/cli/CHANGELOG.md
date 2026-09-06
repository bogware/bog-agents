# Changelog

## [0.9.14](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.9.13...bog-agents-cli==0.9.14) (2026-09-06)


### Features

* **cli:** /tasks command center, /recap and thread flags (ROADMAP [#68](https://github.com/bogware/bog-agents/issues/68)) ([ad63769](https://github.com/bogware/bog-agents/commit/ad63769489e4ffd695347d3ed97568c0bd14e7ad))
* **cli:** Agent Plugins 1.0 native + one-command import — ROADMAP [#62](https://github.com/bogware/bog-agents/issues/62) ([59f0b78](https://github.com/bogware/bog-agents/commit/59f0b78e156a3207cc2e787aa2d646c0bca17f6c))
* **cli:** agent-authored workflows saved as slash commands (ROADMAP [#73](https://github.com/bogware/bog-agents/issues/73)) ([15cbc4f](https://github.com/bogware/bog-agents/commit/15cbc4fb2e99c3ee4f2694390983c90182e40b44))
* **cli:** audit the advertised command surface (`--doctor-features`) ([071549d](https://github.com/bogware/bog-agents/commit/071549d245898f42c565d692172f02edcf177765))
* **cli:** cost-objective routing, decisions log and provider failover (ROADMAP [#53](https://github.com/bogware/bog-agents/issues/53)) ([b54310c](https://github.com/bogware/bog-agents/commit/b54310c82726b4427a08841fa3806f4a1ef98dd5))
* **cli:** Governed Auto Mode — ROADMAP [#47](https://github.com/bogware/bog-agents/issues/47) ([66f4db3](https://github.com/bogware/bog-agents/commit/66f4db3f6068b104c6ad1f228c8d02c206109cb6))
* **cli:** hook bus v2 — result replacement, new events, on_failure, hash pins, prompt hooks (ROADMAP [#64](https://github.com/bogware/bog-agents/issues/64)) ([fd93211](https://github.com/bogware/bog-agents/commit/fd9321158be18a118d3d59a33eee4f79061380b6))
* **cli:** managed governance layer (ROADMAP [#50](https://github.com/bogware/bog-agents/issues/50)) ([7e8fb63](https://github.com/bogware/bog-agents/commit/7e8fb63cfca7b22a0e1c0eaf49cf39d5952a480a))
* **cli:** memory rebuild and the ask_advisor tool (ROADMAP [#75](https://github.com/bogware/bog-agents/issues/75)) ([3f817c1](https://github.com/bogware/bog-agents/commit/3f817c1d2003b34c6fb0daa3b3767de07adeecd2))
* **cli:** plan review screen and headless plan-then-execute (ROADMAP [#69](https://github.com/bogware/bog-agents/issues/69)) ([5aae696](https://github.com/bogware/bog-agents/commit/5aae696aa7b1c5bfa609bc2057248c35db0e6819))
* **cli:** self-review memo, rulings and a jury pass on every PR (ROADMAP [#67](https://github.com/bogware/bog-agents/issues/67)) ([aeea2bb](https://github.com/bogware/bog-agents/commit/aeea2bbd64ebaac6a02622363a449f45088dec56))
* **cli:** session registry, cross-process queue, detach/attach and daemon drain (ROADMAP [#56](https://github.com/bogware/bog-agents/issues/56)) ([f0fdbf0](https://github.com/bogware/bog-agents/commit/f0fdbf08d0e9725278d31cbd0db442d9b28f0638))
* **cli:** steerable approvals and hostile-repo git hardening (ROADMAP [#49](https://github.com/bogware/bog-agents/issues/49)) ([0a325eb](https://github.com/bogware/bog-agents/commit/0a325eb5dfb1d4c0f9eeed0382d992eb707d2e08))
* **cli:** trust profiles, --restricted and workspace trust (ROADMAP [#48](https://github.com/bogware/bog-agents/issues/48)) ([b359f73](https://github.com/bogware/bog-agents/commit/b359f73550feb76e07232226a165edb912140b0d))
* **cli:** Windows distribution and first run (ROADMAP [#61](https://github.com/bogware/bog-agents/issues/61)) ([a0be5f9](https://github.com/bogware/bog-agents/commit/a0be5f92b073beb766ce1b5627e90d241d29a7cb))
* **daemon,sdk,cli:** thread-linked jobs, subscriptions and attempt caps (ROADMAP [#55](https://github.com/bogware/bog-agents/issues/55)) ([d629b8c](https://github.com/bogware/bog-agents/commit/d629b8c6823b45ad9db09bbd78537f2337a7977f))
* **daemon:** add `jobs create --github` for the assign-to-bog trigger ([00dbc5a](https://github.com/bogware/bog-agents/commit/00dbc5aa0c32f872b59f02fdfa5080709ccc6926))
* fork subagents (ROADMAP [#71](https://github.com/bogware/bog-agents/issues/71)) and the compliance artefact (ROADMAP [#74](https://github.com/bogware/bog-agents/issues/74)) ([677bb98](https://github.com/bogware/bog-agents/commit/677bb9850a3aee6db7ad4dd73defc335c511f024))
* **sdk,cli,daemon:** cost certainty — ROADMAP [#51](https://github.com/bogware/bog-agents/issues/51) ([732f76f](https://github.com/bogware/bog-agents/commit/732f76f6a81994bd70f8c98f4c1a1eac694649a0))
* **sdk,cli:** measured harness overhead, lean profile and --mini (ROADMAP [#54](https://github.com/bogware/bog-agents/issues/54)) ([9caa573](https://github.com/bogware/bog-agents/commit/9caa573c4da3b1caeaa14d1377d760505ac00aad))
* **sdk,cli:** turn-end changes tray with proof-ordered diffs — ROADMAP [#66](https://github.com/bogware/bog-agents/issues/66) ([53e1162](https://github.com/bogware/bog-agents/commit/53e1162ef64bb4ce2a0ceb60d6f346073d19b905))
* **sdk,cli:** usage you can read — ROADMAP [#52](https://github.com/bogware/bog-agents/issues/52) ([3804a22](https://github.com/bogware/bog-agents/commit/3804a227c189b304a9208270bf18f7402c35973c))
* **sdk:** findings ledger, scan jobs and the security-scan recipe (ROADMAP [#59](https://github.com/bogware/bog-agents/issues/59), [#70](https://github.com/bogware/bog-agents/issues/70)) ([3d83d6c](https://github.com/bogware/bog-agents/commit/3d83d6cdff5a09f5eef609ff0454ffe356f5f39b))
* **sdk:** governed code mode (ROADMAP [#72](https://github.com/bogware/bog-agents/issues/72)) ([31b00a1](https://github.com/bogware/bog-agents/commit/31b00a1b496ad21f5f1361290b1e666d40486d94))
* **sdk:** team v2 — attachments, file exchange, env reuse, mounts (ROADMAP [#76](https://github.com/bogware/bog-agents/issues/76)) ([eaddd7b](https://github.com/bogware/bog-agents/commit/eaddd7bc14f4757ab533cebbbf0dbd042ca2ff79))


### Bug Fixes

* **ci,acp,harbor,vscode:** Wave D — delivery truth (DEL-3, SAT-3, SAT-4, SAT-5) ([e37d156](https://github.com/bogware/bog-agents/commit/e37d156a317a8223ec2b067e433a26f04810aaa9))
* **cli,daemon,vscode:** Wave B — first-30-minutes polish ([5821cf5](https://github.com/bogware/bog-agents/commit/5821cf50469a48c3208078eac3cbc650051905b9))
* **cli,sdk:** resurrect /think and /worktrees for the server-hosted agent ([06e704f](https://github.com/bogware/bog-agents/commit/06e704f5bf89f1f02a643553cc5eac53ee78bc36))
* **cli:** make /checkpoint load actually restore the saved thread ([b8bd651](https://github.com/bogware/bog-agents/commit/b8bd6516ef3bc26369564cddb121ec343c7757c0))
* **cli:** point install hints at extras that exist ([02a0a18](https://github.com/bogware/bog-agents/commit/02a0a183c1259420680fe9c3965075254e232b78))
* **cli:** run the nine inline model-calling commands as tracked sessions ([e7ceaf7](https://github.com/bogware/bog-agents/commit/e7ceaf79cbed6b10bf11115fbee7c3a81c816e10))
* **daemon:** render trigger context into job prompts and output fields ([d3ef41b](https://github.com/bogware/bog-agents/commit/d3ef41bb62def95d3053a9936b9c085d0f1a9723))
* **sdk,cli:** Wave A — make governance count and enforce where it claimed to ([3c41fde](https://github.com/bogware/bog-agents/commit/3c41fde662081a8aa50d1405a27087f1df418ce1))
* **sdk,cli:** Wave C — context and perf tail (SDK-2/3/4/5/6) ([6ae78be](https://github.com/bogware/bog-agents/commit/6ae78be3ec70615d75dc9daab451e33887c67518))

## [0.9.13](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.9.12...bog-agents-cli==0.9.13) (2026-08-22)


### Bug Fixes

* **cli,sdk:** price CLI sessions by real model and stop the empty-name 1M window ([51666f5](https://github.com/bogware/bog-agents/commit/51666f58480a01698c4e54f644672b3cf09ad29c))
* **cli:** honor BOG_AGENTS_HOME, isolate MCP servers, gate butcher writes, secure tokens ([a2cebd1](https://github.com/bogware/bog-agents/commit/a2cebd113f304c33d6cc6428c1183bc9f0a289a6))
* **cli:** run session commands off the App pump in TurnManager-tracked workers ([057c186](https://github.com/bogware/bog-agents/commit/057c186464a38846d85f12d2c5e2f9060fab7015))
* **safety:** close the approval-gate bypasses (exec-risk, git flags, hooks, batch tools, PTY) ([5b36157](https://github.com/bogware/bog-agents/commit/5b36157d9f0fa014f117b3ae1952fb7ee00546e2))


### Performance Improvements

* **cli:** make /threads search incremental and off the event loop ([98dc349](https://github.com/bogware/bog-agents/commit/98dc34944e56b68e5c961a6544e77985cc806914))
* **cli:** scan streamed tool-call args incrementally ([c7b7908](https://github.com/bogware/bog-agents/commit/c7b7908928c181845d466fc198fb810d96fc52f0))

## [0.9.12](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.9.11...bog-agents-cli==0.9.12) (2026-08-02)


### Features

* **cli:** decision-capable hooks + Claude/Cursor compat (hook-bus completion) ([a119c55](https://github.com/bogware/bog-agents/commit/a119c55539463ae8aa7e208b2148482cc9887b08))
* **cli:** enforce PreToolUse hook denials in the tool path (hook-bus completion) ([bc86d8a](https://github.com/bogware/bog-agents/commit/bc86d8a0bc48660ca3ee43a3b811ed831e7c25e8))
* **cli:** full-text session search — /threads search &lt;text&gt; (Tier-1 [#4](https://github.com/bogware/bog-agents/issues/4)) ([f1e503b](https://github.com/bogware/bog-agents/commit/f1e503b8f4b12e2ca225eadba9268150a7eb983c))
* **cli:** multi-vendor project-rules ingestion (Tier-1 [#5](https://github.com/bogware/bog-agents/issues/5)) ([4240adc](https://github.com/bogware/bog-agents/commit/4240adce2c3029be905ef32ef98e55dc6f06c5e2))
* **cli:** wire the Tier-1/[#8](https://github.com/bogware/bog-agents/issues/8) cores to user surfaces (auto-background, stop gate, memory search) ([a713568](https://github.com/bogware/bog-agents/commit/a713568d5b8f50fb036eb5f87ae857c2a9834e81))
* finish the PTY harness — Windows ConPTY + agent tools (Tier-2 [#6](https://github.com/bogware/bog-agents/issues/6)) ([ef2b006](https://github.com/bogware/bog-agents/commit/ef2b006e9c335143ddb2d8adbb6a0f77b70accc7))
* finish tier-1 remainder (vim editing, git/bash gates, hook types, sidechain continuation) ([4733dda](https://github.com/bogware/bog-agents/commit/4733ddad6aa070ac8ba518b8e07d6dc7f9d75731))
* polish — pyte terminal grid + hybrid-memory vector path ([c887cc3](https://github.com/bogware/bog-agents/commit/c887cc3519b65fb968548746d34bdc1a32934cf9))
* **sdk:** heal context-length & truncation, auto-background by default ([fdf3962](https://github.com/bogware/bog-agents/commit/fdf39628806c1947e42cdd1baa70b676d82bf106))
* tier-1 resilience, hook bus, and agent surfaces ([6c96304](https://github.com/bogware/bog-agents/commit/6c96304398ca649d9bf3eaf0c44756735451796e))
* tier-1 resilience, hook bus, and agent surfaces ([6c96304](https://github.com/bogware/bog-agents/commit/6c96304398ca649d9bf3eaf0c44756735451796e))


### Bug Fixes

* **cli:** catch clustered force-push, +refspec, and remote deletes ([d4b05ec](https://github.com/bogware/bog-agents/commit/d4b05ec544fc66d57e90988aa5a03d28eb68fa26))
* **cli:** contain vim-engine failures on the keystroke path ([08fb063](https://github.com/bogware/bog-agents/commit/08fb0635f08b36ee188c7feab41bdcfe4c0b87c0))
* **cli:** escape session text in /threads search results ([3465960](https://github.com/bogware/bog-agents/commit/34659600c4137e5960456a46901bd0e1ffa4c616))
* **cli:** ingest Cursor .mdc project rules ([e7ef6cc](https://github.com/bogware/bog-agents/commit/e7ef6cc3bc63e2f8e9b69bc32633cd7da1e794ab))
* **cli:** make shell auto-backgrounding opt-in ([939c52b](https://github.com/bogware/bog-agents/commit/939c52b44273d7db5fed5d0716bbdf979d6726ee))
* **cli:** register vim_mode and memory_vector in the config manifest ([c1e3aae](https://github.com/bogware/bog-agents/commit/c1e3aaec7890de757404e9d931712e8609016bae))
* **sdk,cli:** make multi-word FTS queries actually retrieve ([21760a0](https://github.com/bogware/bog-agents/commit/21760a09b380f9a074cac647694efe3a5c71d9ed))

## [0.9.11](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.9.10...bog-agents-cli==0.9.11) (2026-07-27)


### Features

* **cli:** best-of-n attempts with rubric auto-judge ([#31](https://github.com/bogware/bog-agents/issues/31)) ([5d67d88](https://github.com/bogware/bog-agents/commit/5d67d88df1e1fc1807ca3e852f65811e5a099c9c))
* **cli:** self-modification guard — gate writes to the agent's own authority files ([b245ee0](https://github.com/bogware/bog-agents/commit/b245ee026e7ebed5aa46bb06f36c7961416a7798))
* **cli:** wire /team run — governed agent team over a task ledger ([#21](https://github.com/bogware/bog-agents/issues/21)) ([68ffc9c](https://github.com/bogware/bog-agents/commit/68ffc9c480a7f8dae81c30d28c25bba7a07738c3))
* **sdk,cli:** make the OS sandbox reachable via .bog-agents/sandbox.toml ([#22](https://github.com/bogware/bog-agents/issues/22)) ([7ee356a](https://github.com/bogware/bog-agents/commit/7ee356ac169d4da4f4a941825f9871845acca3d1))
* **sdk:** deepagents 0.7.0b2 co-installable parity ([66cca89](https://github.com/bogware/bog-agents/commit/66cca89622d389540716139ff7e7800c2abbb163))
* wire the declarative sandbox spec end-to-end ([#27](https://github.com/bogware/bog-agents/issues/27)) ([7eafe90](https://github.com/bogware/bog-agents/commit/7eafe9097796a531afb6328a570e090ee58eb533))


### Bug Fixes

* **build:** de-advertise the unpublished acp extra, refresh stale locks, add lock+satellite CI ([3d9e7e0](https://github.com/bogware/bog-agents/commit/3d9e7e06280f5f4c6de6f36bd2fde52e0bc9600c))
* **cli:** gate butcher plans behind approval and enforce the per-slice allowlist ([79a0f94](https://github.com/bogware/bog-agents/commit/79a0f94abb46b72f793a6fae8c29cb577276729d))
* **cli:** gate mutating git tools behind HITL and honor BOG_AGENTS_MCP_TRUST ([a8a0818](https://github.com/bogware/bog-agents/commit/a8a08180ccfb727f8e65303f2f34aa0b5db053da))
* **cli:** serialize turn dispatch and stop effort truncating non-reasoning models ([6a3c8c5](https://github.com/bogware/bog-agents/commit/6a3c8c53f21d997e7674941127c5601417dcc75d))
* **cli:** silence the response beep by default + accept `none` for remote-read-timeout; refresh docs ([d24dab2](https://github.com/bogware/bog-agents/commit/d24dab27c21ca6f50a8cee50b98ac1fb49cc9ad0))
* **deps:** patch CVEs across shipped-lib lockfiles + widen dependabot (V3-9) ([6bf8cd8](https://github.com/bogware/bog-agents/commit/6bf8cd804c1a23b52918e54f1b91242dfce28e7f))

## [0.9.10](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.9.9...bog-agents-cli==0.9.10) (2026-07-12)


### Features

* **cli:** env-var registry + config manifest with real config command ([e602887](https://github.com/bogware/bog-agents/commit/e602887fc48b467a6c59922c5edecab51267f251))
* **cli:** managed ripgrep auto-install + ${VAR} MCP header interpolation ([6c4df82](https://github.com/bogware/bog-agents/commit/6c4df82d0010c2d4601b7b9035088c373101ce5f))
* **cli:** native reasoning-effort, ctrl+x external editor, /goal and /rubric ([9feaeb3](https://github.com/bogware/bog-agents/commit/9feaeb3067a13579905644de0af2e4fa84b2940a))
* **cli:** spec-compliant MCP OAuth (mcp SDK OAuthClientProvider) ([c30d5ed](https://github.com/bogware/bog-agents/commit/c30d5edc6967af472a57f6e9b0152b594abf0f76))
* **cli:** theme system, skill trust store, and UX polish ([8271f4b](https://github.com/bogware/bog-agents/commit/8271f4bf8c1bd315e0f0b1c41555007c1f399d4e))


### Bug Fixes

* **cli:** concurrency + lifecycle hardening (orchestrator, auto-commit race, fork lock, oauth state) ([5b0a0dc](https://github.com/bogware/bog-agents/commit/5b0a0dc413dff66db4f7ce4c087d52e9b64bd832))
* **cli:** escape untrusted markup on trust surfaces + headless HITL/cache bugs ([70e4e50](https://github.com/bogware/bog-agents/commit/70e4e50769db5b9ba72ad81881dc287361d8c9cd))
* **cli:** resiliency/reliability/observability hardening (TUI, auth, sessions, ops) ([b3fcbd1](https://github.com/bogware/bog-agents/commit/b3fcbd16417f2091c4aa48d2763950adbb095874))
* **cli:** SSRF guards on web/agent fetch, sign TraceFile header, fix dreamscape laws ([11e589d](https://github.com/bogware/bog-agents/commit/11e589d1683d7ab304866095aec99a99418c1d67))

## [0.9.9](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.9.8...bog-agents-cli==0.9.9) (2026-06-19)


### Features

* **sdk,cli:** document and release /update, PDF read_file, and review-cycle cleanup ([#151](https://github.com/bogware/bog-agents/issues/151)) ([30d05bb](https://github.com/bogware/bog-agents/commit/30d05bbdfa0b70d21367676a0d104db74527181b))

## [0.9.8](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.9.7...bog-agents-cli==0.9.8) (2026-06-18)


### Features

* **cli:** resilient Bedrock fallback and hittable-model auto-default ([#145](https://github.com/bogware/bog-agents/issues/145)) ([bde713d](https://github.com/bogware/bog-agents/commit/bde713d1d6423ab4ec86298d24d437653bc5b2c2))

## [0.9.7](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.9.6...bog-agents-cli==0.9.7) (2026-06-14)


### Features

* document 0.10 features + consolidate Dependabot into one grouped PR per package ([#134](https://github.com/bogware/bog-agents/issues/134)) ([4f00920](https://github.com/bogware/bog-agents/commit/4f009208c55270f5eb9c6d92628cab45670294b8))

## [0.9.6](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.9.5...bog-agents-cli==0.9.6) (2026-06-11)


### Features

* **cli:** add operator routing, butcher decomposition, and jtbd workflow ([#97](https://github.com/bogware/bog-agents/issues/97)) ([4f91594](https://github.com/bogware/bog-agents/commit/4f915941b5decf708fc2dab1c6de26509db984d9))

## [0.9.5](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.9.4...bog-agents-cli==0.9.5) (2026-06-10)


### Features

* **sdk,cli:** add street sweeper context-pruning with savings metrics ([#94](https://github.com/bogware/bog-agents/issues/94)) ([0170d2f](https://github.com/bogware/bog-agents/commit/0170d2f5df2e27ff291e7239ee98efab1b4d9f72))

## [0.9.4](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.9.3...bog-agents-cli==0.9.4) (2026-06-08)


### Features

* **sdk,cli:** deepagents parity, headless driving, provider resilience ([#91](https://github.com/bogware/bog-agents/issues/91)) ([165c5cb](https://github.com/bogware/bog-agents/commit/165c5cbc17a28fac8e15026914dfd7b0da3b02f2))

## [0.9.3](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.9.2...bog-agents-cli==0.9.3) (2026-05-23)


* **bog-agents-cli:** Synchronize bog-agents-monorepo versions

## [0.9.2](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.9.1...bog-agents-cli==0.9.2) (2026-05-22)


### Bug Fixes

* **cli:** disable per-chunk SSE timeout, add liveness watchdog for long jobs ([#86](https://github.com/bogware/bog-agents/issues/86)) ([f9347bc](https://github.com/bogware/bog-agents/commit/f9347bc85f550441302a25b9ad4264dc42196990))

## [0.9.1](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.9.0...bog-agents-cli==0.9.1) (2026-05-20)


### Features

* **sdk,cli:** Bedrock seamless — auto inference-profile resolver, /bedrock fix + config, auto SSO refresh, docs ([#84](https://github.com/bogware/bog-agents/issues/84)) ([66f7338](https://github.com/bogware/bog-agents/commit/66f7338aa7b1283549ca7e876f6e452ec55d2847))

## [0.9.0](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.8.7...bog-agents-cli==0.9.0) (2026-05-19)


* force 0.9.0 release after PR [#82](https://github.com/bogware/bog-agents/issues/82) squash-merge ([64e7726](https://github.com/bogware/bog-agents/commit/64e772666d46d86c9d9e873e09a0aba85837b6a3))


### Features

* **cli:** scriptable TUI, compliance, and security sweep ([df94b67](https://github.com/bogware/bog-agents/commit/df94b67cc0aa6cdcd9b21aa896364532f3d9463e))

## [0.8.7](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.8.6...bog-agents-cli==0.8.7) (2026-05-16)


### Features

* dreamscape subsystem, nine killer slash commands, release-train enrichment, and reliability hardening ([#79](https://github.com/bogware/bog-agents/issues/79)) ([17a52ab](https://github.com/bogware/bog-agents/commit/17a52abcb8b20e8ebd3fdc5c2eb5f282b8e0026b))


### Bug Fixes

* **cli:** de-flake dreamscape rate-limit test on slow Windows runners ([#81](https://github.com/bogware/bog-agents/issues/81)) ([eb14713](https://github.com/bogware/bog-agents/commit/eb14713ebba9a219fa53bddfdc5378c5a7056193))

## [0.8.6](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.8.5...bog-agents-cli==0.8.6) (2026-05-12)


### Features

* **cli:** refine model/provider system — display names, lag fix, smoketest, thinking, bedrock inference profiles ([#76](https://github.com/bogware/bog-agents/issues/76)) ([28d005d](https://github.com/bogware/bog-agents/commit/28d005dc4786c1251d27636f7086d298cd048a5a))


### Bug Fixes

* **cli:** drop filter debounce in model picker — fixes CI flake on Python 3.12 ([#78](https://github.com/bogware/bog-agents/issues/78)) ([09063f3](https://github.com/bogware/bog-agents/commit/09063f38aed97948b49451e4cfe5ebe9db1fd9bc))

## [0.8.5](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.8.4...bog-agents-cli==0.8.5) (2026-05-10)


### Bug Fixes

* unsandboxed fs shell mode ([#73](https://github.com/bogware/bog-agents/issues/73)) ([40526cb](https://github.com/bogware/bog-agents/commit/40526cb69f708a866b16e5abc2ed39cf046f2714))

## [0.8.4](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.8.3...bog-agents-cli==0.8.4) (2026-05-08)


### Bug Fixes

* **cli:** server-graph crash on bad MCP config + doctor MCP discovery + copy notify clutter ([#70](https://github.com/bogware/bog-agents/issues/70)) ([463db87](https://github.com/bogware/bog-agents/commit/463db87b04cb24275e73926b1bf64fa863df1091))

## [0.8.3](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.8.2...bog-agents-cli==0.8.3) (2026-05-05)


### Bug Fixes

* **cli:** slash subcommands — dispatch + autocomplete + markup rendering ([#67](https://github.com/bogware/bog-agents/issues/67)) ([9a819ce](https://github.com/bogware/bog-agents/commit/9a819ce2ab08df1913302ba4e80b09d7ea497005))

## [0.8.2](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.8.1...bog-agents-cli==0.8.2) (2026-05-04)


### Bug Fixes

* **cli,sdk:** kill ReadTimeouts on long turns + paste UX + ripgrep noise ([#65](https://github.com/bogware/bog-agents/issues/65)) ([b8f3498](https://github.com/bogware/bog-agents/commit/b8f349869dc0d6395a43a7edebdfe02385ede5c6))

## [0.8.1](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.8.0...bog-agents-cli==0.8.1) (2026-05-04)


### Features

* 0.7.0 - daemon/mcp/plugins/hardening ([#40](https://github.com/bogware/bog-agents/issues/40)) ([2427dfb](https://github.com/bogware/bog-agents/commit/2427dfbda3bffc17ea34f6e38de8d2634a57f86f))
* 0.8.0 — patient as still water ([#63](https://github.com/bogware/bog-agents/issues/63)) ([8b93798](https://github.com/bogware/bog-agents/commit/8b9379850e8c0360bb10dced6fe1dc83ebe9e11c))
* **cli:** add /init command, /onboard handler, CLAUDE.md context ([2a97670](https://github.com/bogware/bog-agents/commit/2a97670c13cf45c111eb8e0cd383d19dc6e128cb))
* **cli:** add /init command, /onboard handler, CLAUDE.md context loa… ([2a97670](https://github.com/bogware/bog-agents/commit/2a97670c13cf45c111eb8e0cd383d19dc6e128cb))
* **cli:** add /init command, /onboard handler, CLAUDE.md context loading ([b1f161e](https://github.com/bogware/bog-agents/commit/b1f161e683260be09f2e38b64180db2206a5e1fa))
* **cli:** add Bedrock SSO support, provider fallback chain, and /settings command ([17e6e5f](https://github.com/bogware/bog-agents/commit/17e6e5fc92bb7ec8879aa94e0e04b29454a2c3a9))
* **cli:** add Bedrock SSO support, provider fallback, /settings command, themes ([0efa283](https://github.com/bogware/bog-agents/commit/0efa283be6236b2bb03004eb8e2cf1a758738c21))
* **cli:** add Bedrock SSO support, provider fallback, /settings command, themes ([0efa283](https://github.com/bogware/bog-agents/commit/0efa283be6236b2bb03004eb8e2cf1a758738c21))
* **cli:** add deeper slash support/ui fixes/etc ([#35](https://github.com/bogware/bog-agents/issues/35)) ([5a0dbe3](https://github.com/bogware/bog-agents/commit/5a0dbe39c99a9d905ae146adf12f51a732d5ac9a))
* **cli:** Bedrock credential check modes and provider model fixes ([#26](https://github.com/bogware/bog-agents/issues/26)) ([39e8fb2](https://github.com/bogware/bog-agents/commit/39e8fb2f556741e2354a6f5c857939caaf763790))
* **cli:** comprehensive docs, help screen, and rebrand sweep ([c053d58](https://github.com/bogware/bog-agents/commit/c053d589d1bbc69ef10aa82c6d6bbb294699ce87))
* **cli:** Comprehensive docs, help screen, fixes ([bd7c325](https://github.com/bogware/bog-agents/commit/bd7c325802bc4fd67a6b9c37e107830b39d90027))
* **cli:** Comprehensive docs, help screen, fixes ([bd7c325](https://github.com/bogware/bog-agents/commit/bd7c325802bc4fd67a6b9c37e107830b39d90027))
* **cli:** green theme, &lt;&gt; prompt prefix, and updated tagline ([7ac023b](https://github.com/bogware/bog-agents/commit/7ac023b83c4e4e67c020a2331e5e0e1c526ef406))
* **cli:** resilient credential detection and interactive setup wizard ([8460244](https://github.com/bogware/bog-agents/commit/8460244720a6f1dfda9df11a1cf511d540470708))
* **cli:** wire serve, PR, background, dashboard, and /recommend command ([c7f2fff](https://github.com/bogware/bog-agents/commit/c7f2fff7c0df7e4965e54b38142fdde12fb233ac))
* prompts/pipelines/bugfixes ([#38](https://github.com/bogware/bog-agents/issues/38)) ([1191992](https://github.com/bogware/bog-agents/commit/1191992cca613282ae792063269159d729a08194))
* **sdk,cli:** add 17 killer features — middleware, serve API, CLI tools ([ab55111](https://github.com/bogware/bog-agents/commit/ab551118815438e538ae2dcbc1cdebcff84a49c5))
* **sdk,cli:** complete all 5 recommendations for production readiness ([de02cd6](https://github.com/bogware/bog-agents/commit/de02cd650ca927dec7c2769b1a0cfa9ca209f4a0))
* verify/call subcommands, shell reliability, cross-platform hardening, 12 CVEs closed ([#51](https://github.com/bogware/bog-agents/issues/51)) ([5f13fb4](https://github.com/bogware/bog-agents/commit/5f13fb4de5aa7cb50731b634796f0732a8a25f65))


### Bug Fixes

* 0.7.2 patch — memory/skills regression, ollama UX, partner resilience, Windows hardening ([#46](https://github.com/bogware/bog-agents/issues/46)) ([650975e](https://github.com/bogware/bog-agents/commit/650975e951997146a6299b8bcacb87e7c88389e5))
* 0.7.5 follow-up — resolve issue [#60](https://github.com/bogware/bog-agents/issues/60) (9 bug fixes) + dep-pin range ([#61](https://github.com/bogware/bog-agents/issues/61)) ([0a1c026](https://github.com/bogware/bog-agents/commit/0a1c0261dc58c55c20e2c5f89f3d95f00f4186db))
* **ci:** update uv from 0.5.25 to 0.8.17 to match lockfile format ([#33](https://github.com/bogware/bog-agents/issues/33)) ([59f2ada](https://github.com/bogware/bog-agents/commit/59f2adac2b54fa260bb15ca95b2df132e1e9014c))
* **cli,sdk:** fix lint errors and failing test ([a6d2f58](https://github.com/bogware/bog-agents/commit/a6d2f58931ccb823f8ed64c54b1c91f4d36f86e2))
* **cli:** fix Windows-incompat URI/polish ([5e714f5](https://github.com/bogware/bog-agents/commit/5e714f5c02117ff334d1cc7de757240f6377fa2d))
* **cli:** fix Windows-incompat URI/polish ([5e714f5](https://github.com/bogware/bog-agents/commit/5e714f5c02117ff334d1cc7de757240f6377fa2d))
* **cli:** fix Windows-incompatible file URI and process termination ([bb24222](https://github.com/bogware/bog-agents/commit/bb2422250c30b631424693bdbc3067a4636f8008))
* **cli:** remove tavily from required dep check, fix broken tests, rewrite README ([0753160](https://github.com/bogware/bog-agents/commit/0753160c91be33b3a85c1db6590198b268cf8258))
* **cli:** resolve merge conflicts with main (v0.5.2) ([42aa364](https://github.com/bogware/bog-agents/commit/42aa3640935c5dbca45cc750ac4da29d33308025))
* **cli:** resolve ruff lint errors in config.py setup wizard ([41125e5](https://github.com/bogware/bog-agents/commit/41125e5bb7bfd30c77214da331269e5d341be335))
* **cli:** survive ReadTimeout from langgraph SSE stream ([#58](https://github.com/bogware/bog-agents/issues/58)) ([233ae54](https://github.com/bogware/bog-agents/commit/233ae54ef8aab22f2d25a7a5296c8822791146ed))
* **cli:** switch Bedrock provider from bedrock to bedrock_converse  ([#31](https://github.com/bogware/bog-agents/issues/31)) ([77bcca3](https://github.com/bogware/bog-agents/commit/77bcca31db3d58e98958184a8075295ea6c31b3b))
* **cli:** use compatible version range for SDK dependency ([326a9e8](https://github.com/bogware/bog-agents/commit/326a9e82bd0344826ba15a3980160e8c7ee12291))
* **cli:** use compatible version range for SDK dependency ([326a9e8](https://github.com/bogware/bog-agents/commit/326a9e82bd0344826ba15a3980160e8c7ee12291))
* **cli:** use compatible version range for SDK dependency ([64bef09](https://github.com/bogware/bog-agents/commit/64bef09942a1ff1784d58cd6e6b2edc4188adb49))
* **cli:** use relative module refs for LangGraph server graph loading ([66e0dcc](https://github.com/bogware/bog-agents/commit/66e0dcc32f381b13a359147b2bd34ee557757aba))
* **sdk,cli:** CTO review — fix 11 production readiness issues ([ed3be04](https://github.com/bogware/bog-agents/commit/ed3be044bf4f30f4ca6cbe47a38d4e47c5852920))
* **sdk,cli:** fix 3 runtime bugs found in hands-on testing ([3a002af](https://github.com/bogware/bog-agents/commit/3a002affe19eb907981ae246521669428299e453))
* **sdk:** architect review — fix 5 bugs, add lazy imports, add serve deps ([1a17d78](https://github.com/bogware/bog-agents/commit/1a17d78e6c43dcdc5bee6172aaa1cdc62397b927))

## [0.7.6](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.7.5...bog-agents-cli==0.7.6) (2026-05-02)


### Bug Fixes

* 0.7.5 follow-up — resolve issue [#60](https://github.com/bogware/bog-agents/issues/60) (9 bug fixes) + dep-pin range ([#61](https://github.com/bogware/bog-agents/issues/61)) ([0a1c026](https://github.com/bogware/bog-agents/commit/0a1c0261dc58c55c20e2c5f89f3d95f00f4186db))

## [0.7.5](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.7.4...bog-agents-cli==0.7.5) (2026-05-01)


### Bug Fixes

* **cli:** survive ReadTimeout from langgraph SSE stream ([#58](https://github.com/bogware/bog-agents/issues/58)) ([233ae54](https://github.com/bogware/bog-agents/commit/233ae54ef8aab22f2d25a7a5296c8822791146ed))

## [0.7.4](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.7.3...bog-agents-cli==0.7.4) (2026-04-30)


### Bug Fixes

* **bedrock:** add `auth_mode` toggle (`auto`/`sso`/`static`/`profile`/`iam`) plus auto-fallback from expired SSO to static credentials when `~/.aws/config` short-circuits the credential chain ([#54](https://github.com/bogware/bog-agents/issues/54)) ([fef8228](https://github.com/bogware/bog-agents/commit/fef82283e9fc07f5d286a26eea093e68d28cdb42))
* **bedrock:** cache probe failures so a single expired SSO session no longer logs 20+ identical TokenRetrievalError tracebacks ([#53](https://github.com/bogware/bog-agents/issues/53)) ([fef8228](https://github.com/bogware/bog-agents/commit/fef82283e9fc07f5d286a26eea093e68d28cdb42))
* **bedrock:** add `langchain-aws` + AWS credential probe to `--doctor`; pre-flight credential check in `-n` mode surfaces SSO-expired errors as a clean stderr line instead of a wrapped RemoteException ([fef8228](https://github.com/bogware/bog-agents/commit/fef82283e9fc07f5d286a26eea093e68d28cdb42))
* **cli:** install atexit + signal handlers in `cli_main()` to emit terminal-restore sequences (disable mouse tracking, leave alternate screen, show cursor) so a Textual crash mid-launch no longer leaves SGR mouse-protocol garbage like `[<35;57;14M[` in the user's shell input line ([fef8228](https://github.com/bogware/bog-agents/commit/fef82283e9fc07f5d286a26eea093e68d28cdb42))


### Catalog

* refresh provider model lists against live docs (2026-04-30): Anthropic Opus 4.7 / Sonnet 4.6 / Haiku 4.5 + legacy 4.6/4.5/4.1; Bedrock `us.*` inference-profile IDs + base IDs for Anthropic + Amazon Nova (Premier/Pro/Lite/Micro) + Meta Llama 4 Maverick/Scout + 3.3 + Mistral Large 3 / Pixtral Large; Google Gemini 2.5 Pro/Flash/Flash-Lite + Gemini 3.1 preview family ([fef8228](https://github.com/bogware/bog-agents/commit/fef82283e9fc07f5d286a26eea093e68d28cdb42))

## [0.7.3](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.7.2...bog-agents-cli==0.7.3) (2026-04-29)


### Features

* verify/call subcommands, shell reliability, cross-platform hardening, 12 CVEs closed ([#51](https://github.com/bogware/bog-agents/issues/51)) ([5f13fb4](https://github.com/bogware/bog-agents/commit/5f13fb4de5aa7cb50731b634796f0732a8a25f65))

## [0.7.2](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.7.1...bog-agents-cli==0.7.2) (2026-04-25)


### Bug Fixes

* 0.7.2 patch — memory/skills regression, ollama UX, partner resilience, Windows hardening ([#46](https://github.com/bogware/bog-agents/issues/46)) ([650975e](https://github.com/bogware/bog-agents/commit/650975e951997146a6299b8bcacb87e7c88389e5))

## [0.7.1](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.7.0...bog-agents-cli==0.7.1) (2026-04-20)


### Features

* 0.7.0 - daemon/mcp/plugins/hardening ([#40](https://github.com/bogware/bog-agents/issues/40)) ([2427dfb](https://github.com/bogware/bog-agents/commit/2427dfbda3bffc17ea34f6e38de8d2634a57f86f))
* **cli:** add /init command, /onboard handler, CLAUDE.md context ([2a97670](https://github.com/bogware/bog-agents/commit/2a97670c13cf45c111eb8e0cd383d19dc6e128cb))
* **cli:** add /init command, /onboard handler, CLAUDE.md context loa… ([2a97670](https://github.com/bogware/bog-agents/commit/2a97670c13cf45c111eb8e0cd383d19dc6e128cb))
* **cli:** add /init command, /onboard handler, CLAUDE.md context loading ([b1f161e](https://github.com/bogware/bog-agents/commit/b1f161e683260be09f2e38b64180db2206a5e1fa))
* **cli:** add Bedrock SSO support, provider fallback chain, and /settings command ([17e6e5f](https://github.com/bogware/bog-agents/commit/17e6e5fc92bb7ec8879aa94e0e04b29454a2c3a9))
* **cli:** add Bedrock SSO support, provider fallback, /settings command, themes ([0efa283](https://github.com/bogware/bog-agents/commit/0efa283be6236b2bb03004eb8e2cf1a758738c21))
* **cli:** add Bedrock SSO support, provider fallback, /settings command, themes ([0efa283](https://github.com/bogware/bog-agents/commit/0efa283be6236b2bb03004eb8e2cf1a758738c21))
* **cli:** add deeper slash support/ui fixes/etc ([#35](https://github.com/bogware/bog-agents/issues/35)) ([5a0dbe3](https://github.com/bogware/bog-agents/commit/5a0dbe39c99a9d905ae146adf12f51a732d5ac9a))
* **cli:** Bedrock credential check modes and provider model fixes ([#26](https://github.com/bogware/bog-agents/issues/26)) ([39e8fb2](https://github.com/bogware/bog-agents/commit/39e8fb2f556741e2354a6f5c857939caaf763790))
* **cli:** comprehensive docs, help screen, and rebrand sweep ([c053d58](https://github.com/bogware/bog-agents/commit/c053d589d1bbc69ef10aa82c6d6bbb294699ce87))
* **cli:** Comprehensive docs, help screen, fixes ([bd7c325](https://github.com/bogware/bog-agents/commit/bd7c325802bc4fd67a6b9c37e107830b39d90027))
* **cli:** Comprehensive docs, help screen, fixes ([bd7c325](https://github.com/bogware/bog-agents/commit/bd7c325802bc4fd67a6b9c37e107830b39d90027))
* **cli:** green theme, &lt;&gt; prompt prefix, and updated tagline ([7ac023b](https://github.com/bogware/bog-agents/commit/7ac023b83c4e4e67c020a2331e5e0e1c526ef406))
* **cli:** resilient credential detection and interactive setup wizard ([8460244](https://github.com/bogware/bog-agents/commit/8460244720a6f1dfda9df11a1cf511d540470708))
* **cli:** wire serve, PR, background, dashboard, and /recommend command ([c7f2fff](https://github.com/bogware/bog-agents/commit/c7f2fff7c0df7e4965e54b38142fdde12fb233ac))
* prompts/pipelines/bugfixes ([#38](https://github.com/bogware/bog-agents/issues/38)) ([1191992](https://github.com/bogware/bog-agents/commit/1191992cca613282ae792063269159d729a08194))
* **sdk,cli:** add 17 killer features — middleware, serve API, CLI tools ([ab55111](https://github.com/bogware/bog-agents/commit/ab551118815438e538ae2dcbc1cdebcff84a49c5))
* **sdk,cli:** complete all 5 recommendations for production readiness ([de02cd6](https://github.com/bogware/bog-agents/commit/de02cd650ca927dec7c2769b1a0cfa9ca209f4a0))


### Bug Fixes

* **ci:** update uv from 0.5.25 to 0.8.17 to match lockfile format ([#33](https://github.com/bogware/bog-agents/issues/33)) ([59f2ada](https://github.com/bogware/bog-agents/commit/59f2adac2b54fa260bb15ca95b2df132e1e9014c))
* **cli,sdk:** fix lint errors and failing test ([a6d2f58](https://github.com/bogware/bog-agents/commit/a6d2f58931ccb823f8ed64c54b1c91f4d36f86e2))
* **cli:** fix Windows-incompat URI/polish ([5e714f5](https://github.com/bogware/bog-agents/commit/5e714f5c02117ff334d1cc7de757240f6377fa2d))
* **cli:** fix Windows-incompat URI/polish ([5e714f5](https://github.com/bogware/bog-agents/commit/5e714f5c02117ff334d1cc7de757240f6377fa2d))
* **cli:** fix Windows-incompatible file URI and process termination ([bb24222](https://github.com/bogware/bog-agents/commit/bb2422250c30b631424693bdbc3067a4636f8008))
* **cli:** remove tavily from required dep check, fix broken tests, rewrite README ([0753160](https://github.com/bogware/bog-agents/commit/0753160c91be33b3a85c1db6590198b268cf8258))
* **cli:** resolve merge conflicts with main (v0.5.2) ([42aa364](https://github.com/bogware/bog-agents/commit/42aa3640935c5dbca45cc750ac4da29d33308025))
* **cli:** resolve ruff lint errors in config.py setup wizard ([41125e5](https://github.com/bogware/bog-agents/commit/41125e5bb7bfd30c77214da331269e5d341be335))
* **cli:** switch Bedrock provider from bedrock to bedrock_converse  ([#31](https://github.com/bogware/bog-agents/issues/31)) ([77bcca3](https://github.com/bogware/bog-agents/commit/77bcca31db3d58e98958184a8075295ea6c31b3b))
* **cli:** use compatible version range for SDK dependency ([326a9e8](https://github.com/bogware/bog-agents/commit/326a9e82bd0344826ba15a3980160e8c7ee12291))
* **cli:** use compatible version range for SDK dependency ([326a9e8](https://github.com/bogware/bog-agents/commit/326a9e82bd0344826ba15a3980160e8c7ee12291))
* **cli:** use compatible version range for SDK dependency ([64bef09](https://github.com/bogware/bog-agents/commit/64bef09942a1ff1784d58cd6e6b2edc4188adb49))
* **cli:** use relative module refs for LangGraph server graph loading ([66e0dcc](https://github.com/bogware/bog-agents/commit/66e0dcc32f381b13a359147b2bd34ee557757aba))
* **sdk,cli:** CTO review — fix 11 production readiness issues ([ed3be04](https://github.com/bogware/bog-agents/commit/ed3be044bf4f30f4ca6cbe47a38d4e47c5852920))
* **sdk,cli:** fix 3 runtime bugs found in hands-on testing ([3a002af](https://github.com/bogware/bog-agents/commit/3a002affe19eb907981ae246521669428299e453))
* **sdk:** architect review — fix 5 bugs, add lazy imports, add serve deps ([1a17d78](https://github.com/bogware/bog-agents/commit/1a17d78e6c43dcdc5bee6172aaa1cdc62397b927))

## [0.6.6](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.6.5...bog-agents-cli==0.6.6) (2026-04-16)


### Features

* prompts/pipelines/bugfixes ([#38](https://github.com/bogware/bog-agents/issues/38)) ([1191992](https://github.com/bogware/bog-agents/commit/1191992cca613282ae792063269159d729a08194))

## [0.6.5](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.6.4...bog-agents-cli==0.6.5) (2026-04-14)


### Features

* **cli:** add deeper slash support/ui fixes/etc ([#35](https://github.com/bogware/bog-agents/issues/35)) ([5a0dbe3](https://github.com/bogware/bog-agents/commit/5a0dbe39c99a9d905ae146adf12f51a732d5ac9a))

## [0.6.4](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.6.3...bog-agents-cli==0.6.4) (2026-03-29)


### Bug Fixes

* **ci:** update uv from 0.5.25 to 0.8.17 to match lockfile format ([#33](https://github.com/bogware/bog-agents/issues/33)) ([59f2ada](https://github.com/bogware/bog-agents/commit/59f2adac2b54fa260bb15ca95b2df132e1e9014c))
* **cli:** switch Bedrock provider from bedrock to bedrock_converse  ([#31](https://github.com/bogware/bog-agents/issues/31)) ([77bcca3](https://github.com/bogware/bog-agents/commit/77bcca31db3d58e98958184a8075295ea6c31b3b))

## [0.6.3](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.6.2...bog-agents-cli==0.6.3) (2026-03-27)


### Features

* **cli:** Bedrock credential check modes and provider model fixes ([#26](https://github.com/bogware/bog-agents/issues/26)) ([39e8fb2](https://github.com/bogware/bog-agents/commit/39e8fb2f556741e2354a6f5c857939caaf763790))

## [0.6.0](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.5.2...bog-agents-cli==0.6.0) (2026-03-24)


### Features

* **cli:** comprehensive docs, help screen, and rebrand sweep ([c053d58](https://github.com/bogware/bog-agents/commit/c053d58))
* **cli:** resilient credential detection and interactive setup wizard ([8460244](https://github.com/bogware/bog-agents/commit/8460244))


### Bug Fixes

* **cli:** resolve merge conflicts with main (v0.5.2) ([42aa364](https://github.com/bogware/bog-agents/commit/42aa364))
* **cli,sdk:** fix lint errors and failing test ([a6d2f58](https://github.com/bogware/bog-agents/commit/a6d2f58))

## [0.5.2](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.5.1...bog-agents-cli==0.5.2) (2026-03-23)


### Features

* **cli:** wire serve, PR, background, dashboard, and /recommend command ([c7f2fff](https://github.com/bogware/bog-agents/commit/c7f2fff7c0df7e4965e54b38142fdde12fb233ac))
* **sdk,cli:** add 17 killer features — middleware, serve API, CLI tools ([ab55111](https://github.com/bogware/bog-agents/commit/ab551118815438e538ae2dcbc1cdebcff84a49c5))
* **sdk,cli:** complete all 5 recommendations for production readiness ([de02cd6](https://github.com/bogware/bog-agents/commit/de02cd650ca927dec7c2769b1a0cfa9ca209f4a0))


### Bug Fixes

* **cli:** remove tavily from required dep check, fix broken tests, rewrite README ([0753160](https://github.com/bogware/bog-agents/commit/0753160c91be33b3a85c1db6590198b268cf8258))
* **sdk,cli:** CTO review — fix 11 production readiness issues ([ed3be04](https://github.com/bogware/bog-agents/commit/ed3be044bf4f30f4ca6cbe47a38d4e47c5852920))
* **sdk,cli:** fix 3 runtime bugs found in hands-on testing ([3a002af](https://github.com/bogware/bog-agents/commit/3a002affe19eb907981ae246521669428299e453))
* **sdk:** architect review — fix 5 bugs, add lazy imports, add serve deps ([1a17d78](https://github.com/bogware/bog-agents/commit/1a17d78e6c43dcdc5bee6172aaa1cdc62397b927))

## [0.5.1](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.5.0...bog-agents-cli==0.5.1) (2026-03-23)


### Features

* **cli:** wire serve, PR, background, dashboard, and /recommend command ([c7f2fff](https://github.com/bogware/bog-agents/commit/c7f2fff7c0df7e4965e54b38142fdde12fb233ac))
* **sdk,cli:** add 17 killer features — middleware, serve API, CLI tools ([ab55111](https://github.com/bogware/bog-agents/commit/ab551118815438e538ae2dcbc1cdebcff84a49c5))
* **sdk,cli:** complete all 5 recommendations for production readiness ([de02cd6](https://github.com/bogware/bog-agents/commit/de02cd650ca927dec7c2769b1a0cfa9ca209f4a0))


### Bug Fixes

* **cli:** remove tavily from required dep check, fix broken tests, rewrite README ([0753160](https://github.com/bogware/bog-agents/commit/0753160c91be33b3a85c1db6590198b268cf8258))
* **sdk,cli:** CTO review — fix 11 production readiness issues ([ed3be04](https://github.com/bogware/bog-agents/commit/ed3be044bf4f30f4ca6cbe47a38d4e47c5852920))
* **sdk,cli:** fix 3 runtime bugs found in hands-on testing ([3a002af](https://github.com/bogware/bog-agents/commit/3a002affe19eb907981ae246521669428299e453))
* **sdk:** architect review — fix 5 bugs, add lazy imports, add serve deps ([1a17d78](https://github.com/bogware/bog-agents/commit/1a17d78e6c43dcdc5bee6172aaa1cdc62397b927))

## [0.0.32](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.31...bog-agents-cli==0.0.32) (2026-03-11)

### Features

* Add token breakdown to `/tokens` and simplify `/compact` messages ([#1782](https://github.com/bogware/bog-agents/issues/1782)) ([2f37bff](https://github.com/bogware/bog-agents/commit/2f37bffa9d7a9ced6945abe4ab6bc3409bfb97b1))
* `--json` flag for machine-readable output ([#1768](https://github.com/bogware/bog-agents/issues/1768)) ([6f62496](https://github.com/bogware/bog-agents/commit/6f62496bb699dfa6086ee1850b83f38d3b1242fa))

### Bug Fixes

* Work around VS Code 1.110 space key regression ([#1748](https://github.com/bogware/bog-agents/issues/1748)) ([f5fe431](https://github.com/bogware/bog-agents/commit/f5fe4315143bf5b636cf42fc98cbfe3d99918cfc))

## [0.0.31](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.30...bog-agents-cli==0.0.31) (2026-03-09)

### Features

* Opt-in `ask_user` tool for interactive agent questions ([#1377](https://github.com/bogware/bog-agents/issues/1377)) ([de7068d](https://github.com/bogware/bog-agents/commit/de7068d21fd4b932c6e53f500b0ea3b02a04c0aa))
* Big thread improvements!
  * Rework `/thread` switcher with search, columns, delete, and sort toggle ([#1723](https://github.com/bogware/bog-agents/issues/1723)) ([8b21ddb](https://github.com/bogware/bog-agents/commit/8b21ddb2ff7f13d6b3ffcbf2fe605bfbadbc3d38))
  * Track and display working directory per thread ([#1735](https://github.com/bogware/bog-agents/issues/1735)) ([0e4f25d](https://github.com/bogware/bog-agents/commit/0e4f25dfbc3e15653bc3f8a6d32a0a61ead4ba82))
  * Add `-n` short flag for `threads list --limit` ([#1731](https://github.com/bogware/bog-agents/issues/1731)) ([8bbace9](https://github.com/bogware/bog-agents/commit/8bbace9facd1e33757521e835dcb291accd2fa91))
  * Add sort, branch filter, and verbose flags to threads list ([#1732](https://github.com/bogware/bog-agents/issues/1732)) ([11dc8e3](https://github.com/bogware/bog-agents/commit/11dc8e3397ef9e9dbe8b15578e9258544ed6b452))
* Tailor system prompt for non-interactive mode ([#1727](https://github.com/bogware/bog-agents/issues/1727)) ([871e5cf](https://github.com/bogware/bog-agents/commit/871e5cf76b1a7e7cf7175b4415bb8e2206da39ec))
* `/reload` command for in-session config refresh ([#1722](https://github.com/bogware/bog-agents/issues/1722)) ([381aee6](https://github.com/bogware/bog-agents/commit/381aee6d223fe3d866bedfe3a534916f419a4435))
* Rearrange HITL option order in approval menu ([#1726](https://github.com/bogware/bog-agents/issues/1726)) ([0ca6cb2](https://github.com/bogware/bog-agents/commit/0ca6cb237b6da538bad2b4bf292942c8db72ec1f))

### Bug Fixes

* Localize newline shortcut labels by platform ([#1721](https://github.com/bogware/bog-agents/issues/1721)) ([f35576b](https://github.com/bogware/bog-agents/commit/f35576bafac711d6c04f1f9dd40ec97a90e30060))
* Prevent `shift+enter` from sending `backslash+enter` ([#1728](https://github.com/bogware/bog-agents/issues/1728)) ([81dceb0](https://github.com/bogware/bog-agents/commit/81dceb043097a47702bb5a0227a8f12e9055bd05))
* Write files with langsmith sandbox ([#1714](https://github.com/bogware/bog-agents/issues/1714)) ([5933c9e](https://github.com/bogware/bog-agents/commit/5933c9e2995c422e43649c61981e086ac1eaf725))

## [0.0.30](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.29...bog-agents-cli==0.0.30) (2026-03-07)

### Features

* `--acp` mode to run CLI agent as ACP server ([#1297](https://github.com/bogware/bog-agents/issues/1297)) ([c9ba00a](https://github.com/bogware/bog-agents/commit/c9ba00a56b7ee5e48b56b13f9f093bb8bf639700))
* Model detail footer + persist `--profile-override` on hot-swap ([#1700](https://github.com/bogware/bog-agents/issues/1700)) ([f2c8b54](https://github.com/bogware/bog-agents/commit/f2c8b54e9b4c541bf6f91139bfb9b6a2f20c8de0))
* Show message timestamp toast on click ([#1702](https://github.com/bogware/bog-agents/issues/1702)) ([4f403ec](https://github.com/bogware/bog-agents/commit/4f403ecb3332010062158ec30fd55f349654a533))

### Bug Fixes

* Expire `ctrl+c` quit window when toast disappears ([#1701](https://github.com/bogware/bog-agents/issues/1701)) ([38b5ea9](https://github.com/bogware/bog-agents/commit/38b5ea9484ab121c9b2919dd74469e82fce19b82))
* Preserve input text when escaping shell/command mode ([#1706](https://github.com/bogware/bog-agents/issues/1706)) ([3c00edb](https://github.com/bogware/bog-agents/commit/3c00edb93eddf74e87d58526a02be72577ed65b1))
* Right-align token count next to model name in status bar ([#1705](https://github.com/bogware/bog-agents/issues/1705)) ([311c919](https://github.com/bogware/bog-agents/commit/311c9191cf663540e1b62eb9452abecda5bc7b4f))

## [0.0.29](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.28...bog-agents-cli==0.0.29) (2026-03-06)

### Features

* `--model-params` flag on `/model` command ([#1679](https://github.com/bogware/bog-agents/issues/1679)) ([9b6433d](https://github.com/bogware/bog-agents/commit/9b6433d557e6e8b3d39c10577595b0ef6d741c94))
* `--shell-allow-list all` ([#1695](https://github.com/bogware/bog-agents/issues/1695)) ([4aec7b3](https://github.com/bogware/bog-agents/commit/4aec7b35caa7723b8bbda189c9ca1d213e0a9a6d))
* Hook dispatch for external tool integration ([#1553](https://github.com/bogware/bog-agents/issues/1553)) ([cdb2230](https://github.com/bogware/bog-agents/commit/cdb2230f04ce7a2b7ef0837cbbc223dcbf04b78e))
* Detect deceptive unicode in tool args and URLs ([#1694](https://github.com/bogware/bog-agents/issues/1694)) ([d4c8544](https://github.com/bogware/bog-agents/commit/d4c8544bd6bf3b6df50b99f8a0c7208c20f86bd9))
* MCP tool loading with auto-discovery ([#801](https://github.com/bogware/bog-agents/issues/801)) ([df0908e](https://github.com/bogware/bog-agents/commit/df0908ebed4e17f0fd904d83e9d4ea38dfc1207d))
  * Surface mcp server/tool info in system prompt ([#1693](https://github.com/bogware/bog-agents/issues/1693)) ([068e075](https://github.com/bogware/bog-agents/commit/068e075ecd4a7f3e35219ae6b87707bd9dc3f785))

### Bug Fixes

* Anchor `ChatInput` below scrollable area ([#1671](https://github.com/bogware/bog-agents/issues/1671)) ([11105d9](https://github.com/bogware/bog-agents/commit/11105d93f593d802d5e120c095f16d771c674bef))
  * Remove dead chat-spacer widget and resize handler ([#1686](https://github.com/bogware/bog-agents/issues/1686)) ([b6ecec5](https://github.com/bogware/bog-agents/commit/b6ecec5bd14677a878c92a1b51e950f61fabf8d3))

## [0.0.28](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.27...bog-agents-cli==0.0.28) (2026-03-05)

### Features

* Video support to multimodal inputs ([#1521](https://github.com/bogware/bog-agents/issues/1521)) ([f9b49b7](https://github.com/bogware/bog-agents/commit/f9b49b7341bd42b5278a03496743e4709689598e))
* NVIDIA API key support and default model ([#1577](https://github.com/bogware/bog-agents/issues/1577)) ([9ce2660](https://github.com/bogware/bog-agents/commit/9ce2660a67c3497cff18d27131fb7ef49e85b310))
* Fuzzy search for slash command autocomplete ([#1660](https://github.com/bogware/bog-agents/issues/1660)) ([5f6e9c0](https://github.com/bogware/bog-agents/commit/5f6e9c014e6a99783b3113184cc12f0179a902f0))
* Tab autocomplete in model selector ([#1669](https://github.com/bogware/bog-agents/issues/1669)) ([28bd0aa](https://github.com/bogware/bog-agents/commit/28bd0aaca737b8bb194ecb9f6612989b9aacec02))

### Bug Fixes

* Backspace at cursor position 0 exits mode even with text ([#1666](https://github.com/bogware/bog-agents/issues/1666)) ([dfa4c1f](https://github.com/bogware/bog-agents/commit/dfa4c1fedcecf2bb17d8ffef01cf50efe6c80fb0))
* Skip auto-approve toggle when modal screen is open ([#1668](https://github.com/bogware/bog-agents/issues/1668)) ([6597f0b](https://github.com/bogware/bog-agents/commit/6597f0b8da3c3bd701a42e228660d459cefe3f64))
* Truncate model name in status bar on narrow terminals ([#1665](https://github.com/bogware/bog-agents/issues/1665)) ([0e24a04](https://github.com/bogware/bog-agents/commit/0e24a04aa9e5894735522ce23295bb27fd2b8190))

## [0.0.27](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.26...bog-agents-cli==0.0.27) (2026-03-04)

### Features

* Background PyPI update check ([#1648](https://github.com/bogware/bog-agents/issues/1648)) ([2e7a5e7](https://github.com/bogware/bog-agents/commit/2e7a5e7d97f64147ab2d000fae833fe681f1d6b2))
* Install script ([#1649](https://github.com/bogware/bog-agents/issues/1649)) ([68f6ef9](https://github.com/bogware/bog-agents/commit/68f6ef96e7d66b2c98d1371e91e5d25f107b80fe))
* Fuzzy search for model switcher ([#1266](https://github.com/bogware/bog-agents/issues/1266)) ([a6bbb18](https://github.com/bogware/bog-agents/commit/a6bbb182a2336ba748d93a06b9fcf27966321e20))
* Model usage stats display ([#1587](https://github.com/bogware/bog-agents/issues/1587)) ([a1208db](https://github.com/bogware/bog-agents/commit/a1208db096761eb54e0fe712a5aa922502575cb6))
* Substring matching in command history navigation ([#1301](https://github.com/bogware/bog-agents/issues/1301)) ([e276d5a](https://github.com/bogware/bog-agents/commit/e276d5a64bee9394f53ab993b01447023bcd4c7d))

### Bug Fixes

* Allow Esc to exit command/bash input mode ([#1644](https://github.com/bogware/bog-agents/issues/1644)) ([906da72](https://github.com/bogware/bog-agents/commit/906da72ea40e16492f8e7f3c35758af486c92b3c))
* Make `!` bash commands interruptible via `Esc`/`Ctrl+C` ([#1638](https://github.com/bogware/bog-agents/issues/1638)) ([0c414d1](https://github.com/bogware/bog-agents/commit/0c414d154a74cfabebfae8fc2dbb6d7e39da3857))
* Make escape reject pending HITL approval first ([#1645](https://github.com/bogware/bog-agents/issues/1645)) ([5d7be0c](https://github.com/bogware/bog-agents/commit/5d7be0c1a2fbe54f7fe062c5a43a7591aecb00e4))
* Show cwd on startup ([#1209](https://github.com/bogware/bog-agents/issues/1209)) ([23032dd](https://github.com/bogware/bog-agents/commit/23032ddd80b0ec8bf58c91776e62b834f6e03b5e))
* Terminate active subprocesses on app quit ([#1646](https://github.com/bogware/bog-agents/issues/1646)) ([5f2e614](https://github.com/bogware/bog-agents/commit/5f2e614f05912d3278a988cb7366612099105acf))
* Use first-class OpenRouter attribution kwargs ([#1635](https://github.com/bogware/bog-agents/issues/1635)) ([9c1ed93](https://github.com/bogware/bog-agents/commit/9c1ed93861a52b9ced2c1426131d542f50afa623))

## [0.0.26](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.25...bog-agents-cli==0.0.26) (2026-03-03)

### Features

* Compaction hook ([#1420](https://github.com/bogware/bog-agents/issues/1420)) ([e87cdad](https://github.com/bogware/bog-agents/commit/e87cdaddb9a984c4fd189b4f71303881edb32cb2))
  * `/compact` command ([#1579](https://github.com/bogware/bog-agents/issues/1579)) ([46e9e95](https://github.com/bogware/bog-agents/commit/46e9e950087e973175d49d6a863cfa9d2f241528))
* `--profile-override` CLI flag ([#1605](https://github.com/bogware/bog-agents/issues/1605)) ([1984099](https://github.com/bogware/bog-agents/commit/1984099ae9ac4b0c13dc08722abb9d56055da7b7))
* Model profile overrides in config ([#1603](https://github.com/bogware/bog-agents/issues/1603)) ([d3d6899](https://github.com/bogware/bog-agents/commit/d3d6899209b7cf97447da0eee642b3f55261ffbc))
* Show summarization status and notification    ([#919](https://github.com/bogware/bog-agents/issues/919)) ([2e3cb74](https://github.com/bogware/bog-agents/commit/2e3cb743eff8e0a33b215359132cee13a673a4df))

### Bug Fixes

* Fix image path pasting qualms ([#1560](https://github.com/bogware/bog-agents/issues/1560)) ([8caaf3e](https://github.com/bogware/bog-agents/commit/8caaf3e71ae7f5a26c20ca86700cc51f3c6f37ed))
* Load `.agents` skill alias directories at interactive startup ([#1556](https://github.com/bogware/bog-agents/issues/1556)) ([af0a759](https://github.com/bogware/bog-agents/commit/af0a759ee231cfe8860da34fe39dbcff38726102))
* Coerce execute timeout to int before formatting tool display ([#1588](https://github.com/bogware/bog-agents/issues/1588)) ([04b8c72](https://github.com/bogware/bog-agents/commit/04b8c72361f7eb60b86fa560ef3f6283912c3395)), closes [#1586](https://github.com/bogware/bog-agents/issues/1586)
* Add missing flags to help screen ([#1619](https://github.com/bogware/bog-agents/issues/1619)) ([6067749](https://github.com/bogware/bog-agents/commit/60677492b3f49adc8535b34156029271a0728923))
* Align compaction messaging across `/compact` and `compact_conversation` ([#1583](https://github.com/bogware/bog-agents/issues/1583)) ([d455a6b](https://github.com/bogware/bog-agents/commit/d455a6b117dbca2dfb5156050273a84946adc247))
* Apply profile overrides in `/compact` ([#1612](https://github.com/bogware/bog-agents/issues/1612)) ([a9dc2c5](https://github.com/bogware/bog-agents/commit/a9dc2c5a1ad6d37f3f682491664b3f709cad8552))
* Disambiguate `/tokens` vs `/compact` token reporting ([#1618](https://github.com/bogware/bog-agents/issues/1618)) ([51c3347](https://github.com/bogware/bog-agents/commit/51c3347e5a402115d4ecbb09f0074c607270f992))
* Make LangSmith URL lookups non-blocking ([#1595](https://github.com/bogware/bog-agents/issues/1595)) ([572eaee](https://github.com/bogware/bog-agents/commit/572eaeefbe2f9318555733977e4771815879273c))
* Only exit input mode on backspace, not text clear ([#1479](https://github.com/bogware/bog-agents/issues/1479)) ([da0965e](https://github.com/bogware/bog-agents/commit/da0965ee33e6bdf7aec30865bed44a1bd38a7d12))
* Retry langsmith project url lookup until project exists ([#1562](https://github.com/bogware/bog-agents/issues/1562)) ([e137a63](https://github.com/bogware/bog-agents/commit/e137a633fdadda205b8e05a9fdabc4b978726a37))
* Show model info in `/tokens` before first usage ([#1607](https://github.com/bogware/bog-agents/issues/1607)) ([7b01ae7](https://github.com/bogware/bog-agents/commit/7b01ae7258ed079046262d1c174f1c406101294c))
* Support `timeout=0` for sandbox `execute()` ([#1558](https://github.com/bogware/bog-agents/issues/1558)) ([ed14443](https://github.com/bogware/bog-agents/commit/ed14443b5aec8afde1f74bb2e12a17cb7d1829b6))
* Unreachable `except` block ([#1535](https://github.com/bogware/bog-agents/issues/1535)) ([0e17e35](https://github.com/bogware/bog-agents/commit/0e17e352fa2ae4e34320a27d272586a10a0a7aec))

### Performance Improvements

* Optimize thread resume path with prefetch and batched hydration ([#1561](https://github.com/bogware/bog-agents/issues/1561)) ([068d112](https://github.com/bogware/bog-agents/commit/068d1128177de0f0a01f533a01184039c2a2f09f))
* Parallelize detection scripts for faster first-turn ([#1541](https://github.com/bogware/bog-agents/issues/1541)) ([dad8b6e](https://github.com/bogware/bog-agents/commit/dad8b6e15a78d26921c0cb831579648927caa551))
* Speed up `/threads` first-open ([#1481](https://github.com/bogware/bog-agents/issues/1481)) ([b248b15](https://github.com/bogware/bog-agents/commit/b248b15fd70de3c4d055b68a0dae04f00e41ea9e))

## [0.0.25](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.24...bog-agents-cli==0.0.25) (2026-02-20)

### Features

* Set OpenRouter headers, default to `gemini-3.1-pro-preview` ([#1455](https://github.com/bogware/bog-agents/issues/1455)) ([95c0b71](https://github.com/bogware/bog-agents/commit/95c0b71c2fafbec8424d92e7698563045a787866)), closes [#1454](https://github.com/bogware/bog-agents/issues/1454)

### Bug Fixes

* Duplicate paste issue ([#1460](https://github.com/bogware/bog-agents/issues/1460)) ([9177515](https://github.com/bogware/bog-agents/commit/9177515c8a968882e980d229fb546c9753475de7)), closes [#1425](https://github.com/bogware/bog-agents/issues/1425)
* Remove model fallback to env variables ([#1458](https://github.com/bogware/bog-agents/issues/1458)) ([c9b4275](https://github.com/bogware/bog-agents/commit/c9b4275e22fda5aa35b3ddce924277ec8aaa9e1f))

## [0.0.24](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.23...bog-agents-cli==0.0.24) (2026-02-20)

### Features

* Add single-click link opening for rich-style hyperlinks ([#1433](https://github.com/bogware/bog-agents/issues/1433)) ([ef1fd31](https://github.com/bogware/bog-agents/commit/ef1fd3115d77cd769e664d2ad0345623f9ce4019))
* Display model name and context window size using `/tokens` ([#1441](https://github.com/bogware/bog-agents/issues/1441)) ([ff7ef0f](https://github.com/bogware/bog-agents/commit/ff7ef0f87e6dfc6c581edb34b1a57be7ff6e059c))
* Refresh local context after summarization events ([#1384](https://github.com/bogware/bog-agents/issues/1384)) ([dcb9583](https://github.com/bogware/bog-agents/commit/dcb95839de360f03d2fc30c9144096874b24006f))
* Windowed thread hydration and configurable thread limit ([#1435](https://github.com/bogware/bog-agents/issues/1435)) ([9da8d0b](https://github.com/bogware/bog-agents/commit/9da8d0b5c86441e87b85ee6f8db1d23848a823ed))
* Per-command `timeout` override to `execute()` ([#1154](https://github.com/bogware/bog-agents/issues/1154)) ([49277d4](https://github.com/bogware/bog-agents/commit/49277d45a026c86b5bf176142dcb1dfc2c7643ae))

### Bug Fixes

* Escape `Rich` markup in shell command display ([#1413](https://github.com/bogware/bog-agents/issues/1413)) ([c330290](https://github.com/bogware/bog-agents/commit/c33029032a1e2072dab2d06e93953f2acaa6d400))
* Load root-level `AGENTS.md` into agent system prompt ([#1445](https://github.com/bogware/bog-agents/issues/1445)) ([047fa2c](https://github.com/bogware/bog-agents/commit/047fa2cadfb9f005410c21a6e1e3b3d59eadda7d))
* Prevent crash when quitting with queued messages ([#1421](https://github.com/bogware/bog-agents/issues/1421)) ([a3c9ae6](https://github.com/bogware/bog-agents/commit/a3c9ae681501cd3efca82573a8d20a0dc8c9b338))

## [0.0.23](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.22...bog-agents-cli==0.0.23) (2026-02-18)

### Features

* Add drag-and-drop image attachment to chat input ([#1386](https://github.com/bogware/bog-agents/issues/1386)) ([cd3d89b](https://github.com/bogware/bog-agents/commit/cd3d89b4419b4c164915ff745afff99cb11b55a5))
* Skill deletion command ([#580](https://github.com/bogware/bog-agents/issues/580)) ([40a8d86](https://github.com/bogware/bog-agents/commit/40a8d866f952e0cf8d856e2fa360de771721b99a))
* Add visual mode indicators to chat input ([#1371](https://github.com/bogware/bog-agents/issues/1371)) ([1ea6159](https://github.com/bogware/bog-agents/commit/1ea6159b068b8c7d721d90a5c196e2eb9877c1c5))
* Dismiss completion dropdown on `esc` ([#1362](https://github.com/bogware/bog-agents/issues/1362)) ([961b7fc](https://github.com/bogware/bog-agents/commit/961b7fc764a7fbf63466d78c1d80b154b5d1692b))
* Expand local context & implement via bash for sandbox support ([#1295](https://github.com/bogware/bog-agents/issues/1295)) ([de8bc7c](https://github.com/bogware/bog-agents/commit/de8bc7cbbd7780ef250b3838f61ace85d4465c0a))
* Show sdk version alongside cli version ([#1378](https://github.com/bogware/bog-agents/issues/1378)) ([e99b4c8](https://github.com/bogware/bog-agents/commit/e99b4c864afd01d68c3829304fb93cc0530eedee))
* Strip mode-trigger prefix from chat input text ([#1373](https://github.com/bogware/bog-agents/issues/1373)) ([6879eff](https://github.com/bogware/bog-agents/commit/6879effb37c2160ef3835cd2d058b79f9d3a5a99))

### Bug Fixes

* Path hardening ([#918](https://github.com/bogware/bog-agents/issues/918)) ([fc34a14](https://github.com/bogware/bog-agents/commit/fc34a144a2791c75f8b4c11f67dd1adbc029c81e))
* Only navigate prompt history at input boundaries ([#1385](https://github.com/bogware/bog-agents/issues/1385)) ([6d82d6d](https://github.com/bogware/bog-agents/commit/6d82d6de290e73b897a58d724f3dfc7a32a06cba))
* Substitute image base64 for placeholder in result block ([#1381](https://github.com/bogware/bog-agents/issues/1381)) ([54f4d8e](https://github.com/bogware/bog-agents/commit/54f4d8e834c4aad672d78b4130cd43f2454424fa))

### Performance Improvements

* Defer more heavy imports to speed up startup ([#1389](https://github.com/bogware/bog-agents/issues/1389)) ([4dd10d5](https://github.com/bogware/bog-agents/commit/4dd10d5c9f3cfe13cd7b9ac18a1799c0832976ff))

## [0.0.22](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.21...bog-agents-cli==0.0.22) (2026-02-17)

### Features

* Add `langchain-openrouter` ([#1340](https://github.com/bogware/bog-agents/issues/1340)) ([5b35247](https://github.com/bogware/bog-agents/commit/5b35247b126ed328e9562ac3a3c2acd184b39011))
* Update system & default prompt ([#1293](https://github.com/bogware/bog-agents/issues/1293)) ([2aeb092](https://github.com/bogware/bog-agents/commit/2aeb092e027affd9eaa8a78b33101e1fd930d444))
* Warn when ripgrep is not installed ([#1337](https://github.com/bogware/bog-agents/issues/1337)) ([0367efa](https://github.com/bogware/bog-agents/commit/0367efa323b7a29c015d6a3fbb5af8894dc724b8))
* Ensure dep group version match for CLI ([#1316](https://github.com/bogware/bog-agents/issues/1316)) ([db05de1](https://github.com/bogware/bog-agents/commit/db05de1b0c92208b9752f3f03fa5fa54813ab4ef))
* Enable type checking in `bog-agents` and resolve most linting issues ([#991](https://github.com/bogware/bog-agents/issues/991)) ([5c90376](https://github.com/bogware/bog-agents/commit/5c90376c02754c67d448908e55d1e953f54b8acd))

### Bug Fixes

* Handle `None` selection endpoint, `IndexError` in clipboard copy ([#1342](https://github.com/bogware/bog-agents/issues/1342)) ([5754031](https://github.com/bogware/bog-agents/commit/57540316cf928da3dcf4401fb54a5d0102045d67))

### Performance Improvements

* Defer heavy imports ([#1361](https://github.com/bogware/bog-agents/issues/1361)) ([dd992e4](https://github.com/bogware/bog-agents/commit/dd992e48feb3e3a9fc6fd93f56e9d8a9cb51c7bf))

## [0.0.21](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.20...bog-agents-cli==0.0.21) (2026-02-11)

### Features

* Support piped stdin as prompt input ([#1254](https://github.com/bogware/bog-agents/issues/1254)) ([cca61ff](https://github.com/bogware/bog-agents/commit/cca61ff5edb5e2424bfc54b2ac33b59a520fdd6a))
* `/threads` command switcher ([#1262](https://github.com/bogware/bog-agents/issues/1262)) ([45bf38d](https://github.com/bogware/bog-agents/commit/45bf38d7c5ca7ca05ec58c320494a692e419b632)), closes [#1111](https://github.com/bogware/bog-agents/issues/1111)
* Make thread link clickable when switching ([#1296](https://github.com/bogware/bog-agents/issues/1296)) ([9409520](https://github.com/bogware/bog-agents/commit/9409520d524c576c3b0b9686c96a1749ee9dcbbb)), closes [#1291](https://github.com/bogware/bog-agents/issues/1291)
* `/trace` command to open LangSmith thread, link in switcher ([#1291](https://github.com/bogware/bog-agents/issues/1291)) ([fbbd45b](https://github.com/bogware/bog-agents/commit/fbbd45b51be2cf09726a3cd0adfcb09cb2b1ff46))
* `/changelog`, `/feedback`, `/docs` ([#1261](https://github.com/bogware/bog-agents/issues/1261)) ([4561afb](https://github.com/bogware/bog-agents/commit/4561afbea17bb11f7fc02ae9f19db15229656280))
* Show langsmith thread url on session teardown ([#1285](https://github.com/bogware/bog-agents/issues/1285)) ([899fd1c](https://github.com/bogware/bog-agents/commit/899fd1cdea6f7b2003992abd3f6173d630849a90))

### Bug Fixes

* Fix stale model settings during model hot-swap ([#1257](https://github.com/bogware/bog-agents/issues/1257)) ([55c119c](https://github.com/bogware/bog-agents/commit/55c119cb6ce73db7cae0865172f00ab8fc9f8fc1))

## [0.0.20](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.19...bog-agents-cli==0.0.20) (2026-02-10)

### Features

* `--quiet` flag to suppress non-agent output w/ `-n` ([#1201](https://github.com/bogware/bog-agents/issues/1201)) ([3e96792](https://github.com/bogware/bog-agents/commit/3e967926655cf5249a1bc5ca3edd48da9dd3061b))
* Add docs link to `/help` ([#1098](https://github.com/bogware/bog-agents/issues/1098)) ([8f8fc98](https://github.com/bogware/bog-agents/commit/8f8fc98bd403d96d6ed95fce8906d9c881236613))
* Built-in skills, ship `skill-creator` as first ([#1191](https://github.com/bogware/bog-agents/issues/1191)) ([42823a8](https://github.com/bogware/bog-agents/commit/42823a88d1eb7242a5d9b3eba981f24b3ea9e274))
* Enrich built-in skill metadata with license and compatibility info ([#1193](https://github.com/bogware/bog-agents/issues/1193)) ([b8179c2](https://github.com/bogware/bog-agents/commit/b8179c23f9130c92cb1fb7c6b34d98cc32ec092a))
* Implement message queue for CLI ([#1197](https://github.com/bogware/bog-agents/issues/1197)) ([c4678d7](https://github.com/bogware/bog-agents/commit/c4678d7641785ac4f17045eb75d55f9dc44f37fe))
* Model switcher & arbitrary chat model support ([#1127](https://github.com/bogware/bog-agents/issues/1127)) ([28fc311](https://github.com/bogware/bog-agents/commit/28fc311da37881257e409149022f0717f78013ef))
* Non-interactive mode w/ shell allow-listing ([#909](https://github.com/bogware/bog-agents/issues/909)) ([433bd2c](https://github.com/bogware/bog-agents/commit/433bd2cb493d6c4b59f2833e4304eead0304195a))
* Support custom working directories and LangSmith sandbox templates ([#1099](https://github.com/bogware/bog-agents/issues/1099)) ([21e7150](https://github.com/bogware/bog-agents/commit/21e715054ea5cf48cab05319b2116509fbacd899))

### Bug Fixes

* `-m` initial prompt submission ([#1184](https://github.com/bogware/bog-agents/issues/1184)) ([a702e82](https://github.com/bogware/bog-agents/commit/a702e82a0f61edbadd78eff6906ecde20b601798))
* Align skill-creator example scripts with agent skills spec ([#1177](https://github.com/bogware/bog-agents/issues/1177)) ([199d176](https://github.com/bogware/bog-agents/commit/199d17676ac1bfee645908a6c58193291e522890))
* Harden dictionary iteration and HITL fallback handling ([#1151](https://github.com/bogware/bog-agents/issues/1151)) ([8b21fc6](https://github.com/bogware/bog-agents/commit/8b21fc6105d808ad25c53de96f339ab21efb4474))
* Per-subcommand help screens, short flags, and skills enhancements ([#1190](https://github.com/bogware/bog-agents/issues/1190)) ([3da1e8b](https://github.com/bogware/bog-agents/commit/3da1e8bc20bf39aba80f6507b9abc2352de38484))
* Port skills behavior from SDK ([#1192](https://github.com/bogware/bog-agents/issues/1192)) ([ad9241d](https://github.com/bogware/bog-agents/commit/ad9241da6e7e23e4430756a1d5a3afb6c6bfebcc)), closes [#1189](https://github.com/bogware/bog-agents/issues/1189)
* Rewrite skills create template to match spec guidance ([#1178](https://github.com/bogware/bog-agents/issues/1178)) ([f08ad52](https://github.com/bogware/bog-agents/commit/f08ad520172bd114e4cebf69138a10cbf98e157a))
* Terminal virtualize scrolling to stop perf issues ([#965](https://github.com/bogware/bog-agents/issues/965)) ([5633c82](https://github.com/bogware/bog-agents/commit/5633c825832a0e8bd645681db23e97af31879b65))
* Update splash thread ID on `/clear` ([#1204](https://github.com/bogware/bog-agents/issues/1204)) ([23651ed](https://github.com/bogware/bog-agents/commit/23651edbc236e4a68fb0d9496506e6293b836cd9))
* Refactor summarization middleware ([#1138](https://github.com/bogware/bog-agents/issues/1138)) ([e87001e](https://github.com/bogware/bog-agents/commit/e87001eace2852c2df47095ffd2611f09fdda2f5))

## [0.0.19](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.18...bog-agents-cli==0.0.19) (2026-02-06)

### Features

* Add click support and hover styling to autocomplete popup ([#1130](https://github.com/bogware/bog-agents/issues/1130)) ([b1cc83d](https://github.com/bogware/bog-agents/commit/b1cc83d277e01614b0cc4141993cde40ce68d632))
* Per-command `timeout` override to `execute` tool ([#1158](https://github.com/bogware/bog-agents/issues/1158)) ([cb390ef](https://github.com/bogware/bog-agents/commit/cb390ef7a89966760f08c5aceb2211220e8653b8))
* Highlight file mentions and support CJK parsing ([#558](https://github.com/bogware/bog-agents/issues/558)) ([cebe333](https://github.com/bogware/bog-agents/commit/cebe333246f8bea6b04d6283985e102c2ed5d744))
* Make thread id in splash clickable ([#1159](https://github.com/bogware/bog-agents/issues/1159)) ([6087fb2](https://github.com/bogware/bog-agents/commit/6087fb276f39ed9a388d722ff1be88d94debf49f))
* Use LocalShellBackend, gives shell to subagents ([#1107](https://github.com/bogware/bog-agents/issues/1107)) ([b57ea39](https://github.com/bogware/bog-agents/commit/b57ea3906680818b94ecca88b92082d4dea63694))

### Bug Fixes

* Disable iTerm2 cursor guide during execution ([#1123](https://github.com/bogware/bog-agents/issues/1123)) ([4eb7d42](https://github.com/bogware/bog-agents/commit/4eb7d426eaefa41f74cc6056ae076f475a0a400d))
* Dismiss modal screens on escape key ([#1128](https://github.com/bogware/bog-agents/issues/1128)) ([27047a0](https://github.com/bogware/bog-agents/commit/27047a085de99fcb9977816663e61114c2b008ac))
* Hide resume hint on app error and improve startup message ([#1135](https://github.com/bogware/bog-agents/issues/1135)) ([4e25843](https://github.com/bogware/bog-agents/commit/4e258430468b56c3e79499f6b7c5ab7b9cd6f45b))
* Propagate app errors instead of masking ([#1126](https://github.com/bogware/bog-agents/issues/1126)) ([79a1984](https://github.com/bogware/bog-agents/commit/79a1984629847ce067b6ce78ad14797889724244))
* Remove Interactive Features from --help output ([#1161](https://github.com/bogware/bog-agents/issues/1161)) ([a296789](https://github.com/bogware/bog-agents/commit/a2967898933b77dd8da6458553f49e717fa732e6))
* Rename `SystemMessage` -&gt; `AppMessage` ([#1113](https://github.com/bogware/bog-agents/issues/1113)) ([f576262](https://github.com/bogware/bog-agents/commit/f576262aeee54499e9970acf76af93553fccfefd))
* Unify spinner API to support dynamic status text ([#1124](https://github.com/bogware/bog-agents/issues/1124)) ([bb55608](https://github.com/bogware/bog-agents/commit/bb55608b7172f55df38fef88918b2fded894e3ce))
* Update help text to include `Esc` key for rejection ([#1122](https://github.com/bogware/bog-agents/issues/1122)) ([8f4bcf5](https://github.com/bogware/bog-agents/commit/8f4bcf52547dcd3e38d4d75ce395eb973a7ee2c0))

## [0.0.18](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.17...bog-agents-cli==0.0.18) (2026-02-05)

### Features

* LangSmith sandbox integration ([#1077](https://github.com/bogware/bog-agents/issues/1077)) ([7d17be0](https://github.com/bogware/bog-agents/commit/7d17be00b59e586c55517eaca281342e1a6559ff))
* Resume thread enhancements ([#1065](https://github.com/bogware/bog-agents/issues/1065)) ([e6663b0](https://github.com/bogware/bog-agents/commit/e6663b0b314582583afd32cb906a6d502cd8f16b))
* Support  .`agents/skills` dir alias ([#1059](https://github.com/bogware/bog-agents/issues/1059)) ([ec1db17](https://github.com/bogware/bog-agents/commit/ec1db172c12bc8b8f85bb03138e442353d4b1013))

### Bug Fixes

* `Ctrl+E` for tool output toggle ([#1100](https://github.com/bogware/bog-agents/issues/1100)) ([9fa9d72](https://github.com/bogware/bog-agents/commit/9fa9d727dbf6b8996a61f2f764675dbc2e23c1b6))
* Consolidate tool output expand/collapse hint placement ([#1102](https://github.com/bogware/bog-agents/issues/1102)) ([70db34b](https://github.com/bogware/bog-agents/commit/70db34b5f15a7e81ff586dd0adb2bdfd9ac5d4e9))
* Delete `/exit` ([#1052](https://github.com/bogware/bog-agents/issues/1052)) ([8331b77](https://github.com/bogware/bog-agents/commit/8331b7790fcf0474e109c3c29f810f4ced0f1745)), closes [#836](https://github.com/bogware/bog-agents/issues/836) [#651](https://github.com/bogware/bog-agents/issues/651)
* Installed default prompt not updated following upgrade ([#1082](https://github.com/bogware/bog-agents/issues/1082)) ([bffd956](https://github.com/bogware/bog-agents/commit/bffd95610730c668406c485ad941835a5307c226))
* Replace silent exception handling with proper logging ([#708](https://github.com/bogware/bog-agents/issues/708)) ([20faf7a](https://github.com/bogware/bog-agents/commit/20faf7ac244d97e688f1cc4121d480ed212fe97c))
* Show full shell command in error output ([#1097](https://github.com/bogware/bog-agents/issues/1097)) ([23bb1d8](https://github.com/bogware/bog-agents/commit/23bb1d8af85eec8739aea17c3bb3616afb22072a)), closes [#1080](https://github.com/bogware/bog-agents/issues/1080)
* Support `-h`/`--help` flags ([#1106](https://github.com/bogware/bog-agents/issues/1106)) ([26bebf5](https://github.com/bogware/bog-agents/commit/26bebf592ab56ffdc5eeff55bb7c2e542ef8f706))

## [0.0.17](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.16...bog-agents-cli==0.0.17) (2026-02-03)

### Features

* Add expandable shell command display in HITL approval ([#976](https://github.com/bogware/bog-agents/issues/976)) ([fb8a007](https://github.com/bogware/bog-agents/commit/fb8a007123d18025beb1a011f2050e1085dcf69b))
* Model identity ([#770](https://github.com/bogware/bog-agents/issues/770)) ([e54a0ee](https://github.com/bogware/bog-agents/commit/e54a0ee43c7dfc7fd14c3f43d37cc0ee5e85c5a8))
* Sandbox provider interface ([#900](https://github.com/bogware/bog-agents/issues/900)) ([d431cfd](https://github.com/bogware/bog-agents/commit/d431cfd4a56713434e84f4fa1cdf4a160b43db95))

## [0.0.16](https://github.com/bogware/bog-agents/compare/bog-agents-cli==0.0.15...bog-agents-cli==0.0.16) (2026-02-02)

### Features

* Add configurable timeout to `ShellMiddleware` ([#961](https://github.com/bogware/bog-agents/issues/961)) ([bc5e417](https://github.com/bogware/bog-agents/commit/bc5e4178a76d795922beab93b87e90ccaf99fba6))
* Add timeout formatting to enhance `shell` command display ([#987](https://github.com/bogware/bog-agents/issues/987)) ([cbbfd49](https://github.com/bogware/bog-agents/commit/cbbfd49011c9cf93741a024f6efeceeca830820e))
* Display thread ID at splash ([#988](https://github.com/bogware/bog-agents/issues/988)) ([e61b9e8](https://github.com/bogware/bog-agents/commit/e61b9e8e7af417bf5f636180631dbd47a5bb31bb))

### Bug Fixes

* Improve clipboard copy/paste on macOS ([#960](https://github.com/bogware/bog-agents/issues/960)) ([3e1c604](https://github.com/bogware/bog-agents/commit/3e1c604474bd98ce1e0ac802df6fb049dd049682))
* Make `pyperclip` hard dep ([#985](https://github.com/bogware/bog-agents/issues/985)) ([0f5d4ad](https://github.com/bogware/bog-agents/commit/0f5d4ad9e63d415c9b80cd15fa0f89fc2f91357b)), closes [#960](https://github.com/bogware/bog-agents/issues/960)
* Revert, improve clipboard copy/paste on macOS ([#964](https://github.com/bogware/bog-agents/issues/964)) ([4991992](https://github.com/bogware/bog-agents/commit/4991992a5a60fd9588e2110b46440337affc80da))
* Update timeout message for long-running commands in `ShellMiddleware` ([#986](https://github.com/bogware/bog-agents/issues/986)) ([dcbe128](https://github.com/bogware/bog-agents/commit/dcbe12805a3650e63da89df0774dd7e0181dbaa6))

---

## Prior Releases

Versions prior to 0.0.16 were released without release-please and do not have changelog entries. Refer to the [releases page](https://github.com/bogware/bog-agents/releases?q=bog-agents-cli) for details on previous versions.
