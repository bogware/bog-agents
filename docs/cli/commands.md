# Command reference

Every slash command in `bog-agents-cli`, grouped by what you reach for it to do.
Type `/` in the TUI and fuzzy autocomplete surfaces matches; `/help` searches by
keyword. Commands marked **(headless)** also run without the TUI via
`bog-agents command "/…"`.

> There are 140+ commands; the everyday ones are at the top of each group. If a
> command isn't here it's still one `/help <keyword>` away.

## Core

| Command | What |
|---|---|
| `/help` | Slash-command help; search by keyword. **(headless)** |
| `/commands` | Browse commands and quick descriptions. **(headless)** |
| `/clear` | Clear history, start a fresh thread. |
| `/compact` · `/compress` · `/sweep` | Summarize / compress / continuously prune context to cut token cost. |
| `/resume` · `/threads` · `/session` | Resume a thread; browse threads; label/tag/export the session. |
| `/checkpoint` · `/rewind` | Save/load named checkpoints; fork a thread from an earlier snapshot. |
| `/recap` | Where this session stands: turns, spend, files, running work, what needs you. |
| `/quit` | Exit. |

## Models, effort, and routing

| Command | What |
|---|---|
| `/model` · `/settings` · `/refresh-models` | Switch models; configure providers/fallbacks; rebuild the catalog. |
| `/effort` · `/think` | Native reasoning effort per model; extended thinking on the next query. |
| `/operator` | Judge each prompt and route it to an `easy/medium/hard/max` tier. |
| `/profile` · `/persona` · `/theme` | Configuration presets; output-style persona; color theme. |
| `/race` | Fan a prompt out to N models in parallel; side-by-side + a suggested winner. |

## Plan, review, and quality

| Command | What |
|---|---|
| `/plan` · `/review-plan` | Read-only plan mode; review a plan line by line before it runs. |
| `/review` · `/recommend` | Structured code review; AI-powered review/recommendation flows. |
| `/self-review` | Pre-submit gate: five reviewer lenses over your diff (`--fix` loops). |
| `/jury` | Run the diff past N juror models; aggregate SHIP/FIX verdicts. |
| `/devil` · `/squad` · `/telephone` | Adversarial critique; multi-persona debate; rewrite a prompt to production grade. |
| `/finding` | Rule on a review finding so the next review learns (`addressed`/`wontfix`/`incorrect`). |
| `/ci-fix` | Read the branch's CI, diagnose and fix failing jobs. |
| `/test` · `/audit` · `/health` | Tests + coverage; dependency vulnerability audit; codebase health score. |

## Governed autonomy

See [Governed autonomy](governed-autonomy.md) for the full walkthrough.

| Command | What |
|---|---|
| `/team` | Team shared config + `/team run` — a governed team over a task ledger. |
| `/best-of-n` | N full attempts in isolated worktrees, rubric-judged, winner kept. |
| `/workflow` · `/<name>` | Author and run agent-authored multi-phase workflows saved as slash commands. |
| `/butcher` · `/orchestrate` · `/imagine` | Slice a job for weak workers; mode-typed subtask tree; N parallel angles. |
| `/tasks` · `/dashboard` | The one task command center; the multi-agent status/cost dashboard. |
| `/jtbd` · `/goal` · `/rubric` | Jobs-to-be-done outcomes; a durable objective; its acceptance criteria. |

## Findings and security

See [Findings & security scans](findings.md).

| Command | What |
|---|---|
| `/findings` | The findings ledger: list, triage, gate, SARIF, record a scan report. **(headless)** |
| `/remediate` | Turn one finding into a fix turn with its evidence in the prompt. |
| `/recipe` | Install curated recipe pipelines, including the `security-scan` recipe. |
| `/compliance` | Run a YAML audit pack; produce a signed, SOC2-aligned report. |

## Cost, usage, and changes

| Command | What |
|---|---|
| `/cost` | Spend this session, per model / tool, with the durable daily ledger. |
| `/usage` · `/tokens` | Per-response token usage; the per-middleware/tool overhead breakdown. |
| `/changes` · `/diff` · `/undo` | Turn-end changes tray with per-hunk revert; pending diff; restore tracked changes. |

## Governance and safety

See [Governance & safety](governance.md).

