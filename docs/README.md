# Bog Agents — Documentation

> *Pass through in harmony. Opinionated where it matters.*

The READMEs at the top of each package answer "what is this and how do
I install it." Everything in this tree answers "how do I actually use
it well." Short docs that respect your time. No marketing. Worked
examples wherever possible.

## Start here

| Reading order | Page |
|---|---|
| 1. | [Getting Started](getting-started.md) — install, first agent run, the five commands you'll use forever |
| 2. | [Cookbook](cookbook.md) — fifteen task-shaped recipes |
| 3. | [Troubleshooting](troubleshooting.md) — every common error, what it means, how to fix it |
| 4. | [Tips & Tricks](tips-and-tricks.md) — power-user moves the docs don't surface elsewhere |

## CLI (`bog-agents`)

| | |
|---|---|
| [Command reference](cli/commands.md) | Every slash command (140+), grouped by intent |
| [Governed autonomy](cli/governed-autonomy.md) | Teams, best-of-N, jury, operator, workflows, cost caps, evidence, plan review |
| [Findings & security scans](cli/findings.md) | The findings ledger, the security-scan recipe, the CI gate, `/remediate` |
| [Governance & safety](cli/governance.md) | Trust profiles, managed policy, the OS sandbox, the hook bus, the action log, code mode |
| [Drive runner](cli/drive.md) | Scripted non-interactive TUI runs. The whole point. |
| [Headless driving](../libs/cli/README.md#driving-it-headless) | `-n` / `-p`, `--json` / `--jsonl`, `bog-agents command`, `mcp-server`, when to use which |

## SDK (`bog-agents`)

| | |
|---|---|
| [Quickstart](sdk/quickstart.md) | `create_agent()` in five minutes |
| [Middleware](sdk/middleware.md) | Writing your own. Wraps, hooks, state. When not to. |
| [Tool bundles](sdk/tool-bundles.md) | The alternative to "middleware that only ships tools" |
| [deepagents compatibility](../libs/bog-agents/README.md#deepagents-compatibility) | `create_deep_agent`, permissions, harness/provider profiles (in the SDK README) |

## Providers

| | |
|---|---|
| [AWS Bedrock](providers/bedrock.md) | Inference profiles, model access, six-step probe, `/bedrock fix` |

## Daemon (`bog-agents-daemon`)

| | |
|---|---|
| [Quickstart](daemon/quickstart.md) | Install, run, first scheduled job |
| [Triggers + outputs](../libs/daemon/README.md#triggers) | cron / file / webhook / git-push × log / Slack / email / webhook / GitHub (in the daemon README) |
| [Deploy](../libs/daemon/README.md#running-as-a-service) | systemd / launchd / Windows Task Scheduler (in the daemon README) |

## Advanced

| | |
|---|---|
| [Expert Rules](advanced/expert-rules.md) | Forward+backward chaining rule engine, `/expert`, `/why`, `/prove` |
| [Security model](security.md) | Threat boundaries, sandbox options, what the agent can and cannot do |

## Filing a bug

`~/.bog-agents/crash/<timestamp>.log` after a crash. Attach it.
Redaction's already done — secrets are stripped before the file lands.

```bash
bog-agents --doctor-deep > doctor.txt
```

That one-page summary is the second-most-useful thing to attach.

---

*The bog is calm, deep, and unhurried. So is the agent.*
