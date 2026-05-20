# AWS Bedrock — end-to-end setup

> Bedrock is the AWS-hosted way to run Claude, Nova, Llama, Mistral,
> and DeepSeek behind your AWS account's IAM. It's the right choice
> when compliance, data residency, or an existing AWS footprint
> matter more than the absolute latest features.
>
> It is also the provider most likely to bite a new user. This page
> takes you from zero to a working `/model bedrock_converse:...` call.

## What you need before you start

- An AWS account with IAM permissions for Bedrock.
- An AWS region where the models you want are actually available.
  US-East-1 (N. Virginia) and US-West-2 (Oregon) have the broadest
  coverage. EU-West-3 (Paris) and AP-Northeast-1 (Tokyo) carry the
  Claude profile family but not every variant.
- A credential method. Pick one — they're listed below in
  decreasing order of "easy and works on a laptop":
  1. **`aws configure`** — writes long-lived keys to `~/.aws/credentials`. Fine for solo work.
  2. **`aws sso login`** — refresh-token via your org's SSO. The right default for teams.
  3. **EC2 / ECS instance role** — auto-detected. Nothing to do if you're already on AWS infra.
  4. **Static env vars** — `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` (the last when via STS).

## Install the AWS extra

```bash
pip install --upgrade 'bog-agents-cli[bedrock]'
```

This pulls `langchain-aws` and `boto3` alongside the base CLI. If
you're using the SDK directly:

```bash
pip install --upgrade 'bog-agents[bedrock]'
```

## Inference profiles — the thing that bites everyone

Claude 4.x on Bedrock **requires** a cross-region inference profile
prefix:

| Region you call from | Prefix | Example model id |
|---|---|---|
| us-east-1 / us-west-2 / ca-central-1 | `us.` | `us.anthropic.claude-opus-4-7` |
| eu-west-1 / eu-central-1 / eu-west-3 | `eu.` | `eu.anthropic.claude-sonnet-4-6` |
| ap-southeast-1 / ap-southeast-2 | `apac.` | `apac.anthropic.claude-haiku-4-5-20251001-v1:0` |
| ap-northeast-1 (Tokyo) | `jp.` | `jp.anthropic.claude-opus-4-7` |
| sa-east-1 | `sa.` | `sa.anthropic.claude-sonnet-4-6` |

The bare id `anthropic.claude-opus-4-7` returns `AccessDeniedException`
even when you have model access granted. AWS retired on-demand
throughput for the Claude 4.x line; cross-region profiles are now
the only path.

bog-agents auto-rewrites bare → prefixed based on `AWS_REGION` at
agent-creation time, with a `WARNING` log so the rewrite is visible
in `--debug`. The model picker only lists the prefixed ids, so
clicking through never hits the trap. Set the prefix explicitly to
silence the rewrite log.

## The 30-second setup

```bash
# 1. credentials
aws configure
# or: aws sso login

# 2. region
export AWS_REGION=us-east-1

# 3. install the extra (if you haven't)
pip install --upgrade 'bog-agents-cli[bedrock]'

# 4. confirm AWS can see your account
aws sts get-caller-identity

# 5. probe Bedrock specifically (six-step check)
bog-agents test-bedrock

# 6. start the TUI and pick a model
bog-agents
# then: /model bedrock_converse:us.anthropic.claude-opus-4-7
```

If step 5 prints `[OK]` on every line you're done. If anything is
red, jump to **Fixing what breaks** below.

## Granting model access in the Bedrock console

This trips up everyone the first time. AWS requires explicit
per-region opt-in for every foundation model.

1. Open <https://console.aws.amazon.com/bedrock/>.
2. Top-right region selector → switch to the region you'll call from.
3. Left sidebar → **Model access**.
4. Click **Modify model access**.
5. Tick the models you want. For Claude pick **all variants** so the
   inference-profile family resolves cleanly.
6. **Submit**. Wait ~1 minute for the grant to propagate.

Re-run `bog-agents test-bedrock` after the grant lands.

## Inside the TUI

Once your environment is set up:

- **`/model`** opens the picker. Bedrock entries show **`Bedrock US/EU/APAC`** suffixes for the inference-profile family. Press **Ctrl+T** with a Bedrock model highlighted to run the deep 6-step probe right inside the picker.
- **`/bedrock test`** runs the probe from the chat surface and prints a per-step pass/fail report.
- **`/bedrock fix`** runs the probe and turns each failure into a numbered copy-paste command (set region, `aws sso login`, open the model-access console, etc.). Use this when something's broken and you want the shortest path back.
- **`/bedrock config`** shows the active settings (auth mode, AWS profile, region, the path to `config.toml`) and an example `[models.providers.bedrock_converse]` block you can paste in.
- **`/bedrock status`** is an alias for `/bedrock test`.

