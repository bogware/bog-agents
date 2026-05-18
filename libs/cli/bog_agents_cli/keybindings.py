"""Custom keybinding configuration.

Feature #24: Customizable key bindings — load and apply user-defined
keybindings from a JSON configuration file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Default keybinding configuration
DEFAULT_KEYBINDINGS: dict[str, str] = {
    "submit": "enter",
    "newline": "shift+enter",
    "cancel": "escape",
    "approve": "y",
    "reject": "n",
    "scroll_up": "shift+up",
    "scroll_down": "shift+down",
    "clear": "ctrl+l",
    "quit": "ctrl+c",
    "history_prev": "up",
    "history_next": "down",
    "compact": "ctrl+shift+c",
    "undo": "ctrl+z",
    "diff": "ctrl+d",
}


@dataclass
class KeybindingConfig:
    """Keybinding configuration."""

    bindings: dict[str, str] = field(default_factory=dict)
    """Action name -> key binding mapping."""

    def get(self, action: str) -> str:
        """Get the key binding for an action.

        Args:
            action: Action name.

        Returns:
            Key binding string, falling back to default.
        """
        return self.bindings.get(action, DEFAULT_KEYBINDINGS.get(action, ""))


def load_keybindings(config_dir: Path) -> KeybindingConfig:
    """Load keybinding configuration from disk.

    Args:
        config_dir: Config directory path.

    Returns:
        KeybindingConfig with merged user + default bindings.
    """
    keybindings_file = config_dir / "keybindings.json"
    user_bindings: dict[str, str] = {}

    if keybindings_file.exists():
        try:
            data = json.loads(keybindings_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                user_bindings = {k: v for k, v in data.items() if isinstance(v, str)}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load keybindings: %s", e)

    # Merge: user overrides take precedence
    merged = {**DEFAULT_KEYBINDINGS, **user_bindings}
    return KeybindingConfig(bindings=merged)


def save_keybindings(config_dir: Path, bindings: dict[str, str]) -> Path:
    """Save keybinding configuration to disk.

    Args:
        config_dir: Config directory path.
        bindings: Keybinding mappings to save.

    Returns:
        Path to the saved file.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    keybindings_file = config_dir / "keybindings.json"
    keybindings_file.write_text(json.dumps(bindings, indent=2))
    return keybindings_file


def format_keybindings(config: KeybindingConfig) -> str:
    """Format keybindings for display.

    Args:
        config: Keybinding configuration.

    Returns:
        Formatted string showing all keybindings.
    """
    lines = ["## Keybindings\n"]
    for action, key in sorted(config.bindings.items()):
        default = DEFAULT_KEYBINDINGS.get(action, "")
        customized = " (custom)" if key != default else ""
        lines.append(f"  {action:<20} {key}{customized}")
    return "\n".join(lines)
