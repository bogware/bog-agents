# Changelog

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
