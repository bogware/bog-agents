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
| [Drive runner](cli/drive.md) | Scripted non-interactive TUI runs. The whole point. |
| [Modes](cli/modes.md) | Interactive, `-p`, `-n`, `--serve`, `--acp`, `--drive`, when to use which |
| [Slash commands](cli/slash-commands.md) | The 120+ slash command surface, grouped by intent |
| [MCP](cli/mcp.md) | Marketplace, custom servers, trust, OAuth |
| [Observability](cli/observability.md) | Logs, panic dumps, structured events, `--doctor-deep` |

## SDK (`bog-agents`)

| | |
|---|---|
| [Quickstart](sdk/quickstart.md) | `create_agent()` in five minutes |
| [Middleware](sdk/middleware.md) | Writing your own. Wraps, hooks, state. When not to. |
| [Tool bundles](sdk/tool-bundles.md) | The W4 alternative to "middleware that only ships tools" |
| [Backends](sdk/backends.md) | Filesystem, shell, sandbox; safe defaults explained |

## Providers

| | |
|---|---|
| [AWS Bedrock](providers/bedrock.md) | Inference profiles, model access, six-step probe, `/bedrock fix` |

## Daemon (`bog-agents-daemon`)

| | |
|---|---|
| [Quickstart](daemon/quickstart.md) | Install, run, first scheduled job |
| [Triggers + outputs](daemon/triggers-and-outputs.md) | cron / file / webhook / git-push × log / Slack / email / webhook / GitHub |
| [Deploy](daemon/deploy.md) | systemd / launchd / Windows Task Scheduler |

## Advanced

| | |
|---|---|
| [Expert Rules](advanced/expert-rules.md) | Forward+backward chaining rule engine, `/expert`, `/why`, `/prove` |
| [Compliance](advanced/compliance.md) | `/compliance` auditor, HMAC-sealed reports, FINRA / SOC 2 |
| [TraceFile v1](advanced/tracefile.md) | Ed25519-signed open trace format |
| [Dreamscape](advanced/dreamscape.md) | Long-term memory + nightly proposer |
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
