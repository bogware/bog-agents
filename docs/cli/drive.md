# `bog-agents --drive` — scripted TUI

> Emulate a real user interacting with the TUI. End-to-end. From
> CI. Without a human at the keyboard. Without mocking the TUI.
> Without `expect`.

## Why this exists

The TUI is the most-touched surface in the product. Slash commands,
modals, approval dialogs, model picker, vault prompts, autocomplete,
focus management — none of it is exercised by `bog-agents -p "..."`,
which talks to a langgraph server directly and skips the whole UI.

You can write unit tests against `BogAgentsApp.run_test()` and
Textual's `Pilot` — that's how the project's own 4,000+ CLI tests
work. But that requires Python. It also doesn't help when you want
to script complex flows for QA: "open the model picker, switch to
Claude, run `/help`, snapshot, run a real prompt, approve the file
write, snapshot again."

`bog-agents --drive <script.yaml>` is YAML for that. The runner boots a real
`BogAgentsApp` under Pilot, executes a script of actions, and writes
a JSONL transcript. CI runs scripts. Humans review snapshots. The
TUI surface stays honest.

## The 30-second tour

```yaml
# smoke.yaml
session:
  model: fake:Hello from drive.
  approval_mode: auto-all
steps:
  - "/help"
  - wait_for_idle: 5
  - expect_transcript_contains: "(?i)help|usage"
  - type: "summarize the README"
  - submit
  - wait_for_idle: 30
  - snapshot: artifacts/after-summary
```

```bash
bog-agents --drive smoke.yaml
```

Output:

```jsonl
{"step":0,"action":"slash","ok":true,"duration_ms":42,"detail":{"command":"/help"}}
{"step":1,"action":"waitforidle","ok":true,"duration_ms":0,"detail":{"timeout_seconds":5.0}}
{"step":2,"action":"expecttranscript","ok":true,"duration_ms":0,"detail":{"pattern":"(?i)help|usage","matched":"help"}}
{"step":3,"action":"type","ok":true,"duration_ms":12,"detail":{"text":"summarize the README","slow":false}}
{"step":4,"action":"submit","ok":true,"duration_ms":4,"detail":{"value":"summarize the README","mode":"normal"}}
{"step":5,"action":"waitforidle","ok":true,"duration_ms":2106,"detail":{"timeout_seconds":30.0}}
{"step":6,"action":"snapshot","ok":true,"duration_ms":18,"detail":{"svg":"artifacts/after-summary.svg","txt":"artifacts/after-summary.txt"}}
{"summary":{"total":7,"passed":7,"failed":0,"duration_ms":2210}}
```

Exit code = number of failed steps. Plug into CI.

## Anatomy of a script

```yaml
session:                          # boot-time configuration
  cwd: ./fixture-repo             # optional; defaults to script's dir
  model: fake:hello               # see "Models" below
  approval_mode: auto-all         # explicit | auto-all | auto-reads
  thread_id: drive-smoke-001      # optional; auto-generated otherwise
  no_mcp: true                    # default true (deterministic)
  vars:                           # pre-resolved ${var} values
    target_file: README.md
  env:                            # process env vars set before app boots
    BOG_AGENTS_NO_TELEMETRY: "1"

vars:                             # ${var} substitution declared block
  who: { type: string, default: "World" }

steps:                            # the action sequence
  - "/help"                       # bare string → slash command
  - "!ls"                         # bare string starting with ! → shell
  - submit                        # bare 'submit' → submit current input
  - type: "summarize ${target_file}"
  - submit
  - wait_for_idle: 30
  - expect_transcript_contains: "summary"
  - snapshot: artifacts/shot-1
```

Top-level keys: `session`, `vars`, `steps`. Everything else is
ignored (forward-compatible).

## Action grammar

Fourteen actions. The grammar is intentionally small.

### Input

| Action | What it does |
|---|---|
| `type: "text"` | Set the chat input value to `text` |
| `type: { text: "text", slow: true }` | Same, but `slow: true` chords each character through Pilot for realism |
| `submit` | Submit the current input (bare keyword) |
| `submit: "text"` | Replace input with `text`, then submit |
| `submit: { value: "text", mode: command }` | Force a specific mode (normal / command / shell) |
| `"/foo bar"` | Shorthand for slash command — submits immediately |
| `"!ls -la"` | Shorthand for shell — submits immediately |
| `press: "ctrl+c"` | Send a single key chord through Pilot |
| `press: ["tab", "tab", "enter"]` | Send a sequence |

