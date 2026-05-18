# Cookbook

> Fifteen recipes. Each one solves a real problem in five lines or
> fewer. Read top to bottom or jump to the one you need.

## Index

1. [Read a file and answer a question](#1-read-a-file-and-answer-a-question)
2. [Refactor across many files](#2-refactor-across-many-files)
3. [Review the current diff](#3-review-the-current-diff)
4. [Fix a failing test](#4-fix-a-failing-test)
5. [Generate tests for a module](#5-generate-tests-for-a-module)
6. [Pipe context in from another tool](#6-pipe-context-in-from-another-tool)
7. [Run a saved prompt with variables](#7-run-a-saved-prompt-with-variables)
8. [Spawn parallel agents in worktrees](#8-spawn-parallel-agents-in-worktrees)
9. [Use a long-lived sub-agent with `/peat`](#9-use-a-long-lived-sub-agent-with-peat)
10. [Capture and re-run a session with `/record`](#10-capture-and-re-run-a-session-with-record)
11. [Drive the TUI from a YAML script](#11-drive-the-tui-from-a-yaml-script)
12. [Install an MCP server from the marketplace](#12-install-an-mcp-server-from-the-marketplace)
13. [Run an agent on a cron schedule](#13-run-an-agent-on-a-cron-schedule)
14. [Generate a PR from a task](#14-generate-a-pr-from-a-task)
15. [Embed an agent in your own Python app](#15-embed-an-agent-in-your-own-python-app)

---

## 1. Read a file and answer a question

```bash
bog-agents -p "what does this module do?" < src/agent.py
```

`-p` is "print and exit, clean stdout, no chrome." Pipeable.

When the file is large, the agent paginates with `read_file` rather
than burning context on the whole thing in one shot. You don't have to
do anything — that's the default.

---

## 2. Refactor across many files

Interactive:

```text
> rename CamelCase to snake_case in every Python file under src/, then run the tests
```

The agent will plan it, ask for approval before each `edit_file` call
(or run unattended if you started with `--auto-approve`), run the
test suite when it's done, and report.

For large surfaces it'll use the `multi_edit_file` tool — one tool
call, many edits, less context overhead.

---

## 3. Review the current diff

```text
/review
```

Structured review on the working tree's diff. Returns severity-tagged
findings (block / nit / praise). If you want a jury of multiple
models grading the same diff:

```text
/jury anthropic:claude-opus-4-7, openai:gpt-5, google_genai:gemini-2.5-pro
```

Each model writes its review independently. `/jury` shows agreements
and disagreements side by side.

---

## 4. Fix a failing test

```bash
bog-agents -n "fix the failing test in tests/test_auth.py" --auto-approve
```

`--auto-approve` skips the per-action confirmation prompts. Use when
you trust the agent and the task is bounded. Use `--auto` instead for
"approve safe stuff, ask about risky stuff."

For a longer iteration loop, drop into the TUI without `--auto-approve`
and watch each tool call land. Press the *deny* button on anything that
looks wrong; the agent learns from the rejection.

---

## 5. Generate tests for a module

```text
/test generate src/auth.py --framework pytest
```

The agent reads the module, identifies the public surface, drafts a
test file, runs it, fixes anything that fails, repeats until green.

For coverage analysis: `/test coverage`. For a full audit of test
quality (assertions per test, missing edge cases): `/test audit`.

---

## 6. Pipe context in from another tool

```bash
git log --since="1 week ago" --oneline | bog-agents -p "summarize what's new"
cat error.log                        | bog-agents -p "what's the root cause?"
gh pr view 42 --json files,body      | bog-agents -p "anything risky in this PR?"
```

The CLI reads piped stdin as additional context. Combined with `-p`
this is the foundation of most shell-script automation patterns.

---

## 7. Run a saved prompt with variables

`~/.bog-agents/prompt_library.toml`:

```toml
[prompts.weekly-summary]
body = """
Summarize the activity in {{repo}} for the past {{days}} days.
Highlight breaking changes, security fixes, and noteworthy PRs.
"""
```

Invoke:

```bash
bog-agents --prompt weekly-summary --prompt-vars '{"repo": "bogware/bog-agents", "days": 7}'
```

Same shape works for pipelines (multi-step prompts) in
`~/.bog-agents/pipelines/<name>.yaml`. See `bog-agents --help` for
`--pipeline`.

---

## 8. Spawn parallel agents in worktrees

```text
/orchestrate "refactor the auth module: extract Token, extract Session, add types" --parallel
```

`/orchestrate` decomposes the task into mode-typed subtasks (code /
test / review / doc / research) and runs them in isolated git
worktrees. Each subtask gets its own filesystem, its own model, its
own tool surface. Results merge back with conflict detection.

For fanout where you want N models grading the same prompt (instead
of N tasks):

```text
/race anthropic:claude-opus-4-7, openai:gpt-5, deepseek:deepseek-v3
```

---

## 9. Use a long-lived sub-agent with `/peat`

`/peat` is a hand-crafted persona that schedules recurring jobs,
runs deep research with a five-phase plan, and builds personalized
digests.

```text
/peat schedule "0 9 * * 1-5 | summarize yesterday's PRs in our repo"
/peat research "vector databases" --focus pricing,perf
/peat digest --days 7
/peat config show
```

Jobs persist to `~/.bog-agents/peat/jobs/<id>.yaml`. Results buffer
to `~/.bog-agents/peat/inbox.json` while the CLI is closed; open
the CLI and `/peat inbox` shows what came in.

When you want jobs that run while the CLI is closed and the laptop
is asleep, use the **daemon** instead — see
[Daemon Quickstart](daemon/quickstart.md).

---

## 10. Capture and re-run a session with `/record`

```text
/record start  fix-login-bug
… use the agent normally …
/record stop
```

Output:

```
Saved replay `fix-login-bug` with 12 step(s) (4 tool call(s)) and 2 auto-detected variable(s).
  YAML recording: ~/.bog-agents/replays/replay-abc123.yaml
  Drive script:   ~/.bog-agents/replays/replay-abc123.drive.yaml
```

Re-run later:

```text
/replay run fix-login-bug --var jira_ticket=JIRA-456
```

Or replay the drive script outside the TUI:

```bash
bog-agents --drive ~/.bog-agents/replays/replay-abc123.drive.yaml
```

Edit the YAML between record and replay to refine variable names,
mark fields as secrets, or rewrite a step.

---

## 11. Drive the TUI from a YAML script

Full deep dive in [docs/cli/drive.md](cli/drive.md). The short
version:

```yaml
# smoke.yaml
session:
  model: fake:Hello, drive.
  approval_mode: auto-all
steps:
  - "/help"
  - wait_for_idle: 5
  - expect_transcript_contains: "(?i)usage"
  - type: "summarize the README"
  - submit
  - wait_for_idle: 30
  - snapshot: artifacts/after-summary
```

```bash
bog-agents --drive smoke.yaml
```

Output is JSONL on stdout — one line per step, one summary line at
the end. Exit code = number of failed assertions. Plug it into CI.

---

## 12. Install an MCP server from the marketplace

```text
/mcp marketplace
```

Browse 35+ curated servers. Install one:

```text
/mcp install jira
```

Trust the project's `.mcp.json` if it asks. The new tools (e.g.
`jira__get_issue`, `jira__create_issue`) become available to the
agent on the next turn.

Custom servers:

```text
/mcp add my-tool /usr/local/bin/my-tool --flag value
```

Edit `~/.mcp.json` or `<project>/.mcp.json` to persist. The CLI's
MCP startup is capped at 15 seconds per server — a slow `npx -y`
won't brick first-paint.

---

## 13. Run an agent on a cron schedule

The daemon handles cron / interval / file-change / webhook /
git-push triggers. Install:

```bash
pip install 'bog-agents-daemon[anthropic]'
bog-agents-daemon run --port 7878 &
```

Schedule a job:

```bash
bog-agents-daemon job add \
  --name morning-brief \
  --cron "0 9 * * 1-5" \
  --prompt "Summarize what changed in this repo since yesterday." \
  --output slack:#engineering
```

The scheduler picks it up on the next tick and fires it daily.
Results land in Slack; the run record persists to
`~/.bog-agents/daemon/runs/`.

See [Daemon Quickstart](daemon/quickstart.md) for triggers, outputs,
and how to deploy as a systemd / Windows / launchd service.

---

## 14. Generate a PR from a task

```bash
bog-agents -n "fix issue #123" --pr --pr-base main
```

The agent works the task, creates a branch, commits the changes (with
a Conventional Commits message), opens a PR against `--pr-base`, and
prints the URL. `--pr-draft` for draft PRs.

Combine with `--auto-approve` in CI when you're confident the task
is well-bounded. Combine with `/qa` upstream for acceptance-criteria-
driven PR generation.

---

## 15. Embed an agent in your own Python app

```python
from bog_agents import create_agent

agent = create_agent(
    model="anthropic:claude-opus-4-7",
    system_prompt="You are a careful, concise software engineer.",
)

result = await agent.ainvoke({
    "messages": [{"role": "user", "content": "List Python files in this repo."}]
})

print(result["messages"][-1].content)
```

That gets you: filesystem tools, shell execution, sub-agents,
plan-mode, summarization middleware, prompt caching for Anthropic
models. No additional setup.

Full SDK story in [docs/sdk/quickstart.md](sdk/quickstart.md). For
the middleware pattern, [docs/sdk/middleware.md](sdk/middleware.md).
For tool-only contributions without middleware overhead,
[docs/sdk/tool-bundles.md](sdk/tool-bundles.md).

---

## Beyond the cookbook

- [Tips & Tricks](tips-and-tricks.md) — power-user patterns the docs
  don't surface elsewhere.
- [Troubleshooting](troubleshooting.md) — every common error, what
  it means, how to fix it.
- [Slash command reference](cli/slash-commands.md) — the 120+
  surface, grouped by intent.

*The bog has fish in it. You don't catch them all in one cast.*
