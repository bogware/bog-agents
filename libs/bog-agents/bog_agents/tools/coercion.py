"""Semantic argument coercion for tool schemas.

Models frequently emit argument values that are semantically right but
textually loose: ``staged="yes"`` instead of ``staged=True``, or
``limit="1k"`` instead of ``limit=1000``. These helpers coerce such values
into their declared types so tool authors accept them without hand-rolled
parsing at the top of every tool body.

Usage with ``StructuredTool``/``create_agent``::

    from typing import Annotated

    from bog_agents.tools import SemanticBool, SemanticNumber


    def deploy(*, canary: SemanticBool = False, replicas: SemanticNumber = 1) -> str: ...

The JSON schema emitted for the model stays ``boolean`` / ``number`` —
coercion happens at validation time via a pydantic ``BeforeValidator``, so
existing schema tests and strictly-typed tool contracts are unaffected.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BeforeValidator

__all__ = [
    "SemanticBool",
    "SemanticNumber",
    "semantic_bool",
    "semantic_number",
]

# Case-insensitive string spellings accepted by `semantic_bool`. The truthy
# set mirrors the `BOG_AGENTS_*` env-var convention (see
# `bog_agents_cli._env_vars`) plus the affirmative/negative tokens a chat
# model is likely to emit when it "reasons in prose".
_TRUTHY_STRINGS = frozenset({"1", "true", "yes", "on", "y", "t", "enabled", "enable", "allow", "allowed"})
_FALSY_STRINGS = frozenset({"0", "false", "no", "off", "n", "f", "disabled", "disable", "deny", "denied", ""})

# Word-number vocabulary for `semantic_number` (bounded, deterministic).
_ONES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000}
_NUMBER_SUFFIX_SCALE = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
_CURRENCY_PREFIXES = ("$", "€", "£", "¥")
_DIGIT_RE = re.compile(r"^[-+]?\d+$")
_DECIMAL_RE = re.compile(r"^[-+]?(?:\d+\.\d*|\.\d+)$")


def _parse_word_number(text: str) -> int | None:
    """Parse a bounded English number phrase into an integer, or `None`.

    Supports the canonical forms models produce: ``"forty two"``, ``"42"``,
    ``"three thousand"``, ``"one million"``. Hyphens and the conjunction
    ``"and"`` are tolerated. Returns `None` when the phrase is not a
    well-formed number so callers can fall back to a clear error.

    Args:
        text: Lowercased, whitespace-trimmed number phrase.

    Returns:
        The parsed integer, or `None` if the phrase is unrecognized.
    """
    tokens = re.split(r"[\s\-]+", text)
    tokens = [t for t in tokens if t and t != "and"]
    if not tokens:
        return None
    total = 0
    current = 0
    for token in tokens:
        if token in _ONES:
            current += _ONES[token]
        elif token in _TENS:
            current += _TENS[token]
        elif token in _SCALES:
            if current == 0:
                current = 1
            current *= _SCALES[token]
            total += current
            current = 0
        else:
            return None
    return total + current


def semantic_bool(value: object) -> bool:
    """Coerce a model-provided value into a boolean.

    Accepts booleans, zero/non-zero numbers, and a bounded set of English
    spellings (`true`/`yes`/`on`/`1`/… and their negations, case-insensitive,
    surrounding whitespace ignored). An unrecognized string raises `ValueError`
    so a garbage argument surfaces at validation time instead of silently
    becoming `True`; a non-scalar type raises `TypeError`.

    Args:
        value: The raw value the model supplied for a boolean-typed argument.

    Returns:
        The coerced boolean.

    Raises:
        TypeError: If `value` is neither a scalar nor a string.
        ValueError: If a string `value` cannot be interpreted as a boolean.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUTHY_STRINGS:
            return True
        if normalized in _FALSY_STRINGS:
            return False
        msg = f"Cannot interpret {value!r} as a boolean; expected true/false, yes/no, on/off, 1/0, or enabled/disabled"
        raise ValueError(msg)
    msg = f"Cannot interpret {type(value).__name__} value {value!r} as a boolean"
    raise TypeError(msg)


def semantic_number(value: object) -> int | float:
    """Coerce a model-provided value into an `int` or `float`.

    Accepts numbers, plain digit strings, and the loose forms a model is
    likely to emit: thousands separators (`"1,000"`), currency prefixes
    (`"$42"`), percentages (`"10%"`), scale suffixes (`"1k"`, `"1.5m"`,
    `"2B"`), and bounded English number phrases (`"forty two"`,
    `"three thousand"`). Integral results return `int`; fractional results
    return `float`.

    Args:
        value: The raw value the model supplied for a numeric argument.

    Returns:
        The coerced numeric value.

    Raises:
        TypeError: If `value` is a boolean or a non-scalar type.
        ValueError: If `value` cannot be interpreted as a number.
    """
    if isinstance(value, bool):
        msg = f"Cannot interpret boolean {value!r} as a number; pass an integer or numeric string"
        raise TypeError(msg)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if not isinstance(value, str):
        msg = f"Cannot interpret {type(value).__name__} value {value!r} as a number"
        raise TypeError(msg)

    text = value.strip()
    if not text:
        msg = "Cannot interpret an empty string as a number"
        raise ValueError(msg)
    lowered = text.lower()
    for prefix in _CURRENCY_PREFIXES:
        if lowered.startswith(prefix):
            lowered = lowered[1:].strip()
            break
    for suffix in ("percent", "%"):
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)].strip()
            break
    # Comma thousands-separators apply to digit forms only; strip them
    # before both parses so "1,000" and "1 000" both work.
    lowered = lowered.replace(",", "")

    word_number = _parse_word_number(lowered)
    if word_number is not None:
        return word_number

    lowered = lowered.replace(" ", "")
    scale = 1.0
    if lowered:
        last_char = lowered[-1]
        if last_char in _NUMBER_SUFFIX_SCALE:
            scale = float(_NUMBER_SUFFIX_SCALE[last_char])
            lowered = lowered[:-1]

    if not lowered:
        msg = f"Cannot interpret {value!r} as a number"
        raise ValueError(msg)

    if not (_DIGIT_RE.match(lowered) or _DECIMAL_RE.match(lowered)):
        msg = f"Cannot interpret {value!r} as a number"
        raise ValueError(msg)

    try:
        number = float(lowered) * scale
    except (OverflowError, ValueError) as exc:
        msg = f"Cannot interpret {value!r} as a number"
        raise ValueError(msg) from exc

    if number.is_integer():
        return int(number)
    return number


# Pydantic-friendly aliases: use these as tool-arg type hints so coercion
# runs automatically whenever `StructuredTool`/`create_agent` validates a
# call. The emitted JSON schema keeps the plain type (boolean / number).
SemanticBool = Annotated[bool, BeforeValidator(semantic_bool)]
SemanticNumber = Annotated[int | float, BeforeValidator(semantic_number)]
