"""Acceptance Criteria ingestion.

Sources:

- ``--from-jira <ticket>``: pulls AC from a Jira issue via an MCP Jira tool.
  This module is provider-agnostic — it returns a *fetch contract* that the
  caller (which has the live agent + MCP tool list) executes.
- ``--from-file <path>``: plaintext or markdown.
- ``--from-json <text-or-path>``: structured.
- ``--from-stdin``: piped text.
- Interactive wizard (no flag): the caller drives the wizard; this module
  just exposes :func:`parse_ac_from_text` for the typed answers.

The output is always a list of :class:`AcceptanceCriterion`.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AcceptanceCriterion:
    """A single acceptance criterion.

    Attributes:
        id: Stable identifier for the AC (e.g. ``"AC1"``). Generated
            sequentially when not supplied.
        text: Human-readable statement of the criterion.
        priority: Optional priority hint (``"must"``, ``"should"``, ``"may"``).
        tags: Optional free-form labels for grouping.
        source: Where this AC came from (``"jira:JIRA-134"``, ``"file:..."``,
            etc.) — useful for the report.
    """

    id: str
    text: str
    priority: str = "must"
    tags: list[str] = field(default_factory=list)
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "text": self.text}
        if self.priority and self.priority != "must":
            out["priority"] = self.priority
        if self.tags:
            out["tags"] = list(self.tags)
        if self.source:
            out["source"] = self.source
        return out

    @classmethod
    def from_dict(
        cls, d: dict[str, Any], *, fallback_id: str = "AC?"
    ) -> AcceptanceCriterion:
        return cls(
            id=str(d.get("id") or fallback_id),
            text=str(d.get("text", "")).strip(),
            priority=str(d.get("priority", "must")),
            tags=[str(t) for t in d.get("tags", []) or []],
            source=str(d.get("source", "")),
        )


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

# Common prefixes used in Jira/Confluence/markdown AC lists.
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
# Gherkin lines we keep grouped together.
_GHERKIN_KEYWORDS = ("given", "when", "then", "and ", "but ")


def parse_ac_from_text(text: str, *, source: str = "text") -> list[AcceptanceCriterion]:
    """Parse acceptance criteria out of a freeform plaintext / markdown blob.

    Heuristics:

    - Lines starting with bullet markers (``-``, ``*``, ``1.``, ``2)``) are
      treated as separate ACs.
    - Bare-paragraph blocks (separated by blank lines) become one AC each.
    - Gherkin-style ``Given/When/Then/And/But`` sequences are kept grouped
      under the AC they follow.

    Args:
        text: Raw input.
        source: Provenance label stored on each AC.

    Returns:
        List of :class:`AcceptanceCriterion`. Empty list if nothing parses.
    """
    if not text:
        return []
    normalized = text.replace("\r\n", "\n")
    blocks: list[list[str]] = [[]]
    for raw_line in normalized.split("\n"):
        stripped = raw_line.strip()
        if not stripped:
            if blocks[-1]:
                blocks.append([])
            continue
        # New bullet always starts a new block at the top level.
        if _BULLET_PREFIX.match(raw_line) and not _is_gherkin_continuation(stripped):
            if blocks[-1]:
                blocks.append([])
            blocks[-1].append(_BULLET_PREFIX.sub("", raw_line).strip())
        else:
            blocks[-1].append(stripped)
    blocks = [b for b in blocks if b]

    out: list[AcceptanceCriterion] = []
    for i, block in enumerate(blocks, 1):
        body = "\n".join(block).strip()
        if not body:
            continue
        out.append(AcceptanceCriterion(id=f"AC{i}", text=body, source=source))
    return out


def _is_gherkin_continuation(stripped_lower_in: str) -> bool:
    s = stripped_lower_in.lower()
    return any(s.startswith(kw) for kw in _GHERKIN_KEYWORDS)


def parse_ac_from_json(raw: str, *, source: str = "json") -> list[AcceptanceCriterion]:
    """Parse acceptance criteria from a JSON string.

    Accepted shapes:

    - ``[{"id": "...", "text": "..."}, ...]``
    - ``["text 1", "text 2", ...]`` (ids auto-assigned)
    - ``{"acceptance_criteria": [...]}`` (envelope)

    Args:
        raw: JSON-encoded text.
        source: Provenance label.

    Returns:
        List of :class:`AcceptanceCriterion`.

    Raises:
        ValueError: If the JSON is malformed or the shape is unrecognized.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"--from-json: invalid JSON: {exc}"
        raise ValueError(msg) from exc
    if isinstance(data, dict):
        # Envelope.
        for key in ("acceptance_criteria", "criteria", "ac"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                break
        else:
            msg = "--from-json: object payload must include 'acceptance_criteria'"
            raise ValueError(msg)
    if not isinstance(data, list):
        msg = "--from-json: expected a list of criteria"
        raise ValueError(msg)
    out: list[AcceptanceCriterion] = []
    for i, item in enumerate(data, 1):
        if isinstance(item, str):
            out.append(
                AcceptanceCriterion(id=f"AC{i}", text=item.strip(), source=source)
            )
        elif isinstance(item, dict):
            out.append(AcceptanceCriterion.from_dict(item, fallback_id=f"AC{i}"))
            if not out[-1].source:
                out[-1].source = source
        else:
            logger.debug("--from-json: skipping non-string/object item: %r", item)
    return [ac for ac in out if ac.text]


def load_acceptance_criteria(
    *,
    from_file: str | Path | None = None,
    from_json: str | None = None,
    from_jira_ticket: str | None = None,  # noqa: ARG001 — surfaced in the contract dict
    from_stdin: bool = False,
    inline_text: str | None = None,
) -> list[AcceptanceCriterion]:
    """Load AC from one of the offline sources.

    For Jira ingestion the caller must perform the MCP fetch separately and
    feed the resulting text/JSON back through ``inline_text`` or
    ``from_json``. This module deliberately doesn't talk to the network.

    Args:
        from_file: Path to a .md / .txt / .json file.
        from_json: Inline JSON string OR a path to a JSON file.
        from_jira_ticket: (placeholder — handled by the caller via MCP).
        from_stdin: When True, read from sys.stdin.
        inline_text: Raw text, e.g. from the wizard.

    Returns:
        List of :class:`AcceptanceCriterion`.
    """
    sources_used = sum(1 for x in (from_file, from_json, from_stdin, inline_text) if x)
    if sources_used == 0:
        return []
    if sources_used > 1:
        msg = "load_acceptance_criteria: choose exactly one source"
        raise ValueError(msg)

    if inline_text:
        return parse_ac_from_text(inline_text, source="inline")

    if from_stdin:
        text = sys.stdin.read()
        return parse_ac_from_text(text, source="stdin")

    if from_json:
        # Heuristic: if it looks like a path and the file exists, read it.
        candidate = Path(from_json)
        if candidate.is_file():
            return parse_ac_from_json(
                candidate.read_text(encoding="utf-8"), source=f"file:{candidate}"
            )
        return parse_ac_from_json(from_json, source="json")

    if from_file:
        path = Path(from_file)
        if not path.is_file():
            msg = f"--from-file: not a file: {path}"
            raise ValueError(msg)
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            return parse_ac_from_json(text, source=f"file:{path}")
        return parse_ac_from_text(text, source=f"file:{path}")

    return []
