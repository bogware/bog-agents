# Findings and security scans

A scan produces findings as prose. Prose isn't something you can gate a build on,
triage over weeks, or hand to a code-scanning dashboard. Bog Agents turns those
findings into a durable **ledger** — one SQLite file per project at
`<repo>/.bog-agents/findings.db` — and gives you a CLI, a CI gate, SARIF output,
and a one-command path from a finding to a fix.

## The stable fingerprint

Every finding is keyed by a fingerprint over the **rule id**, the **path**, and a
**normalised message** — never the line number. That one decision is what makes
the ledger useful over time:

- A re-scan of the same issue **updates** the existing row (bumping `last_seen`
  and `occurrences`) instead of opening a duplicate.
- A finding that stops appearing is auto-marked **`fixed`**.
- A triage decision — `triaged`, `wontfix`, `false_positive`, with a note —
  **sticks** until the code changes enough to change the message.

So a finding you dismissed once stays dismissed, and a finding you fixed doesn't
come back the next time the file shifts by a line.

## `/findings` — the ledger

```text
/findings                          # open findings, worst first
/findings --all                    # every state, including fixed and wontfix
/findings --min high               # severity floor
/findings show <fp>                # one finding in full
/findings triage <fp> false_positive "sanitised at the boundary"
/findings gate --max high          # the CI verdict, printed
/findings sarif out/findings.sarif # write SARIF 2.1.0
/findings record report.md --source security-scan   # ingest a report's `## Findings`
```

`<fp>` accepts the short prefix shown in the table. Findings enter the ledger
three ways: a `/findings record` of any report, the packaged `security-scan`
recipe, or a scheduled daemon scan job (below) — all writing to the same store.

## `/remediate` — a finding to a fix

```text
/remediate <fp>
```

This turns one finding into a focused fix turn: the agent gets the rule, the
severity, the location, the message, and the triage note in its prompt, is told
to confirm the finding is real before changing anything, make the smallest fix
that removes the root cause, add or adjust a test, run the checks, and end with a
PR-ready summary. The finding is marked `triaged` while the fix is in flight. Run
`/pr --evidence` when the fix is in to open a PR with the proof attached.

## Gate CI on it

The headless twin exits non-zero when the gate fails, so it drops straight into a
CI step:

```bash
# fail the build if anything high or worse is still open
bog-agents command "/findings gate --max high"

# upload SARIF to GitHub code scanning
bog-agents command "/findings sarif findings.sarif"
```

`gate` returns exit `1` when open findings sit at or above the threshold, `0`
otherwise. Triaged-away findings (`wontfix`, `false_positive`) don't block.

## The `security-scan` recipe

The packaged recipe runs the full sweep and lands its results in the ledger.
Install it once (it lands in your pipelines directory) and run it with
`/pipeline` — or headless with `--pipeline`:

```text
/recipe install security-scan     # copies it to ~/.bog-agents/pipelines/
/pipeline                          # pick "security-scan" from the picker
```

```bash
bog-agents --pipeline security-scan     # headless
```

It maps the architecture, writes a threat model, spawns hunter sub-agents per
attack class (injection, authz, secrets, SSRF, deserialization, …) under the
run's budget, has a sceptical second reviewer confirm each candidate, attempts a
safe sandbox reproduction, writes the report in the ledger's line format, and
finishes with `/findings record`. Cap the spend with the session budget or a
daemon scan job's `--budget-usd`.

## Scheduled scans with the daemon

Make it a nightly job and your CI reads the results the next morning:

```bash
bog-agents daemon jobs create \
  --name nightly-security \
  --cron "0 2 * * *" \
  --working-dir /srv/app \
  --scan security \
  --scan-gate high \
  --output github_comment --output-github-repo example/app --output-github-issue 1
```

Scan profiles are `security`, `cleanup`, `perf`, or `custom` (your `--prompt`
becomes the rubric). The run's findings land in the scanned repo's ledger — the
same file the interactive `/findings` in that repo reads — and the daemon exposes
`GET /findings`, `/findings/gate`, `/findings/sarif`, and
`POST /findings/{fingerprint}/triage` for programmatic access. A `--scan-gate`
marks the run red (in `run.error`) when open findings sit at or above the
severity, so a failing scan is visible in `/runs` and in the dispatch target.

## From the SDK

The store is a plain SDK primitive — `bog_agents.findings_store` — if you want to
build your own producer:

```python
from bog_agents.findings_store import FindingsStore, parse_findings_text

store = FindingsStore(".bog-agents/findings.db")
store.record(parse_findings_text(scan_output, source="my-scanner", run_id="r7"), run_id="r7")
if not store.gate(max_severity="high").passed:
    raise SystemExit(1)
store.to_sarif()
```

Ask any producer for the shared `FINDINGS_FORMAT_INSTRUCTIONS` line format rather
than inventing another one, so every source writes rows the same store can dedup.

## See also

- [Governed autonomy](governed-autonomy.md) · [Governance & safety](governance.md)
- [Command reference](commands.md)
