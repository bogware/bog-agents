"""Interactive settings screen for /settings command."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

from bog_agents_cli.model_config import (
    DEFAULT_CONFIG_PATH,
    ModelConfig,
    has_provider_credentials,
    save_bedrock_credential_check as _save_bedrock_credential_check,
    save_default_model,
    save_fallbacks,
)

logger = logging.getLogger(__name__)

# Sections the user can navigate between
_SECTIONS = ("default_model", "fallbacks", "providers", "config_path")


class SettingsScreen(ModalScreen[bool]):
    """Full-screen modal for managing ~/.bog-agents/config.toml settings.

    Returns True if settings were changed, False/None on cancel.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    CSS = """
    SettingsScreen {
        align: center middle;
    }

    SettingsScreen > Vertical {
        width: 90;
        max-width: 95%;
        height: 85%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    SettingsScreen .settings-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    SettingsScreen .settings-section-header {
        color: $primary;
        text-style: bold;
        margin-top: 1;
    }

    SettingsScreen .settings-section-content {
        height: auto;
        padding: 0 2;
    }

    SettingsScreen .settings-list {
        height: 1fr;
        min-height: 10;
        scrollbar-gutter: stable;
        background: $background;
        padding: 1 1;
    }

    SettingsScreen .settings-value {
        height: auto;
        padding: 0 1;
    }

    SettingsScreen .settings-value-highlight {
        color: $success;
    }

    SettingsScreen .settings-value-dim {
        color: $text-muted;
    }

    SettingsScreen .settings-value-warn {
        color: $warning;
    }

    SettingsScreen .settings-input {
        margin-top: 1;
        border: solid $primary-lighten-2;
    }

    SettingsScreen .settings-input:focus {
        border: solid $primary;
    }

    SettingsScreen .settings-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
    }

    SettingsScreen .settings-status {
        height: auto;
        color: $success;
        text-align: center;
        margin-top: 1;
    }
    """

    def __init__(self) -> None:
        """Initialize the SettingsScreen."""
        super().__init__()
        self._changed = False

    def compose(self) -> ComposeResult:  # noqa: PLR6301
        """Compose the settings UI.

        Yields:
            Textual widgets for the settings layout.
        """
        config = ModelConfig.load()

        with Vertical():
            yield Static("Settings", classes="settings-title")

            with VerticalScroll(classes="settings-list"):
                # Config file path
                yield Static("Config File", classes="settings-section-header")
                yield Static(
                    f"  {DEFAULT_CONFIG_PATH}",
                    classes="settings-value settings-value-dim",
                )

                # Default model
                yield Static("Default Model", classes="settings-section-header")
                if config.default_model:
                    yield Static(
                        f"  {config.default_model}",
                        classes="settings-value settings-value-highlight",
                    )
                else:
                    yield Static(
                        "  (not set - auto-detected from environment)",
                        classes="settings-value settings-value-dim",
                    )
                yield Input(
                    placeholder="provider:model (e.g., bedrock_converse:anthropic.claude-sonnet-4-6)",
                    id="default-model-input",
                    classes="settings-input",
                )

                # Fallback models
                yield Static("Fallback Models", classes="settings-section-header")
                if config.fallbacks:
                    for i, fb in enumerate(config.fallbacks, 1):
                        yield Static(
                            f"  {i}. {fb}",
                            classes="settings-value settings-value-highlight",
                        )
                else:
                    yield Static(
                        "  (none configured)",
                        classes="settings-value settings-value-dim",
                    )
                yield Input(
                    placeholder="Comma-separated: bedrock_converse:anthropic.claude-sonnet-4-6, ollama:llama3",
                    id="fallbacks-input",
                    classes="settings-input",
                )

                # Recent model
                yield Static("Recent Model", classes="settings-section-header")
                yield Static(
                    f"  {config.recent_model or '(none)'}",
                    classes="settings-value settings-value-dim",
                )

                # Bedrock credential check mode
                yield Static(
                    "Bedrock Credential Check", classes="settings-section-header"
                )
                bedrock_cfg = config.providers.get("bedrock", {})
                bedrock_mode = bedrock_cfg.get("credential_check", "thorough")
                yield Static(
                    f"  Current: {bedrock_mode}",
                    classes="settings-value settings-value-highlight",
                )
                yield Input(
                    placeholder="thorough (default), boto3, or files",
                    id="bedrock-cred-check-input",
                    classes="settings-input",
                )

                # Provider status
                yield Static("Provider Status", classes="settings-section-header")
                yield Static(
                    id="provider-status",
                    classes="settings-section-content",
                )

            yield Static(id="settings-status-bar", classes="settings-status")
            yield Static(
                "Enter = save field | Esc = close",
                classes="settings-help",
            )

    def on_mount(self) -> None:
        """Populate provider status on mount."""
        self._refresh_provider_status()

    def _refresh_provider_status(self) -> None:
        """Check and display credential status for known providers."""
        from bog_agents_cli.model_config import PROVIDER_API_KEY_ENV

        lines: list[str] = []
        # Check well-known providers + bedrock
        providers = sorted(set(PROVIDER_API_KEY_ENV.keys()))
        for provider in providers:
            creds = has_provider_credentials(provider)
            if creds is True:
                symbol = "[green]OK[/green]"
            elif creds is False:
                symbol = "[red]Missing[/red]"
            else:
                symbol = "[yellow]Unknown[/yellow]"
            lines.append(f"  {provider:20s} {symbol}")

        # Check custom providers from config
        config = ModelConfig.load()
        for name in sorted(config.providers.keys()):
            if name not in providers:
                creds = has_provider_credentials(name)
                if creds is True:
                    symbol = "[green]OK[/green]"
                elif creds is False:
                    symbol = "[red]Missing[/red]"
                else:
                    symbol = "[yellow]Unknown[/yellow]"
                lines.append(f"  {name:20s} {symbol}")

        status_widget = self.query_one("#provider-status", Static)
        status_widget.update("\n".join(lines))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter on input fields to save values."""
        status_bar = self.query_one("#settings-status-bar", Static)

        if event.input.id == "default-model-input":
            value = event.value.strip()
            if not value:
                status_bar.update("[yellow]Enter a model spec to set default[/yellow]")
                return
            if save_default_model(value):
                self._changed = True
                status_bar.update(f"[green]Default model set to: {value}[/green]")
                event.input.value = ""
                # Clear config cache and refresh display
                from bog_agents_cli.model_config import clear_caches

                clear_caches()
            else:
                status_bar.update("[red]Failed to save default model[/red]")

        elif event.input.id == "fallbacks-input":
            value = event.value.strip()
            if not value:
                # Clear fallbacks
                if save_fallbacks([]):
                    self._changed = True
                    status_bar.update("[green]Fallbacks cleared[/green]")
                    from bog_agents_cli.model_config import clear_caches

                    clear_caches()
                return
            fallback_list = [f.strip() for f in value.split(",") if f.strip()]
            if save_fallbacks(fallback_list):
                self._changed = True
                status_bar.update(
                    f"[green]Fallbacks set: {', '.join(fallback_list)}[/green]"
                )
                event.input.value = ""
                from bog_agents_cli.model_config import clear_caches

                clear_caches()
            else:
                status_bar.update("[red]Failed to save fallbacks[/red]")

        elif event.input.id == "bedrock-cred-check-input":
            value = event.value.strip().lower()
            if value not in ("thorough", "boto3", "files"):
                status_bar.update(
                    "[red]Invalid mode. Use: thorough, boto3, or files[/red]"
                )
                return
            if _save_bedrock_credential_check(value):
                self._changed = True
                status_bar.update(
                    f"[green]Bedrock credential check set to: {value}[/green]"
                )
                event.input.value = ""
                from bog_agents_cli.model_config import clear_caches

                clear_caches()
                self._refresh_provider_status()
            else:
                status_bar.update(
                    "[red]Failed to save Bedrock credential check mode[/red]"
                )

    def action_cancel(self) -> None:
        """Close the settings screen."""
        self.dismiss(self._changed)
