"""``/telephone`` — rewrite a casual user prompt as a production-grade LLM prompt.

Rationale: most agentic failures trace back to ambiguous, under-specified
prompts. ``/telephone`` runs the user's message through a focused
prompt-engineering pass before submitting it to the main agent. The
rewriter system prompt lives in ``model_config.toml`` (under
``[telephone]``) so each user can tune it; ``/settings`` exposes an
``Edit telephone prompt`` action for quick edits.

Entry points:

* :func:`rewrite_prompt_with_model` — the pure async function that
  invokes the configured model and returns the rewritten prompt.
* :func:`load_system_prompt` / :func:`save_system_prompt` — read/write
  the configurable system prompt via the standard model_config TOML
  surface.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


DEFAULT_TELEPHONE_SYSTEM_PROMPT = """\
You are a senior prompt engineer rewriting a user's request into a clearer,
more actionable instruction for an AI coding agent.

Your job is NOT to answer the request, NOT to add extra work, and NOT to
guess details that the user did not specify. You preserve the user's intent
EXACTLY while improving clarity, structure, and concreteness.

Rewrite the input following these rules:
1. Lead with the goal in one sentence.
2. List concrete deliverables and acceptance criteria as bullet points
   when the original implies more than one outcome.
3. Surface implicit constraints (file paths, frameworks, languages,
   environment) only if the user already mentioned them.
4. Replace vague verbs ("clean up", "improve") with specific actions.
5. Preserve the original tone and any explicit "do not" / "must" rules.
6. If the request is fundamentally unclear, output a single line:
   `CLARIFY: <one targeted question>` — never invent details.

Output ONLY the rewritten prompt — no commentary, no preamble, no markdown
fence around the whole response. The rewrite must be ready to send to the
agent verbatim.
"""

_TELEPHONE_TOML_KEY = "telephone"
_SYSTEM_PROMPT_FIELD = "system_prompt"


def load_system_prompt(config_path: Path | None = None) -> str:
    """Return the active rewriter system prompt.

    Reads ``[telephone].system_prompt`` from the user's model_config TOML
    file, falling back to :data:`DEFAULT_TELEPHONE_SYSTEM_PROMPT`.

    Args:
        config_path: Optional override for the config TOML path. Defaults
            to the standard ``~/.bog-agents/config.toml`` resolved by
            ``model_config``.

    Returns:
        The system prompt string. Always returns a non-empty value.
    """
    import tomllib

    from bog_agents_cli.model_config import DEFAULT_CONFIG_PATH

    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return DEFAULT_TELEPHONE_SYSTEM_PROMPT
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        logger.debug("Could not read telephone prompt from %s", path, exc_info=True)
        return DEFAULT_TELEPHONE_SYSTEM_PROMPT
    section = data.get(_TELEPHONE_TOML_KEY, {})
    if not isinstance(section, dict):
        return DEFAULT_TELEPHONE_SYSTEM_PROMPT
    prompt = section.get(_SYSTEM_PROMPT_FIELD)
    if isinstance(prompt, str) and prompt.strip():
        return prompt
    return DEFAULT_TELEPHONE_SYSTEM_PROMPT


def save_system_prompt(prompt: str, config_path: Path | None = None) -> bool:
    """Persist a custom rewriter system prompt to the model_config TOML.

    Args:
        prompt: New system prompt. Pass an empty string to fall back to
            the default on next read.
        config_path: Optional override for the TOML path.

    Returns:
        True on successful write.
    """
    import contextlib
    import os
    import tempfile
    import tomllib

    import tomli_w

    from bog_agents_cli.model_config import DEFAULT_CONFIG_PATH

    path = config_path or DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if path.exists():
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            logger.warning("Replacing unparseable %s when saving telephone prompt", path)
            data = {}
    section = data.get(_TELEPHONE_TOML_KEY)
    if not isinstance(section, dict):
        section = {}
    section[_SYSTEM_PROMPT_FIELD] = prompt
    data[_TELEPHONE_TOML_KEY] = section

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(data, f)
            Path(tmp_path).replace(path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except (OSError, ValueError):
        logger.exception("Failed to save telephone prompt to %s", path)
        return False
    return True


async def rewrite_prompt_with_model(
    user_prompt: str,
    model: BaseChatModel,
    *,
    system_prompt: str | None = None,
) -> str:
    """Run ``user_prompt`` through ``model`` using the rewriter system prompt.

    Args:
        user_prompt: The casual / under-specified text the user typed.
        model: A LangChain ``BaseChatModel`` to call.
        system_prompt: Optional explicit system prompt. When ``None`` the
            current configured prompt is loaded via
            :func:`load_system_prompt`.

    Returns:
        The rewritten prompt. The function strips leading/trailing
        whitespace and any accidental markdown fence the model added.

    Raises:
        ValueError: If ``user_prompt`` is empty.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    cleaned = (user_prompt or "").strip()
    if not cleaned:
        msg = "rewrite_prompt_with_model() requires a non-empty user_prompt"
        raise ValueError(msg)

    sp = system_prompt if system_prompt is not None else load_system_prompt()
    response = await model.ainvoke(
        [SystemMessage(content=sp), HumanMessage(content=cleaned)],
    )
    text = response.content if hasattr(response, "content") else str(response)
    if isinstance(text, list):
        # Multimodal block list — concatenate text parts.
        parts: list[str] = []
        for part in text:
            if isinstance(part, dict) and part.get("type") == "text":
                value = part.get("text")
                if isinstance(value, str):
                    parts.append(value)
            elif isinstance(part, str):
                parts.append(part)
        text = "".join(parts)

    rewritten = str(text or "").strip()
    return _strip_outer_fence(rewritten)


def _strip_outer_fence(text: str) -> str:
    """Drop a single outer ``` fence if the model wrapped its whole reply."""
    if not text:
        return text
    lines = text.splitlines()
    if (
        len(lines) >= 2
        and lines[0].startswith("```")
        and lines[-1].startswith("```")
    ):
        return "\n".join(lines[1:-1]).strip()
    return text


__all__ = [
    "DEFAULT_TELEPHONE_SYSTEM_PROMPT",
    "load_system_prompt",
    "rewrite_prompt_with_model",
    "save_system_prompt",
]
