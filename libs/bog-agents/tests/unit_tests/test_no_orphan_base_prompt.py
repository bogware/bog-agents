"""Guard against the dead, drifted `base_prompt.md` file reappearing.

`bog_agents/base_prompt.md` was a static copy of the system prompt that nothing
loaded -- the live base prompt is the `BASE_AGENT_PROMPT` constant in `graph.py`.
The orphan had already drifted out of sync (see PARITY.md wave 3). This test fails
if the file comes back, so a future edit can't resurrect a second source of truth.
"""

from pathlib import Path

import bog_agents


def test_orphan_base_prompt_md_stays_deleted() -> None:
    pkg_dir = Path(bog_agents.__file__).parent
    orphan = pkg_dir / "base_prompt.md"
    assert not orphan.exists(), (
        "bog_agents/base_prompt.md was re-added. The live base prompt is the "
        "BASE_AGENT_PROMPT constant in graph.py; a markdown copy is a drift trap."
    )
