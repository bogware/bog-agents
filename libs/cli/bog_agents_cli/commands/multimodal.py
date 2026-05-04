"""Multimodal input commands (images, audio, video)."""

from __future__ import annotations

from bog_agents_cli._spec import SlashCommandSpec
from bog_agents_cli.commands._base import SlashCommand

COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        spec=SlashCommandSpec(
            "/image",
            "Analyze images or paste from clipboard",
            "screenshot multimodal",
            "multimodal",
            available=True,
        ),
        handler_method="_handle_image_command",
    ),
)