### Waiting

| Action | What it does |
|---|---|
| `wait_for_idle: 30` | Block until `_agent_running=False` AND no pending approval / ask-user widget. Times out after N seconds. |

### Assertions

| Action | What it does |
|---|---|
| `expect_transcript_contains: "regex"` | Polls the `MessageStore` for a regex match. Times out after 10s by default. |
| `expect_transcript_contains: { pattern: "...", timeout_seconds: 30, message_type: assistant }` | Restrict to a specific message type (user/assistant/tool/etc.) |
| `expect_modal: ModelSelector` | Assert a modal screen of that class name is mounted. Substring + case-insensitive. |
| `assert_widget: { selector: "#status-bar", text_matches: "Ready" }` | Query a widget by Textual CSS selector and optionally check rendered text |

### Modals

| Action | What it does |
|---|---|
| `select_option: "Anthropic"` | Pick a focusable item inside the topmost modal by label (substring, case-insensitive) |
| `select_option: 2` | Pick by index |

### Approvals

| Action | What it does |
|---|---|
| `on_approval: approve` | Wait for an approval dialog, then approve once |
| `on_approval: auto` | Approve and enable auto-approve for that tool for the session |
| `on_approval: deny` | Refuse the tool call |
| `on_approval: { choice: approve, wait: false }` | Respond if a dialog is up; don't block waiting for one |

### Artifacts

| Action | What it does |
|---|---|
| `snapshot: "shot-1"` | Write `<artifact_dir>/shot-1.svg` (Textual `save_screenshot`) and `<artifact_dir>/shot-1.txt` (plain text grid). |

### State

| Action | What it does |
|---|---|
| `set_env: { KEY: value, ... }` | Set process env vars for subsequent steps |
| `switch_model: "anthropic:claude-opus-4-7"` | Mid-session model switch via the same path `/model` uses |

## Models

The `session.model` value picks one of three deterministic modes plus
the regular real-provider path:

| Spec | What it does |
|---|---|
| `fake:Hello.` | Single fixed response, every turn. No fixture, no network. |
| `replay:fixtures/run.jsonl` | Walk a JSONL fixture of recorded responses, one turn per line. Loops on exhaustion with a warning. |
| `record:fixtures/new.jsonl:anthropic:claude-opus-4-7` | Real provider, but every response appended to the fixture file. Use to capture a fresh recording. |
| `anthropic:claude-opus-4-7` | Real provider, no recording. Costs money. |

For CI, prefer `replay:` — deterministic and free. Capture the
fixture once against a real provider with `record:`, commit the
fixture, replay forever after.

### Fixture format

One JSON object per line:

```jsonl
{"response": "Hello! How can I help?"}
{"response": "I'll read the file now.", "tool_calls": [{"name": "read_file", "args": {"path": "README.md"}, "id": "call_1"}]}
{"response": "Here's the summary..."}
```

`tool_calls` is optional. The agent executes them as if the real
model emitted them — exactly the right shape for testing how the
agent reacts to a tool-call sequence.

## Var substitution

Anywhere a string can appear, `${name}` substitution applies.
Values come from (in order):

1. `--drive-var name=value` on the CLI (overrides everything)
2. `session.vars.name` in the script
3. `vars.name.default` (the `vars:` block)

```yaml
vars:
  who: { type: string, default: "World" }

steps:
  - submit: "hello ${who}"
  - expect_transcript_contains: "hello ${who}"
```

```bash
bog-agents --drive vars.yaml                           # uses "World"
bog-agents --drive vars.yaml --drive-var who=Mars      # uses "Mars"
```

Required vars without a default or override raise immediately. The
script doesn't run with empty placeholder strings.

## Approval mode

`session.approval_mode` controls how unmatched approval dialogs are
handled:

| Value | Behavior |
|---|---|
| `explicit` | Don't auto-respond. The script must use `on_approval` actions. Dialogs that nobody handles fail with a timeout. |
| `auto-all` | Auto-approve everything. Best for happy-path smoke tests. |
| `auto-reads` | Auto-approve read-only tools; surface writes / shell / web for explicit handling. |

The default is `explicit` — the strictest. Use that when your script
is testing the approval flow itself.

## Snapshots

Each `snapshot: <stem>` writes two files:

- `<stem>.svg` — Textual's `save_screenshot`. High-fidelity, good
  for human review, not great for diffing.
- `<stem>.txt` — plain text grid of the visible screen. Lower
  fidelity but `git diff`s cleanly.

