"""Generated worktree names (petnames) for ``create`` with no NAME.

The point of branchless create is that nobody has to invent a name up front,
so the generated one only needs to be pronounceable and collision-free — the
enriched ``list`` (branch, last commit, age) carries the recognition load.
Two small word lists keep the tool dependency-free; ~2.5k combinations is
plenty for a directory of short-lived worktrees.
"""

from __future__ import annotations

import random
from collections.abc import Callable

_ADJECTIVES = (
    "able",
    "amber",
    "azure",
    "bold",
    "brave",
    "brisk",
    "calm",
    "cedar",
    "civil",
    "clear",
    "clever",
    "coral",
    "cosy",
    "crisp",
    "daring",
    "deft",
    "dewy",
    "eager",
    "early",
    "fable",
    "fair",
    "fleet",
    "fond",
    "free",
    "gentle",
    "glad",
    "golden",
    "grand",
    "green",
    "happy",
    "hardy",
    "hazel",
    "honest",
    "ivory",
    "jolly",
    "keen",
    "kind",
    "light",
    "lively",
    "loyal",
    "lucid",
    "mellow",
    "merry",
    "mild",
    "noble",
    "opal",
    "pale",
    "plucky",
    "proud",
    "quick",
    "quiet",
    "rapid",
    "rosy",
    "royal",
    "rustic",
    "sage",
    "sandy",
    "sharp",
    "silent",
    "silver",
    "smart",
    "snug",
    "solid",
    "sunny",
    "swift",
    "tidy",
    "trusty",
    "vivid",
    "warm",
    "wise",
    "witty",
    "young",
)

_ANIMALS = (
    "otter",
    "heron",
    "lynx",
    "falcon",
    "badger",
    "beaver",
    "bison",
    "crane",
    "dove",
    "eagle",
    "ferret",
    "finch",
    "fox",
    "gecko",
    "hare",
    "hawk",
    "ibex",
    "jay",
    "koala",
    "lark",
    "lemur",
    "llama",
    "marten",
    "mole",
    "moose",
    "newt",
    "ocelot",
    "oriole",
    "osprey",
    "owl",
    "panda",
    "pelican",
    "pika",
    "plover",
    "puffin",
    "quail",
    "raven",
    "robin",
    "seal",
    "shrew",
    "sparrow",
    "stoat",
    "stork",
    "swan",
    "swift",
    "tapir",
    "tern",
    "toucan",
    "trout",
    "vole",
    "walrus",
    "weasel",
    "whale",
    "wombat",
    "wren",
    "yak",
)

_MAX_TRIES = 64


def petname(taken: Callable[[str], bool]) -> str:
    """A fresh ``adjective-animal`` name for which ``taken(name)`` is False.

    Falls back to a numbered suffix if the (astronomically unlikely) case of
    ``_MAX_TRIES`` straight collisions ever happens, so create never hangs.
    """
    for _ in range(_MAX_TRIES):
        name = f"{random.choice(_ADJECTIVES)}-{random.choice(_ANIMALS)}"
        if not taken(name):
            return name
    base = f"{random.choice(_ADJECTIVES)}-{random.choice(_ANIMALS)}"
    n = 2
    while taken(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"
