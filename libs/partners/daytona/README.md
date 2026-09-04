# bog-agents-daytona

Daytona sandbox backend for [Bog Agents](https://github.com/bogware/bog-agents).
The import package is `langchain_daytona` (kept for source compatibility with
the deepagents-era integration this was forked from), but the distribution is
**`bog-agents-daytona`** — `langchain-daytona` on PyPI is langchain-ai's
deepagents package, whose `DaytonaSandbox` targets `deepagents.backends`, not
bog's `BaseSandbox` protocol. Do not install both in one environment.

> Not on PyPI yet. Install from a source checkout of the repository.

## Install

```bash
uv pip install -e libs/partners/daytona          # from the bog-agents repo root
export DAYTONA_API_KEY=...
```

## Use

```python
from daytona import Daytona

from langchain_daytona import DaytonaSandbox

from bog_agents import create_agent

sandbox = Daytona().create()
backend = DaytonaSandbox(
    sandbox=sandbox,
    timeout=300,
    sync_polling_interval=0.25,
)
print(backend.execute("echo hello").output)

agent = create_agent(model="anthropic:claude-opus-4-7", backend=backend)
```

The CLI reaches the same backend through `bog-agents --sandbox daytona`
(`bog-agents-cli[daytona-sandbox]`).

## Tests

```bash
make test                 # unit tests, no network
make integration_test     # runs the SDK's SandboxConformanceSuite against a live Daytona (needs DAYTONA_API_KEY)
```
