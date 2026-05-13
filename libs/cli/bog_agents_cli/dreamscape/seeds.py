"""Seed snippets mixed into dream prompts.

Dreams want grit to react against — pieces of the world the agent's
context has nothing to say about. A snippet from "nature" or "myth"
or "computing-history" gives the model a non-codebase axis to play
along, which yields more vivid (and useful) imagery than asking it to
free-associate on the same TODO it's been staring at.

The library is hand-curated and intentionally short — five categories,
five seeds each. Quality > quantity; we want every snippet to read
well aloud. New categories or seeds can be added by appending to
``_SEEDS`` — no other module changes needed.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Final

# Curated seed corpus. Each entry is one or two sentences chosen for
# evocative texture, not factual depth. The dream prompt mixes K of
# these with the agent's own memory so the output isn't pure free-
# association on the codebase.
_SEEDS: Final[dict[str, tuple[str, ...]]] = {
    "nature": (
        "A starling murmuration over a Roman ruin at dusk — thousands of birds turning as one.",
        "An old oak grown around the iron of a forgotten fence, the metal now inside the heartwood.",
        "Bioluminescent plankton glowing in the bow-wave of a fishing boat near midnight.",
        "Spider silk that is stronger per gram than steel, spun in absolute silence.",
        "The slow march of glaciers — moving rivers of ice that remember every winter.",
    ),
    "space": (
        "The radio-silence of Pluto's New Horizons probe drifting outward, still transmitting.",
        "Two neutron stars spiralling inward, painting gravitational waves we now hear.",
        "A black hole in M87 the size of our solar system, casting a shadow we photographed.",
        "Voyager 1 entering interstellar space carrying a golden record we will never see again.",
        "The CMB — a faint hum from when the universe first became transparent.",
    ),
    "history": (
        "Ada Lovelace describing engines that would compose music a century before they existed.",
        "The library of Alexandria's lost catalogs — books we know the titles of but never read.",
        "An eight-year-old Mozart writing a symphony in 1764 in a room he no longer remembers.",
        "Antikythera — bronze gears the size of a fist that tracked the sky two thousand years ago.",
        "A Roman aqueduct still running, still flowing, no one alive who built it.",
    ),
    "myth": (
        "Sigurd, who learned the speech of birds after tasting a dragon's heart.",
        "Anansi the spider, who tricked the sky-god into giving up all the stories.",
        "The Welsh story of Taliesin reborn from a transformation through hare, fish, bird, grain.",
        "Bifröst — a bridge of rainbow that will burn when the wolf comes.",
        "Mímir's well, whose water grants wisdom in exchange for one of your eyes.",
    ),
    "computing-history": (
        "Margaret Hamilton hand-writing the rope memory of Apollo 11 in a Cambridge lab.",
        "The Multics team designing for hardware that wouldn't exist for ten years.",
        "Doug Engelbart demoing the mouse, hyperlinks, and video calls in a single 1968 hour.",
        "Smalltalk-72 children at PARC programming a graphical world before Apple existed.",
        "Knuth interrupting all work on TAOCP to spend ten years writing TeX, so it would render right.",
    ),
}


def list_categories() -> list[str]:
    """Return all known seed categories."""
    return list(_SEEDS.keys())


def pick_seeds(
    categories: Sequence[str], *, count: int, rng: random.Random | None = None
) -> list[str]:
    """Pick ``count`` distinct snippets, sampling across the chosen categories.

    Empty ``categories`` (or any value not in the corpus) draws from
    every category. Sampling is deterministic when a seeded ``rng`` is
    supplied — used by tests to pin output.
    """
    chooser = rng or random.Random()
    pool: list[str] = []
    chosen = categories or list_categories()
    for cat in chosen:
        pool.extend(_SEEDS.get(cat, ()))
    if not pool:
        return []
    n = max(0, min(count, len(pool)))
    return chooser.sample(pool, n)
