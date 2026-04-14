"""Tests for unified extensibility management."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


class TestExtensibility:
    """Tests for merged plugin and extension flows."""

    @staticmethod
    def _make_config_dir() -> Path:
        base = Path("E:/Code/bog-agents/libs/cli/.tmp-ext-tests")
        path = base / uuid4().hex
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_list_extensibility_items_merges_plugins_and_extensions(self) -> None:
        """Installed plugins and extensions should share one listing."""
        from bog_agents_cli.extensibility import list_extensibility_items

        config_dir = self._make_config_dir()
        plugin_dir = config_dir / "plugins" / "formatter"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "name": "formatter",
                    "version": "1.0.0",
                    "description": "Formatting helpers",
                    "commands": [{"name": "/format"}],
                }
            ),
            encoding="utf-8",
        )

        ext_dir = config_dir / "extensions" / "review-pack"
        ext_dir.mkdir(parents=True)
        (ext_dir / "bog-agents-extension.json").write_text(
            json.dumps(
                {
                    "name": "review-pack",
                    "version": "2.0.0",
                    "description": "Review helpers",
                    "skills": ["skills/review/SKILL.md"],
                    "commands": [{"name": "/scout", "prompt": "Scout: {args}"}],
                }
            ),
            encoding="utf-8",
        )

        try:
            items = list_extensibility_items(config_dir)
            names = {(item.kind, item.name) for item in items}
            assert ("plugin", "formatter") in names
            assert ("extension", "review-pack") in names
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)

    def test_extension_commands_are_rendered_with_args(self) -> None:
        """Extension command templates should expand arguments safely."""
        from bog_agents_cli.extensibility import (
            find_extension_command,
            render_extension_command_prompt,
        )

        config_dir = self._make_config_dir()
        ext_dir = config_dir / "extensions" / "review-pack"
        ext_dir.mkdir(parents=True)
        (ext_dir / "bog-agents-extension.json").write_text(
            json.dumps(
                {
                    "name": "review-pack",
                    "version": "1.0.0",
                    "commands": [
                        {
                            "name": "/scout",
                            "aliases": ["/survey"],
                            "description": "Scout the target area",
                            "prompt": "Scout this area: {args}",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        try:
            command = find_extension_command(config_dir, "/survey")
            assert command is not None
            assert command.name == "/scout"
            assert (
                render_extension_command_prompt(command, "services/api")
                == "Scout this area: services/api"
            )
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)

    def test_list_extensions_ignores_unreadable_directory(self) -> None:
        """Extension discovery should degrade cleanly on permission errors."""
        from bog_agents_cli.extensions import list_extensions

        config_dir = self._make_config_dir()
        extensions_dir = config_dir / "extensions"
        extensions_dir.mkdir(parents=True)

        try:
            with patch.object(
                Path,
                "iterdir",
                autospec=True,
                side_effect=lambda path: (
                    (_ for _ in ()).throw(PermissionError("denied"))
                    if path == extensions_dir
                    else []
                ),
            ):
                assert list_extensions(config_dir) == []
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)

    def test_list_extensibility_items_ignores_unreadable_plugins_directory(
        self,
    ) -> None:
        """Plugin discovery should degrade cleanly on permission errors."""
        from bog_agents_cli.extensibility import list_extensibility_items

        config_dir = self._make_config_dir()
        plugins_dir = config_dir / "plugins"
        plugins_dir.mkdir(parents=True)

        try:
            with patch.object(
                Path,
                "iterdir",
                autospec=True,
                side_effect=lambda path: (
                    (_ for _ in ()).throw(PermissionError("denied"))
                    if path == plugins_dir
                    else []
                ),
            ):
                assert list_extensibility_items(config_dir) == []
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)
