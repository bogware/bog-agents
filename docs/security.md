# Security Model

> What the agent can and cannot do. Where the boundaries are. Where
> the boundaries leak. The honest version, not the marketing one.

## Threat model

The agent talks to a model provider. The model produces tool calls.
Tool calls execute on your machine. Adversarial input can reach the
model through many paths — a malicious README, a poisoned web page,
an MCP server description, a prompt-injected git commit message.

The defaults below assume the model is *not* trusted. They assume any
specific tool call could have been authored by an attacker via prompt
injection. The boundaries are layered so a single bad call doesn't
compromise the system.

## What's safe by default

### Filesystem

- **Confined to the working directory.** `FilesystemBackend` defaults
  to `virtual_mode=True` since 0.8.0. Absolute paths, `..` traversal,
  and symlink escapes all refuse. Set
  `BOG_AGENTS_FS_UNSANDBOXED=1` to opt out — only when you understand
  what you're giving up.
- **Symlinks are not followed when reading user-controlled paths.**
  `_resolve_path` uses `O_NOFOLLOW` on POSIX and rejects symlinks on
  Windows. A symlink to `/etc/passwd` in your working directory will
  not exfiltrate.
- **Path-traversal segments are blocked.** Not by substring match
  (that's defeated by encoded variants) — by `Path.parts` walk + a
  `relative_to(self.cwd)` check that raises on escape.

### Shell

- **`LocalShellBackend.execute` is gated behind HITL by default.**
  Every shell call surfaces an approval prompt. `--auto-approve`,
  `--shell-allow-list`, or `SafeToolsMiddleware` opt out per your
  taste.
- **`inherit_env=False` by default.** The subprocess does not inherit
  your shell environment. The agent sees only what the backend
  explicitly passes. Your `AWS_SECRET_ACCESS_KEY` does not leak into
  every `git status` the agent runs.
- **`stdin=DEVNULL`.** Interactive commands (Windows `date`, `git
  rebase -i`) get EOF immediately and exit. They cannot hang the
  agent forever waiting for input.
- **Timeouts.** Default 60s per command, configurable. Hung commands
  get killed.
- **`_DANGEROUS_PATTERNS` is an accident-catcher, not a security
  boundary.** It catches typos (`rm -rf /`) but a sufficiently
  motivated model can bypass it (`find / -delete`, `python -c
  "shutil.rmtree('/')"`). The real boundary is HITL + the shell
  allow-list. Don't rely on the regex.

### Network

- **Web fetches refuse `file://`, `ftp://`, `gopher://`, `data:` — only
  http(s).** `_is_url_safe` in `browser_agent.py`.
- **Cloud metadata IMDS blocked unconditionally.** Link-local
  (`169.254.0.0/16`) addresses are rejected before the request fires.
  An attacker cannot prompt-inject the agent into fetching AWS / GCP
  / Azure instance credentials.
- **Loopback / RFC1918 / IPv6 site-local rejected by default.**
  `BrowserAgentMiddleware(allow_private_ips=True)` opts in when you
  legitimately need to talk to a local server. Cloud metadata IPs
  stay blocked regardless.
- **MCP servers have a startup timeout** (15s, configurable). A
  hanging server cannot freeze the agent indefinitely.

### Secrets

- **Vault is memory-only.** API keys, OAuth tokens, `/qa` secrets
  live in `SessionVault` for the duration of the process. Nothing
  serializes them to disk.
- **OAuth tokens written atomically with `0o600`.** `atomic_write_text`
  in `io_utils.py` sets the mode on the tempfile *before* the
  rename — no window where the file is world-readable on POSIX.
- **Webhook payloads redact secret-shaped args.** Tool args with
  keys matching `api_key`, `apikey`, `password`, `secret`, `token`,
  `authorization`, etc. are stripped before the payload posts.
  Customizable via `payload_filter`.
- **Crash dumps redact.** `_panic.py` strips secrets from
  `~/.bog-agents/crash/<ts>.log` before writing. Patterns: `sk-…`,
  `xoxb-…`, `ghp_…`, JWTs, AKIA, generic `api[_-]?key=`. Look at the
  file before attaching it to a bug report if you want to double-check.
- **Provider keys live in env vars, not the vault.** The vault
  bridges to env for runtime; nothing in the vault writes back to
  `.env`.

### HITL (Human-in-the-Loop)

- **Risky tools pause for approval by default.** File writes, file
  edits, shell execution, URL fetches, and `task` (sub-agent spawn)
  all surface an approval dialog.
- **`--auto-approve` opts out wholesale.** Use in CI / scripted
  contexts where you trust the task is bounded.
- **`--auto` is a smarter middle.** Auto-approves anything the rule
  engine clears; surfaces an approval dialog for risky operations
  only.
- **`--always-ask` is the strictest setting.** Every tool call,
  every time, no exceptions. Use for high-stakes sessions where you
  want to inspect every action.

### Audit + compliance

- **`AuditTrailMiddleware` records every agent decision.**
  Append-only, FIFO eviction only when explicitly configured.
- **Strict-hook mode for compliance.** Wire `on_entry_recorded` to a
  durable store and set `strict_hooks=True` — if the durable write
  fails, the entry is *not* appended to the in-memory log. No
  divergence between what's persisted and what the agent's seen.
- **TraceFile v1 is Ed25519-signed.** Cross-vendor open trace format
  with Merkle chaining. A trace cannot be modified retroactively
  without invalidating the chain.
- **`/compliance` auditor emits HMAC-SHA-256 sealed reports.** Daemon
  cron trigger included for periodic snapshots.

## What's *not* safe by default (and how to make it safe)

### The shell tool

The shell tool is genuinely a footgun. An agent that can run shell
commands can do anything your user account can do. The mitigations:

- **Approval-on-every-call (the default).** A human sees every
  command before it fires.
- **Shell allow-list.** `--shell-allow-list recommended` whitelists
  a curated set (git, npm, pytest, common build tools); anything
  outside surfaces approval. `--shell-allow-list all` disables the
  gate entirely (don't).
- **Sandbox the shell.** `--sandbox docker` runs commands inside a
  container. `--sandbox daytona` / `--sandbox modal` use remote
  isolated workspaces. The agent's shell access stays contained to
  the sandbox.

### Arbitrary URL fetches

- **`web_fetch` / `api_request` are SSRF-gated** (see "Network"
  above) but they can still hit any public HTTP(S) endpoint the
  network reaches. If your network has access to internal services
  that *trust the network* for auth, those services are reachable.
- **`allowed_domains` is the lockdown.** Pass
  `BrowserAgentMiddleware(allowed_domains=["api.example.com"])` and
  every other host is refused. Defense-in-depth on top of the SSRF
  gate.

### MCP servers

- **MCP tool descriptions are inlined unsanitized into the system
  prompt.** A malicious MCP server can prompt-inject the agent
  through its tool descriptions. Mitigations:
  - **Project-level MCP configs are trust-gated.** First time the
    CLI sees `<repo>/.mcp.json` with stdio servers, it asks for
    approval. The fingerprint is persisted; the prompt doesn't
    re-fire unless the config changes.
  - **The startup timeout caps damage.** A server that takes 15s
    to spawn or never speaks gets isolated and disabled.
  - **OAuth tokens for MCP servers are per-server.** Compromising
    one server's auth doesn't compromise another's.

### Plugins / skills

- **Plugins install via `git clone` of arbitrary URLs.** No
  signature verification, no hash pinning. Treat
  `/plugin install <url>` like `pip install <url>` — you're trusting
  the publisher.
- **Skills are markdown files with frontmatter.** No code execution
  on load, but a malicious skill can prompt-inject the agent. Read
  the skill before enabling it.
- **Symlink rejection on skill load.** A skill directory containing
  a symlink to outside its root will not be loaded.

### The expert rules engine

- **Rule files are YAML.** Loaded via `yaml.safe_load` — no code
  execution. But a hostile rule file could `deny` legitimate
  actions, `notify` to an attacker's endpoint, or `route` calls to
  a sub-agent that does something bad.
- **Treat `.bog-agents/expert_rules/*.yaml` like code.** Review
  before committing. Don't blindly accept rules a `/expert wizard`
  conversation produced — read what it wrote.

## Sandbox tiers (from most to least isolated)

| Sandbox | Where commands run | Trust level needed |
|---|---|---|
| `--sandbox daytona` | Remote Daytona workspace | Lowest — full isolation, separate filesystem, separate network |
| `--sandbox modal` | Remote Modal sandbox | Lowest — same |
| `--sandbox docker` | Local Docker container | Low — host kernel shared, container escape is the limit |
| `LocalShellBackend` + allow-list | Host shell, restricted commands | Medium — approves matter |
| `LocalShellBackend` (default) | Host shell, HITL on every call | Medium — you eyeball every command |
| `LocalShellBackend` + `--auto-approve` | Host shell, no gate | **High — only when the task is bounded and you trust the source** |

## Supply chain

- **CVE-keyed dependency floors.** Direct dep floors carry the CVE
  in the inline comment so a future bump knows why the floor is
  where it is. See `libs/bog-agents/pyproject.toml`,
  `libs/cli/pyproject.toml`, `libs/daemon/pyproject.toml`.
- **`pip-audit` clean.** As of Wave Z, zero CVE-vulnerable
  resolutions across the three lockfiles. Run `pip-audit` against
  the project at any time to verify.
- **PyPI publishing via OIDC trusted publishers.** No long-lived
  PyPI tokens stored as GitHub secrets. The publish workflow uses
  short-lived OIDC credentials.
- **`pull_request` workflows, not `pull_request_target`.** Fork PRs
  cannot access repository secrets in CI.
- **GitHub Actions pinned by tag.** Today we pin to major-version
  tags (`@v4`). Pin to commit SHAs in your fork if you're paranoid
  about action-repo compromise.

## Reporting a vulnerability

Email **security@bogware.com** with details. Do not file a public
GitHub issue. We'll acknowledge within 48 hours.

If you want PGP, the key is at
<https://github.com/bogware/bog-agents/blob/main/SECURITY.md>.

---

*The bog has things in it that bite. Most are slow. Watch where you
put your hands.*
