"""Tests for ``bog_agents_cli.telephone``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bog_agents_cli.telephone import (
    DEFAULT_TELEPHONE_SYSTEM_PROMPT,
    _strip_outer_fence,
    load_system_prompt,
    rewrite_prompt_with_model,
    save_system_prompt,
)


def test_load_system_prompt_uses_default_when_missing(tmp_path: Path) -> None:
    """No config file → fall back to the bundled default prompt."""
    cfg = tmp_path / "config.toml"
    assert load_system_prompt(cfg) == DEFAULT_TELEPHONE_SYSTEM_PROMPT


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    """Saving a prompt then reading returns the same string."""
    cfg = tmp_path / "config.toml"
    custom = "be terse and assume nothing"
    assert save_system_prompt(custom, cfg)
    assert load_system_prompt(cfg) == custom


def test_save_preserves_unrelated_keys(tmp_path: Path) -> None:
    """Writing the telephone section must NOT clobber sibling sections."""
    import tomli_w

    cfg = tmp_path / "config.toml"
    with cfg.open("wb") as f:
        tomli_w.dump({"models": {"default": "anthropic:claude-sonnet-4-6"}}, f)

    assert save_system_prompt("custom", cfg)
    import tomllib

    with cfg.open("rb") as f:
        data = tomllib.load(f)
    assert data["models"] == {"default": "anthropic:claude-sonnet-4-6"}
    assert data["telephone"]["system_prompt"] == "custom"


def test_load_falls_back_when_section_malformed(tmp_path: Path) -> None:
    """A non-string ``system_prompt`` value falls back to the default."""
    import tomli_w

    cfg = tmp_path / "config.toml"
    with cfg.open("wb") as f:
        tomli_w.dump({"telephone": {"system_prompt": ""}}, f)
    assert load_system_prompt(cfg) == DEFAULT_TELEPHONE_SYSTEM_PROMPT


async def test_rewrite_invokes_model_with_system_and_user_messages() -> None:
    """``rewrite_prompt_with_model`` sends a 2-message conversation."""
    response = MagicMock()
    response.content = "REWRITTEN"
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=response)

    out = await rewrite_prompt_with_model(
        "fix the thing", model, system_prompt="rewrite please"
    )
    assert out == "REWRITTEN"
    model.ainvoke.assert_awaited_once()
    args = model.ainvoke.await_args.args[0]
    assert len(args) == 2
    # First message must be the system prompt; second is the user prompt.
    assert args[0].content == "rewrite please"
    assert args[1].content == "fix the thing"


async def test_rewrite_strips_outer_code_fence() -> None:
    """If the model wrapped its reply in ```…``` the fence is removed."""
    response = MagicMock()
    response.content = "```\nREWRITTEN\nstill here\n```"
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=response)
    out = await rewrite_prompt_with_model("anything", model, system_prompt="x")
    assert out == "REWRITTEN\nstill here"


async def test_rewrite_handles_multimodal_text_blocks() -> None:
    """Some providers return content as a list of blocks; we concat text parts."""
    response = MagicMock()
    response.content = [
        {"type": "text", "text": "hello "},
        {"type": "text", "text": "world"},
        {"type": "image_url", "image_url": "data:..."},
    ]
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=response)
    assert (
        await rewrite_prompt_with_model("anything", model, system_prompt="x")
        == "hello world"
    )


async def test_rewrite_rejects_empty_prompt() -> None:
    """An empty / whitespace-only input is a programmer error."""
    model = MagicMock()
    with pytest.raises(ValueError, match="non-empty"):
        await rewrite_prompt_with_model("   ", model, system_prompt="x")


def test_strip_outer_fence_passthrough_when_no_fence() -> None:
    assert _strip_outer_fence("plain text") == "plain text"


def test_strip_outer_fence_keeps_inner_fences() -> None:
    text = "```python\nprint('hello')\n```"
    # Single inner fence, single closing — should be stripped to inner.
    assert _strip_outer_fence(text) == "print('hello')"
