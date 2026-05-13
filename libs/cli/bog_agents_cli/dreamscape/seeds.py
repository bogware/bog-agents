"""Seed snippets mixed into dream prompts.

Dreams want grit to react against — pieces of the world the agent's
context has nothing to say about. A snippet from "nature" or "myth"
or "computing-history" gives the model a non-codebase axis to play
along, which yields more vivid (and useful) imagery than asking it to
free-associate on the same TODO it's been staring at.

The library is hand-curated. Five categories, ten seeds each
(50 total — doubled from the original 25 after Phase 1 flagged
title repetition risk above ~30 dreams). Quality > quantity; we
want every snippet to read well aloud. New categories or seeds can
be added by appending to ``_SEEDS`` — no other module changes
needed.
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
        "The Australian lyrebird mimicking chainsaws, car alarms, and the click of camera shutters.",
        "A mycorrhizal fungal network connecting old-growth trees underground, trading sugars for minerals.",
        "Arctic terns flying pole to pole each year, accumulating two lifetimes of sunlight.",
        "An octopus solving a screw-top jar by watching another octopus do it first.",
        "The bristlecone pines of the White Mountains — five thousand years still standing in stone-thin air.",
    ),
    "space": (
        "The radio-silence of Pluto's New Horizons probe drifting outward, still transmitting.",
        "Two neutron stars spiralling inward, painting gravitational waves we now hear.",
        "A black hole in M87 the size of our solar system, casting a shadow we photographed.",
        "Voyager 1 entering interstellar space carrying a golden record we will never see again.",
        "The CMB — a faint hum from when the universe first became transparent.",
        "The Hubble Deep Field — ten thousand galaxies in a patch of sky the size of a sand grain at arm's length.",
        "Cassini's final dive into Saturn's atmosphere, transmitting until the heat shield failed.",
        "16 Psyche — an asteroid made almost entirely of nickel-iron, the exposed core of a dead planet.",
        "The Carrington event of 1859 — a solar storm that set telegraph wires on fire as far south as Cuba.",
        "A radio echo from a supernova whose light we can still trace through interstellar gas a thousand years later.",
    ),
    "history": (
        "Ada Lovelace describing engines that would compose music a century before they existed.",
        "The library of Alexandria's lost catalogs — books we know the titles of but never read.",
        "An eight-year-old Mozart writing a symphony in 1764 in a room he no longer remembers.",
        "Antikythera — bronze gears the size of a fist that tracked the sky two thousand years ago.",
        "A Roman aqueduct still running, still flowing, no one alive who built it.",
        "Edith Clarke designing graphical calculators for power grids in the 1920s, before women could vote.",
        "The Voynich manuscript — a 600-year-old book in an alphabet that no one has ever read.",
        "Herostratus burning down the temple of Artemis specifically so future generations would remember his name.",
        "Pythagoras forbidding his students to speak in his presence for five full years before they could ask a question.",
        "Hatshepsut, who ruled Egypt as Pharaoh and had her successors spell her in the masculine on every wall.",
    ),
    "myth": (
        "Sigurd, who learned the speech of birds after tasting a dragon's heart.",
        "Anansi the spider, who tricked the sky-god into giving up all the stories.",
        "The Welsh story of Taliesin reborn from a transformation through hare, fish, bird, grain.",
        "Bifröst — a bridge of rainbow that will burn when the wolf comes.",
        "Mímir's well, whose water grants wisdom in exchange for one of your eyes.",
        "Tezcatlipoca, the smoking mirror that shows what you are unwilling to see.",
        "Coyote stealing fire from the fire-people and carrying it home in his fur, burning his back forever.",
        "The selkie who hides her sealskin in the rafters and returns to the sea twenty years later.",
        "Inanna's descent — passing through seven gates, surrendering one ornament at each, arriving naked at the underworld.",
        "The roc, whose wings darkened deserts and whose talons carried elephants to its young.",
    ),
    "computing-history": (
        "Margaret Hamilton hand-writing the rope memory of Apollo 11 in a Cambridge lab.",
        "The Multics team designing for hardware that wouldn't exist for ten years.",
        "Doug Engelbart demoing the mouse, hyperlinks, and video calls in a single 1968 hour.",
        "Smalltalk-72 children at PARC programming a graphical world before Apple existed.",
        "Knuth interrupting all work on TAOCP to spend ten years writing TeX, so it would render right.",
        "Vint Cerf and Bob Kahn sketching the TCP/IP packet header on a hotel napkin in San Francisco.",
        "Grace Hopper preserving an actual moth in the Mark II log book under the entry 'first actual case of bug being found'.",
        "The TX-0 at MIT in 1956, programmed by undergrads through the night via paper tape because nobody else wanted the machine.",
        "Mel Kaye writing the unreadable real-time assembly of the Royal McBee LGP-30, single-stepping through interrupts on a drum.",
        "Ken Thompson and Dennis Ritchie inventing Unix on a cast-off PDP-7 because nobody would let them use the new machine.",
    ),
    "engineering-craft": (
        # Added in Phase 15 — software-engineering-specific seeds for
        # engineering-classified agents. Not historical figures; the
        # texture is the everyday craft of debugging, refactoring,
        # and the long tail of "this has been wrong for years."
        "The 3am page where the on-call engineer found the bug had been live for six years.",
        "The git bisect that ended at the merge commit whose author had left the company.",
        "The assertion that fired only at midnight UTC, only on Fridays, only after a daylight-savings transition.",
        "The off-by-one in the parser that everyone agreed was a feature.",
        "The retry policy that worked perfectly for three years, until the downstream API added a 429 response code.",
        "The cache hit that was actually a cache miss because the key contained a NaN.",
        "The deprecated API still in production six years after EOL, quietly holding up everything that replaced it.",
        "The unit test that passed locally and on CI but failed in prod because prod's NTP was 47 seconds off.",
        "The dependency that worked because its bugs canceled the bugs in the library it wrapped.",
        "The performance regression that traced back to a benchmark harness which was itself the bottleneck.",
        "The feature flag that was meant to be temporary in 2017 and is now load-bearing.",
        "The migration script that ran perfectly in staging and had to be re-run in prod because staging's DB used a different collation.",
        "The integration test suite that quietly stopped running for eighteen months because someone renamed the make target.",
        "The log line added 'just for this one debug session' that is now shipped to a third-party SIEM.",
        "The TODO comment from 2014 that was wrong then and is wrong now in the exact opposite way.",
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
