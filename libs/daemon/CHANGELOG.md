# Changelog

## [0.8.5](https://github.com/bogware/bog-agents/compare/bog-agents-daemon==0.8.4...bog-agents-daemon==0.8.5) (2026-05-10)


* **bog-agents-daemon:** Synchronize bog-agents-monorepo versions

## [0.8.4](https://github.com/bogware/bog-agents/compare/bog-agents-daemon==0.8.3...bog-agents-daemon==0.8.4) (2026-05-08)


* **bog-agents-daemon:** Synchronize bog-agents-monorepo versions

## [0.8.3](https://github.com/bogware/bog-agents/compare/bog-agents-daemon==0.8.2...bog-agents-daemon==0.8.3) (2026-05-05)


* **bog-agents-daemon:** Synchronize bog-agents-monorepo versions

## [0.8.2](https://github.com/bogware/bog-agents/compare/bog-agents-daemon==0.8.1...bog-agents-daemon==0.8.2) (2026-05-04)


* **bog-agents-daemon:** Synchronize bog-agents-monorepo versions

## [0.8.1](https://github.com/bogware/bog-agents/compare/bog-agents-daemon==0.8.0...bog-agents-daemon==0.8.1) (2026-05-04)


### Features

* 0.7.0 - daemon/mcp/plugins/hardening ([#40](https://github.com/bogware/bog-agents/issues/40)) ([2427dfb](https://github.com/bogware/bog-agents/commit/2427dfbda3bffc17ea34f6e38de8d2634a57f86f))
* 0.8.0 — patient as still water ([#63](https://github.com/bogware/bog-agents/issues/63)) ([8b93798](https://github.com/bogware/bog-agents/commit/8b9379850e8c0360bb10dced6fe1dc83ebe9e11c))
* verify/call subcommands, shell reliability, cross-platform hardening, 12 CVEs closed ([#51](https://github.com/bogware/bog-agents/issues/51)) ([5f13fb4](https://github.com/bogware/bog-agents/commit/5f13fb4de5aa7cb50731b634796f0732a8a25f65))

## [0.7.6](https://github.com/bogware/bog-agents/compare/bog-agents-daemon==0.7.5...bog-agents-daemon==0.7.6) (2026-05-02)


* **bog-agents-daemon:** Synchronize bog-agents-monorepo versions

## [0.7.5](https://github.com/bogware/bog-agents/compare/bog-agents-daemon==0.7.4...bog-agents-daemon==0.7.5) (2026-05-01)


* **bog-agents-daemon:** Synchronize bog-agents-monorepo versions

## [0.7.4](https://github.com/bogware/bog-agents/compare/bog-agents-daemon==0.7.3...bog-agents-daemon==0.7.4) (2026-04-30)


### Bug Fixes

* version-sync release alongside bog-agents-cli and bog-agents 0.7.4. No daemon code changes; published to keep linked versions in lockstep with the CLI's Bedrock auth_mode + auto-fallback fixes for [#54](https://github.com/bogware/bog-agents/issues/54) and the probe-cache fix for [#53](https://github.com/bogware/bog-agents/issues/53). ([fef8228](https://github.com/bogware/bog-agents/commit/fef82283e9fc07f5d286a26eea093e68d28cdb42))

## [0.7.3](https://github.com/bogware/bog-agents/compare/bog-agents-daemon==0.7.2...bog-agents-daemon==0.7.3) (2026-04-29)


### Features

* verify/call subcommands, shell reliability, cross-platform hardening, 12 CVEs closed ([#51](https://github.com/bogware/bog-agents/issues/51)) ([5f13fb4](https://github.com/bogware/bog-agents/commit/5f13fb4de5aa7cb50731b634796f0732a8a25f65))

## [0.7.1](https://github.com/bogware/bog-agents/compare/bog-agents-daemon==0.7.0...bog-agents-daemon==0.7.1) (2026-04-20)


### Features

* 0.7.0 - daemon/mcp/plugins/hardening ([#40](https://github.com/bogware/bog-agents/issues/40)) ([2427dfb](https://github.com/bogware/bog-agents/commit/2427dfbda3bffc17ea34f6e38de8d2634a57f86f))

## [0.7.0] - 2026-04-18

### Features

- Initial production release of the ambient agent daemon
- REST API (FastAPI) on `localhost:7391` with token-based auth
- Job store: persistent JSON storage with atomic writes and thread-safe locking
- Trigger types: cron, interval, file-change (with debounce), webhook (HMAC-SHA256 verified), git-push, manual
- Output targets: log, file, email (SMTP), Slack, GitHub comments, webhook, stdout
- Scheduler: asyncio-based with configurable concurrency semaphore (default 5 concurrent jobs)
- Service installer: systemd unit generator (`install_systemd`) and macOS launchd plist generator (`install_launchd`)
- Git hook installer: `install_git_hook` writes a post-receive hook to any repo
- Graceful shutdown: drains in-flight jobs (30 s timeout) on SIGTERM/SIGINT
- Agent timeout: configurable via `BOG_DAEMON_AGENT_TIMEOUT` env var (default 30 min)
- Run file pruning: keeps last N run files per job (`BOG_DAEMON_MAX_RUNS_PER_JOB`, default 100)
- `/ready` readiness probe endpoint (no auth — Kubernetes-compatible)

### Security

- Timing-safe auth token comparison via `hmac.compare_digest`
- Path traversal guard on file output targets
- Shell injection prevention via `shlex.quote()` in git hook generation
- HMAC-SHA256 webhook secret validation