| Command | What |
|---|---|
| `/permissions` | Approval mode, workspace trust, and any org-signed managed policy. |
| `/auto` · `/always-ask` | Smart auto-mode (approve safe ops); paranoid mode (approve everything). |
| `/actionlog` | The hash-chained action log: verify, export signed, prune. **(headless)** |
| `/expert` · `/why` · `/prove` · `/prove-invariant` | The rule engine: gates, explanations, backward-chaining, invariant proofs. |
| `/laws` · `/rules` | Laws/Constitution audit; project rule injection. |
| `/tracefile` · `/trace-mind` · `/postmortem` | Signed content-addressed traces; the proof tree behind a decision; causal postmortem. |

## Memory and context

| Command | What |
|---|---|
| `/memory` | Rebuild the agent-recorded memories (dedup, contradictions, provenance). **(headless)** |
| `/remember` · `/teach` | Capture memory/skills from the session; propose new skills. |
| `/init` · `/onboard` · `/repo` · `/repomap` | Generate `AGENTS.md`; onboarding; repo summary; semantic repo map. |
| `/search` · `/index` · `/explain` | Hybrid codebase search; local KB index; deep-dive on a symbol/file. |
| `/web` · `/image` · `/proxy` | Fetch a URL into context; analyze images; promote a shell command to a tool. |
| `/add-dir` · `/workspace` | Mount a directory at `/mnt/<name>/`; multi-repo context. |
| `/btw` · `/sidecar` · `/scratch` | Out-of-band note; one-shot read-only subagent; ephemeral experiment worktree. |

## Sub-agents, background work, and worktrees

| Command | What |
|---|---|
| `/agent` · `/subtask` · `/fork` | Manage agent threads; run a background task with this context; fork the conversation. |
| `/background` · `/async` · `/jobs` · `/remote` | Background tasks; fire-and-forget; job monitor; remote/cloud execution. |
| `/worktree` · `/worktrees` · `/branch` · `/scratch` | Isolated git worktrees and branches for parallel work. |
| `/detach` | Leave the agent server running and quit; `bog-agents attach <session>` returns. |
| `/handoff` · `/resolve` | Compile a handoff doc; AI-assisted merge-conflict resolution. |

## MCP, skills, and plugins

| Command | What |
|---|---|
| `/mcp` | The MCP marketplace — browse, install, and manage servers. |
| `/skills` · `/plugin` · `/extensions` | Loaded skills + trust; agent plugins; extension packages. |
| `/build` · `/pipeline` · `/recipe` | Wizard to create skills/prompts/pipelines; run saved pipelines; curated recipes. |
| `/prompt` · `/vars` | Saved prompts with variable substitution; the secrets + variables store. |

## Personal assistant and scheduling

| Command | What |
|---|---|
| `/peat` | Your personal assistant: chat, schedule, research, digest. |
| `/qa` | Author and run acceptance-criteria QA plans. |
| `/record` · `/replay` | Capture a session to editable YAML; replay it with variables filled. |
| `/ambient` · `/dream` · `/standing-orders` | The daemon; overnight ideation; curated daemon-job templates. |

## Providers, health, and housekeeping

| Command | What |
|---|---|
| `/doctor` · `/smoketest` · `/bedrock` | Local health check; connectivity test; AWS Bedrock probe + fix. |
| `/update` · `/version` · `/changelog` | Upgrade the CLI (asks first); versions; changelog. **(headless: `/version`, `/update`, `/changelog`)** |
| `/logs` · `/reload` · `/keybindings` · `/docs` · `/feedback` | Log path; reload config; keybindings; docs; file a bug. |
| `/pr` · `/release-train` · `/migrate` · `/infra` | PR management; release notes; migration plan; infra codegen. |

## Headless entry points (no TUI)

```bash
bog-agents -n "task" --auto --json        # one-shot agent run, structured
bog-agents -p "task" < input              # pipe-friendly, clean stdout
bog-agents --plan "task" --auto           # plan read-only, then execute
bog-agents command "/findings gate --max high"   # exit 1 fails CI
bog-agents command "/memory rebuild"      # model-free memory consolidation
bog-agents command "/help"                # any headless-capable slash command
bog-agents mcp-server                     # serve the agent over MCP
bog-agents --drive script.yaml            # scripted TUI run (JSONL out)
```

`--restricted` (locked-down tool set) and `--mini` (low-overhead harness) compose
with any of these. See the [CLI README](../../libs/cli/README.md) for the full
flag table.
