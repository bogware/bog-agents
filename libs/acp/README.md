# Bog Agents ACP integration

> *Pass through in harmony. Opinionated where it matters.*

Bring the agent in off the trail and into your editor. This directory contains an [Agent Client Protocol (ACP)](https://agentclientprotocol.com/overview/introduction) connector that runs a Python [Bog Agents](https://github.com/bogware/bog-agents) agent inside any editor that speaks ACP — such as [Zed](https://zed.dev/).

![Bog Agents ACP Demo](./static/img/bog-agents-acp.gif)

It includes an example coding agent that uses Anthropic's Claude models to write code with its built-in filesystem tools and shell, but you can also connect any Bog Agents with additional tools or different agent architectures!

## Getting started

First, make sure you have [Zed](https://zed.dev/) and [`uv`](https://docs.astral.sh/uv/) installed.

Next, clone this repo:

```sh
git clone git@github.com:bogware/bog-agents.git
```

Then, navigate into the newly created folder and run `uv sync`:

```sh
cd bog-agents/libs/acp
uv sync
```

Rename the `.env.example` file to `.env` and add your [Anthropic](https://claude.com/platform/api) API key. You may also optionally set up tracing for your Bog Agents using [LangSmith](https://smith.langchain.com/) by populating the other env vars in the example file:

```ini
ANTHROPIC_API_KEY=""

# Set up LangSmith tracing for your Bog Agents (optional)

# LANGSMITH_TRACING=true
# LANGSMITH_API_KEY=""
# LANGSMITH_PROJECT="bog-agents-acp"
```

Finally, add this to your Zed `settings.json`:

```json
{
  "agent_servers": {
    "Bog Agents": {
      "type": "custom",
      "command": "/your/absolute/path/to/bog-agents-acp/run_demo_agent.sh"
    }
  }
}
```

You must also make sure that the `run_demo_agent.sh` entrypoint file is executable - this should be the case by default, but if you see permissions issues, run:

```sh
chmod +x run_demo_agent.sh
```

Now, open Zed's Agents Panel (e.g. with `CMD + Shift + ?`). You should see an option to create a new Bog Agents thread:

![](./static/img/newbog-agents.png)

And that's it! You can now use the Bog Agents in Zed to interact with your project.

If you need to upgrade your version of Bog Agents, run:

```sh
uv upgrade bog-agents-acp
```

## Launch a custom Bog Agents with ACP

```sh
uv add bog-agents-acp
```

```python
import asyncio

from acp import run_agent
from bog_agents import create_agent
from langgraph.checkpoint.memory import MemorySaver

from bog_agents_acp.server import AgentServerACP


async def get_weather(city: str) -> str:
    """Get weather for a given city."""
    return f"It's always sunny in {city}!"


async def main() -> None:
    agent = create_agent(
        tools=[get_weather],
        system_prompt="You are a helpful assistant",
        checkpointer=MemorySaver(),
    )
    server = AgentServerACP(agent)
    await run_agent(server)


if __name__ == "__main__":
    asyncio.run(main())
```

### Launch with Toad

```sh
uv tool install -U batrachian-toad --python 3.14

toad acp "python path/to/your_server.py" .
# or
toad acp "uv run python path/to/your_server.py" .
```