Snapshot paths are relative to `--drive-artifacts <dir>` (defaults
to `<script-dir>/.drive-artifacts/<timestamp>/`).

## CLI flags

| Flag | What it does |
|---|---|
| `--drive PATH` | Run the script at PATH |
| `--drive-stdin` | Read the script from stdin (good for `cat foo.yaml \| bog-agents --drive-stdin`) |
| `--drive-var NAME=VALUE` | Override a `${var}`. Repeatable. |
| `--drive-artifacts DIR` | Where snapshots land. Defaults to `<script-dir>/.drive-artifacts/<ts>/`. |
| `--drive-output PATH` | Write JSONL to a file instead of stdout. |
| `--drive-stop-on-failure` | Abort at the first failed step. Subsequent steps land as `ok:false, error:"skipped (prior failure)"`. |

## Worked examples

### Example 1 — slash command surface smoke test

```yaml
session:
  model: fake:slash-smoke
  approval_mode: auto-all

steps:
  - "/help"
  - wait_for_idle: 5
  - expect_transcript_contains: "(?i)usage"

  - "/model"
  - wait_for_idle: 5
  - expect_modal: ModelSelector
  - press: escape
  - wait_for_idle: 5

  - "/threads"
  - wait_for_idle: 5
  - expect_transcript_contains: "(?i)threads"
```

Catches any regression that breaks the dispatch path for these
specific slash commands.

### Example 2 — approval flow

```yaml
session:
  model: replay:fixtures/edit-file-flow.jsonl
  approval_mode: explicit

steps:
  - type: "rename foo to bar in src/main.py"
  - submit
  - wait_for_idle: 10
  - expect_modal: ApprovalMenu
  - on_approval: deny
  - wait_for_idle: 10
  - expect_transcript_contains: "(?i)denied|rejected"
```

Asserts the agent surfaces an approval for the edit, accepts a
denial, and reports the rejection cleanly.

### Example 3 — record a real run, replay forever

Once, against a real provider:

```yaml
# capture.yaml
session:
  model: "record:fixtures/login-fix.jsonl:anthropic:claude-opus-4-7"
  approval_mode: auto-all

steps:
  - submit: "fix the failing test in tests/test_login.py"
  - wait_for_idle: 120
  - expect_transcript_contains: "(?i)test pass|fixed"
  - snapshot: artifacts/login-fix-result
```

```bash
bog-agents --drive capture.yaml
```

Commit `fixtures/login-fix.jsonl`. Then in CI:

```yaml
# verify.yaml — same script, different model spec
session:
  model: "replay:fixtures/login-fix.jsonl"
  approval_mode: auto-all

steps:
  - submit: "fix the failing test in tests/test_login.py"
  - wait_for_idle: 60
  - expect_transcript_contains: "(?i)test pass|fixed"
```

No network. Deterministic. Costs zero.

### Example 4 — multi-modal interaction

```yaml
session:
  model: fake:hi
  approval_mode: auto-all

steps:
  # Open the model picker
  - press: ctrl+m
  - expect_modal: ModelSelector
  # Filter and pick
  - type: "claude"
  - press: enter
  - expect_transcript_contains: "(?i)switched to.*claude"

  # Run a quick prompt
  - submit: "say something"
  - wait_for_idle: 10

  # Snapshot for visual confirmation
  - snapshot: artifacts/after-model-switch
```

## When to use drive (and when not to)

Use drive when:

- You want to lock in the slash-command surface against regression.
- You want a deterministic, cheap, network-free CI smoke test.
- You want to capture a real session as a fixture and replay it.
- You're testing modal flows that a `-p` invocation can't reach.

Don't use drive when:

- You're testing internal Python — write a unit test against
  `app.run_test()` directly.
- You want to run a one-off task at the command line — use `-p` or
  `-n`.
- The flow is purely about model output quality — `/race`,
  `/jury`, or the SDK's evaluation harness fits better.

## Performance notes

- A simple slash-command smoke test runs in ~3 seconds (Pilot boot
  is most of that).
- Real-LLM `wait_for_idle` blocks are the slow part. Use `replay:`
  to skip them entirely in CI.
- Snapshots are cheap (~20ms each). Take liberally.
- Parallel runs: one drive script per process. The Pilot's app is
  single-threaded. CI parallelism uses pytest-xdist on a directory
  of scripts.

---

*The bog has paths through it. Drive is the path that doesn't need
a human walking it to stay clear.*
