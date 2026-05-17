"""Expert Mode sub-package — split out of ``expert_controller.py`` (K4).

Each module hosts the implementation of one logical concern:

* :mod:`._helpers` — parsing utilities and placeholders shared by the
  other sub-modules.
* :mod:`.write` — ``/expert write`` LLM-driven rule authoring.
* :mod:`.wizard` — ``/expert wizard`` guided setup.
* :mod:`.propose` — ``/expert propose`` + ``/expert proposals`` flows.
* :mod:`.watch` — ``/expert watch`` scheduled-proposer dispatcher.

The public :class:`bog_agents_cli.expert_controller.ExpertController`
keeps its method surface unchanged — each method is a thin delegator
to one of these sub-modules, so existing callers (tests, the TUI
handler, slash-command dispatcher) keep working without import
changes.
"""

from __future__ import annotations
