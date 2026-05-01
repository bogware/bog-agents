# bog-agents-daemon

Quiet ambient runner for [`bog-agents`](https://github.com/bogware/bog-agents). Schedules. File watches. Inbound webhooks. Git pushes. Sits in the background, fires the agent when something happens, writes the result wherever you point it.

No terminal needed. No hand-holding. Goes the distance.

[![PyPI](https://img.shields.io/pypi/v/bog-agents-daemon)](https://pypi.org/project/bog-agents-daemon/)
[![License](https://img.shields.io/pypi/l/bog-agents-daemon)](https://opensource.org/licenses/MIT)

Five trigger types. Seven output targets. A small authenticated REST API. POSIX
and Windows. HMAC-validated inbound webhooks. `os.fsync()`-durable job
persistence so a hard kill never loses a freshly-created job.

**Triggers:** `cron` · `interval` · `file_change` · `webhook` · `git_push`
**Outputs:** `log` · `stdout` · `file` · `slack` · `webhook` · `email` · `github_comment`

---

## Install

```bash
pip install bog-agents-daemon

# Or with uv
uv tool install bog-agents-daemon
```

Requires **Python 3.11+** and a running Bog Agents installation (`bog-agents>=0.7.0`).

---

## Quick Start

```bash
# 1. Start the daemon (runs on localhost:7391 by default)
bog-agents-daemon

# 2. Or manage it via the bog-agents CLI
bog-agents daemon start
bog-agents daemon status

# 3. Create a job (cron trigger, every day at 9 AM)
curl -s -X POST http://localhost:7391/jobs \
  -H "X-Daemon-Token: $(cat ~/.bog-agents/daemon/token)" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "daily-standup",
    "prompt": "Summarize recent git commits and open PRs",
    "triggers": [{"type": "cron", "cron": "0 9 * * 1-5"}],
    "outputs": [{"target": "log"}]
  }'
```

---

## Install as a System Service

The daemon can auto-register itself as a background service that starts on login:

```bash
# Linux (systemd)
bog-agents daemon install
systemctl --user enable --now bog-agents-daemon

# macOS (launchd)
bog-agents daemon install
# daemon starts automatically at login
```

---

## Trigger Types

| Trigger | Config key | Description |
|---------|-----------|-------------|
| **cron** | `cron: "0 9 * * 1-5"` | Standard 5-field cron expression |
| **interval** | `interval_seconds: 3600` | Every N seconds |
| **file_change** | `watch_dir`, `watch_patterns` | Any matched file modified |
| **webhook** | `webhook_path: "/hooks/deploy"` | POST to `/webhooks/<path>` |
| **git_push** | `git_branch_pattern: "main"` | Git post-receive hook fires |
| **manual** | — | `POST /jobs/{id}/run` |

---

## Output Targets

| Target | Description |
|--------|-------------|
| `log` | Daemon log (default) |
| `file` | Append/overwrite a local file |
| `email` | Send via SMTP |
| `slack` | Post to a Slack incoming webhook |
| `github_comment` | Comment on a GitHub issue or PR |
| `webhook` | POST JSON to any URL |
| `stdout` | Print to daemon stdout |

---

## REST API

The daemon exposes a REST API on `http://127.0.0.1:7391` (localhost only).
Most endpoints require the `X-Daemon-Token` header. Two exceptions:

- `/ready` — readiness probe, no auth.
- `/webhooks/{path}` — inbound from external services. Auth is the per-trigger
  `webhook_secret` HMAC over `X-Hub-Signature-256` (timing-safe). The daemon
  token also works on `/webhooks/{path}` if you happen to have it; useful for
  local CLI tests.

```bash
TOKEN=$(cat ~/.bog-agents/daemon/token)

# Health
curl -H "X-Daemon-Token: $TOKEN" http://localhost:7391/health

# Readiness probe (no auth)
curl http://localhost:7391/ready

# OpenAPI 3.0 schema (for clients like Swagger UI)
curl http://localhost:7391/openapi.json

# List jobs
curl -H "X-Daemon-Token: $TOKEN" http://localhost:7391/jobs

# Create job
curl -X POST -H "X-Daemon-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"my-job","prompt":"...","triggers":[...],"outputs":[...]}' \
  http://localhost:7391/jobs

# Trigger manually
curl -X POST -H "X-Daemon-Token: $TOKEN" http://localhost:7391/jobs/{id}/run

# View run history
curl -H "X-Daemon-Token: $TOKEN" http://localhost:7391/jobs/{id}/runs

# Enable/disable
curl -X POST -H "X-Daemon-Token: $TOKEN" http://localhost:7391/jobs/{id}/enable
curl -X POST -H "X-Daemon-Token: $TOKEN" http://localhost:7391/jobs/{id}/disable

# Delete
curl -X DELETE -H "X-Daemon-Token: $TOKEN" http://localhost:7391/jobs/{id}
```

---

## Git Push Triggers

Install a git post-receive hook to fire jobs on push:

```bash
# Via bog-agents CLI (recommended)
bog-agents daemon install-git-hook --repo /path/to/repo

# Or via daemon CLI
bog-agents-daemon install-git-hook --repo /path/to/repo
```

The hook POSTs `{"ref": "refs/heads/main", "new_sha": "...", "old_sha": "..."}` to `/webhooks/git-push`. Jobs with `type: git_push` and a matching `git_branch_pattern` will fire.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BOG_DAEMON_AGENT_TIMEOUT` | `1800` | Max seconds per agent run |
| `BOG_DAEMON_MAX_CONCURRENT_JOBS` | `5` | Max parallel agent executions |
| `BOG_DAEMON_MAX_RUNS_PER_JOB` | `100` | Run files kept per job (older pruned) |

---

## Security

- API binds to `127.0.0.1` (localhost only) — not reachable from the network.
- Auth token stored at `~/.bog-agents/daemon/token` (`0600` on POSIX; on
  Windows the daemon also runs `icacls /inheritance:r /grant <user>:F` to
  drop inherited ACEs).
- Webhook secrets validated with HMAC-SHA256 via `hmac.compare_digest` —
  timing-safe, no token leaks.
- Daemon-token comparison also uses `hmac.compare_digest`.
- File output restricted to `$HOME`, the system temp dir, the current
  working directory, and the job's `working_dir` (path traversal guard).
  Relative file paths are anchored to `working_dir`.
- API responses redact `smtp_password`, `github_token`, and `webhook_secret`
  to `'***'` — they're persisted in `jobs.json` for runtime use but never
  echoed back through HTTP, even with a valid token.
- Git hook scripts use `shlex.quote()` to prevent shell injection.
- Job records and run records are written through `os.fsync()` before
  atomic-rename, so a hard kill never loses a freshly-created job.

---

## Development

```bash
cd libs/daemon
uv sync --group test
uv run pytest tests/ -q
uv run ruff check bog_agents_daemon/
```