## Config file

bog-agents reads `~/.bog-agents/config.toml`. The Bedrock-relevant
keys:

```toml
[models.providers.bedrock_converse]
auth_mode   = "auto"        # auto | iam | profile | sso | env
aws_profile = "my-profile"  # only honored when auth_mode = "profile"
```

`auto` walks the boto3 credential chain in the natural order
(env → AWS_PROFILE → SSO → instance role). Pin to a specific mode
only when you have multiple credential sources and want
deterministic resolution.

Environment-variable equivalents (override the file):

```bash
export BOG_AGENTS_BEDROCK_AUTH_MODE=sso
export BOG_AGENTS_BEDROCK_PROFILE=my-team-sso
export AWS_REGION=us-east-1
```

## Auth modes — when to use which

| Mode | When |
|---|---|
| `auto` | Default. Walks the boto3 credential chain. Right for ~95% of users. |
| `iam` | Force the env-var chain (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`). Use when you want to ignore profiles. |
| `profile` | Pin to a named profile from `~/.aws/credentials`. Pair with `aws_profile = "..."`. |
| `sso` | Force SSO refresh. If your SSO token is expired, this fails fast instead of falling back. |
| `env` | Static env vars only. Useful in CI. |

## Fixing what breaks

Run `bog-agents test-bedrock` (or `/bedrock fix` in the TUI) — the
output points you at the right fix for every common failure. Below
is the same recipe in narrative form.

### `AccessDeniedException` on a Claude 4.x model

You either don't have model access, or you used the bare id instead
of the inference profile.

```bash
# Check the model-access console:
#   https://console.aws.amazon.com/bedrock/  →  Model access
# Then switch the id in /model:
/model bedrock_converse:us.anthropic.claude-opus-4-7
```

### `ValidationException` — invalid model identifier

Almost always wrong region or wrong prefix. The probe step
**ListModels** prints the model ids your account can actually
invoke in the current region.

```bash
bog-agents test-bedrock --model us.anthropic.claude-opus-4-7
```

### `ExpiredTokenException` — SSO refresh

In interactive mode (TTY), bog-agents detects this and runs `aws sso
login` for you automatically. A browser tab opens; approve; the model
call retries once. The session is capped at three auto-refreshes; past
that, the categorized error banner takes over so you can investigate.

In headless mode (`bog-agents -p`, CI, the daemon), no subprocess
spawns — the actionable banner goes to stderr and the call fails fast
so the caller can decide what to do:

```bash
aws sso login
```

Disable auto-refresh entirely via the SDK by omitting
`BedrockRefreshMiddleware` from your middleware list.

### `bedrock` vs `bedrock_converse` — which to use

| | `bedrock:` | `bedrock_converse:` |
|---|---|---|
| AWS API | InvokeModel (legacy) | Converse (recommended) |
| Tool calling | Per-vendor formats | Unified schema |
| Multimodal | Inconsistent | First-class |
| What to pick | Only for a few legacy Cohere variants without Converse adapters | Everything else |

Use `bedrock_converse:` unless you have a specific reason not to.

### `Could not connect to the endpoint`

Corp firewall, VPN, or proxy. Try:

```bash
curl -v https://bedrock-runtime.us-east-1.amazonaws.com
# If that fails:
export HTTPS_PROXY=http://your-corp-proxy:8080
```

### `ThrottlingException`

You hit the on-demand quota. Options:

- Wait — the `provider_retry` middleware backs off automatically.
- Request a quota bump under **Service Quotas → Amazon Bedrock**.
- Switch models — the throttle is per-model per-region.

## Verifying it all works

A minimal end-to-end smoke test from the SDK:

```python
from bog_agents import create_agent

agent = create_agent("bedrock_converse:us.anthropic.claude-sonnet-4-6")
result = agent.invoke({"messages": [{"role": "user", "content": "Say only OK."}]})
print(result["messages"][-1].content)
# OK
```

If that prints `OK` (or anything close), the wiring is correct and
the rest of bog-agents will work as expected — middleware, tool
calls, sub-agents, all of it.

## Related

- [Getting started](../getting-started.md) — full first-run setup, all providers.
- [Troubleshooting](../troubleshooting.md) — non-Bedrock issues.
- [Security](../security.md) — secrets handling, IAM minimums.
