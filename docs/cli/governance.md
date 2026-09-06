# Governance and safety

Governance that logs and hopes isn't governance. Bog Agents enforces at the layer
where enforcement can't be talked out of: tools that don't exist for a locked-down
agent, a policy verified by signature, a shell wrapped in a kernel sandbox, an
audit trail whose tampering shows.

This guide covers the trust posture (`--restricted`), org-wide managed policy, the
OS-level sandbox, the hook bus, the hash-chained action log, and governed code
mode.

## Trust profiles and `--restricted`

A trust profile is the policy half of what the agent may do. The built-in
`--restricted` preset strips every tool that spawns a process or opens a raw
socket — the shell, git tools, raw HTTP — and confines the filesystem to the
working directory through virtual paths.

```bash
bog-agents --restricted            # shell/git/raw-HTTP tools are not registered
```

The point is that this is *structural*, not advisory: the restricted agent is
rebuilt without those tools, so there is nothing for a prompt-injection to invoke.
`fetch_url` is allowed only when you provide an explicit domain allow-list. A drift
test (`test_no_surviving_restricted_tool_spawns_processes`) rebuilds the restricted
agent and fails CI if any process-spawning tool survives, so a newly added tool
can't quietly slip past the profile.

`/permissions` shows the current approval mode, workspace trust, and any managed
policy in effect. `/permissions trust-workspace` fingerprints the
repo-controlled instruction files (rules, hooks, skills) so a cloned repo can't
silently change the agent's behavior until you acknowledge it.

## Managed policy (org-wide)

For a fleet, one signed document sets the rules everyone runs under. Point
`managed.policy_source` at a URL or path and `managed.policy_public_key` at the
pinned Ed25519 key:

```toml
[managed]
policy_source = "https://policy.acme.example/bog.json"
policy_public_key = "…base64 public key…"
```

The document is one signed JSON blob (`{"policy": {…}, "signature", "signer"}`),
verified with the pinned key, cached as the last good copy, and loaded once per
process. It can:

- allow-list MCP servers and skills,
- require / forbid plugins (a forbidden plugin install is refused and removed),
- pin the model gateway (`provider_lock` sets `base_url` whatever `config.toml`
  says),
- allow or deny model switches (`/model` is gated, and a `model_switch` fact is
  asserted into the Expert engine either way),
- enforce zero-retention (memory off for the build).

A URL source without a pinned key is refused; a tampered or foreign signature
rejects the policy; and a policy that fails to load never breaks a session — it's
logged and surfaced as an org-pinned row in `/permissions` and a `/doctor` check.

## The OS-level sandbox

`--restricted` removes tools; the sandbox contains the shell you *do* allow.
Driven from `.bog-agents/sandbox.toml`:

```toml
[sandbox]
local_sandbox = "bwrap"          # bubblewrap (Linux) / seatbelt (macOS)
require_sandbox = true           # fail closed if no launcher exists
network_allowlist = ["pypi.org", "github.com"]
```

Every shell command runs inside the sandbox. Network is either cut hard
(`--unshare-net`) or forced through a **localhost CONNECT allow-list proxy** that
only lets through the hosts you named (suffix-matched on label boundaries).
`require_sandbox = true` means that on a platform with no launcher (Windows today,
for the bubblewrap path) the shell fails closed rather than running unsandboxed.

Separately, `bog-agents --sandbox docker` (or `daytona`) runs the whole agent
inside a container / remote workspace — a different, coarser boundary for when you
want the entire run off your host.

## The hook bus

Bog Agents loads Claude Code (`.claude/settings.json`) and Cursor
(`.cursor/hooks.json`) hook files unchanged, aliasing their tool names (`Bash`,
`Edit`, `Read`) onto bog's (`execute`, `edit_file`, `read_file`). A `PreToolUse`
or `Stop` hook can emit `{"decision": "deny", "reason": …}` to block a call;
`PostToolUse` can rewrite a result; `PreModelSwitch` can deny a model change.

Two directions are load-bearing and preserved:

- **Command hooks are fail-open by default** — a crashing or timing-out hook never
  blocks, unless its `on_failure` is `deny` (or `ask`, which forces the approval
  prompt).
- **Enforcement in the tool path is fail-closed** — a denial means the tool body
  never runs and an error result is returned.

Hook scripts are hash-pinned (`pin_hook_hashes`; plugin hooks pinned at
`/plugin trust`), so a changed script must be re-approved. `type: prompt` hooks
are judged by a model, fail-closed like the rule engine.

## The action log — tamper-evident audit

Turn on the compliance artefact and every approval and tool call is appended to a
hash-chained log you can verify and sign:

```text
/actionlog status
/actionlog verify           # confirm the chain is intact
/actionlog export report.jsonl     # signed with the TraceFile key
/actionlog prune --days 90
```

Because each entry hashes the previous one, an altered or removed line breaks the
chain and `verify` reports it. Set `ACTION_LOG=1` (or `compliance.action_log` in
a manifest) to enable it; `OTEL_ENDPOINT` additionally emits GenAI-semconv spans
over dependency-free OTLP/HTTP. Never let two processes append to one chain file.

## Governed code mode

For the model that would rather write a short script than make ten tool calls,
`tools.code_mode` registers a `run_code` tool that executes model-written Python
in a child interpreter, with your tools exposed as plain functions:

```toml
[tools]
code_mode = true
```

The child cannot escape governance: HITL-gated tools are refused inside a script
(an interrupt can't cross the process boundary), `task` / `web_search` spawns are
counted against the cost caps before they run, and the script is bounded by a call
limit and a wall-clock timeout. It's off under `--restricted`.

## Data-loss prevention

`DLPMiddleware` scans tool output and teammate file transfers for secrets and PII
and redacts them, recording the detection count. It runs before the audit log, so
the log never captures an unredacted value. The team file-exchange tools
(`send_file` / `send_patch`) DLP-scan text before it leaves your tree.

## See also

- [Security model](../security.md) — threat boundaries and what the agent can't do
- [Governed autonomy](governed-autonomy.md) · [Findings & security scans](findings.md)
- [Command reference](commands.md)
