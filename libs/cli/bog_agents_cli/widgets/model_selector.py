"""Interactive model selector screen for /model command."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical, VerticalScroll
from textual.events import (
    Click,  # noqa: TC002 - needed at runtime for Textual event dispatch
)
from textual.fuzzy import Matcher
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

from bog_agents_cli.config import CharsetMode, Glyphs, _detect_charset_mode, get_glyphs
from bog_agents_cli.model_config import (
    ModelConfig,
    ModelProfileEntry,
    clear_default_model,
    get_available_models,
    get_model_profiles,
    has_provider_credentials,
    refresh_available_models,
    save_default_model,
)
from bog_agents_cli.provider_catalog import (
    ModelDisplay,
    derive_model_display,
)

logger = logging.getLogger(__name__)


# Tracks which Bedrock model specs have been probed in this process. The
# picker uses this to switch the Ctrl+T hint between "press to test" and
# "tested this session" so users see the diagnostic state at a glance
# without having to remember whether they already ran the probe. Cleared
# on process restart — a fresh `bog-agents` invocation gets fresh hints.
_BEDROCK_PROBED_SPECS: set[str] = set()


class ModelOption(Static):
    """A clickable model option in the selector."""

    def __init__(
        self,
        label: str,
        model_spec: str,
        provider: str,
        index: int,
        *,
        has_creds: bool | None = True,
        display: ModelDisplay | None = None,
        search_blob: str = "",
        classes: str = "",
    ) -> None:
        """Initialize a model option.

        Args:
            label: The display text for the option.
            model_spec: The model specification (provider:model format).
            provider: The provider name.
            index: The index of this option in the filtered list.
            has_creds: Whether the provider has valid credentials. True if
                confirmed, False if missing, None if unknown.
            display: Derived display metadata (display_name, family,
                supports_thinking). When None, the picker treats the
                option as legacy with no human-readable label.
            search_blob: Pre-computed lower-cased text the picker fuzzy-
                matches against. Includes the spec, display_name, family,
                and vendor so the user can find a model by any of them.
            classes: CSS classes for styling.
        """
        super().__init__(label, classes=classes)
        self.model_spec = model_spec
        self.provider = provider
        self.index = index
        self.has_creds = has_creds
        self.model_display = display
        self.search_blob = search_blob or model_spec.lower()

    class Clicked(Message):
        """Message sent when a model option is clicked."""

        def __init__(self, model_spec: str, provider: str, index: int) -> None:
            """Initialize the Clicked message.

            Args:
                model_spec: The model specification.
                provider: The provider name.
                index: The index of the clicked option.
            """
            super().__init__()
            self.model_spec = model_spec
            self.provider = provider
            self.index = index

    def on_click(self, event: Click) -> None:
        """Handle click on this option.

        Args:
            event: The click event.
        """
        event.stop()
        self.post_message(self.Clicked(self.model_spec, self.provider, self.index))


class ProviderHeader(Static):
    """A provider-section header in the model picker.

    Subclasses ``Static`` only to attach the provider id as an attribute,
    so the filter pass can hide headers whose section has no visible
    options without parsing Rich markup.
    """

    def __init__(self, *args: Any, provider: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.provider = provider


class ModelSelectorScreen(ModalScreen[tuple[str, str] | None]):
    """Full-screen modal for model selection.

    Displays available models grouped by provider with keyboard navigation
    and search filtering. Current model is highlighted.

    Returns (model_spec, provider) tuple on selection, or None on cancel.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False, priority=True),
        Binding("k", "move_up", "Up", show=False, priority=True),
        Binding("down", "move_down", "Down", show=False, priority=True),
        Binding("j", "move_down", "Down", show=False, priority=True),
        Binding("tab", "tab_complete", "Tab complete", show=False, priority=True),
        Binding("pageup", "page_up", "Page up", show=False, priority=True),
        Binding("pagedown", "page_down", "Page down", show=False, priority=True),
        Binding("enter", "select", "Select", show=False, priority=True),
        Binding("ctrl+s", "set_default", "Set default", show=False, priority=True),
        Binding("ctrl+r", "refresh_catalog", "Refresh", show=False, priority=True),
        Binding("ctrl+t", "smoketest", "Smoketest", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    CSS = """
    ModelSelectorScreen {
        align: center middle;
    }

    ModelSelectorScreen > Vertical {
        width: 80;
        max-width: 90%;
        height: 80%;
        background: $surface-darken-1;
        border: round $primary;
        padding: 1 2;
    }

    ModelSelectorScreen .model-selector-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    ModelSelectorScreen #model-filter {
        margin-bottom: 1;
        border: round $surface-lighten-1;
    }

    ModelSelectorScreen #model-filter:focus {
        border: round $primary;
    }

    ModelSelectorScreen .model-list {
        height: 1fr;
        min-height: 5;
        scrollbar-gutter: stable;
        background: $surface;
    }

    ModelSelectorScreen #model-options {
        height: auto;
    }

    ModelSelectorScreen .model-provider-header {
        color: $primary;
        margin-top: 1;
    }

    ModelSelectorScreen #model-options > .model-provider-header:first-child {
        margin-top: 0;
    }

    ModelSelectorScreen .model-option {
        height: 1;
        padding: 0 1;
    }

    ModelSelectorScreen .model-option:hover {
        background: $surface-lighten-1;
    }

    ModelSelectorScreen .model-option-selected {
        background: $primary;
        color: #08131c;
        text-style: bold;
    }

    ModelSelectorScreen .model-option-selected:hover {
        background: $primary-lighten-1;
    }

    ModelSelectorScreen .model-option-current {
        text-style: italic;
    }

    ModelSelectorScreen .model-selector-help {
        height: 1;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
    }

    ModelSelectorScreen .model-detail-footer {
        height: 4;
        padding: 0 2;
        border-top: solid $primary-lighten-2;
    }
    """

    def __init__(
        self,
        current_model: str | None = None,
        current_provider: str | None = None,
        cli_profile_override: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the ModelSelectorScreen.

        Args:
            current_model: The currently active model name (to highlight).
            current_provider: The provider of the current model.
            cli_profile_override: Extra profile fields from `--profile-override`.

                Merged on top of upstream + config.toml profiles so that CLI
                overrides appear with `*` markers in the detail footer.
        """
        super().__init__()
        self._current_model = current_model
        self._current_provider = current_provider
        self._cli_profile_override = cli_profile_override

        # Build list from dynamically discovered models (falls back to defaults).
        self._all_models: list[tuple[str, str]] = []
        for provider, models in get_available_models().items():
            for model in models:
                model_spec = f"{provider}:{model}"
                self._all_models.append((model_spec, provider))

        # Derive display metadata once per model so the filter loop can
        # search across spec + display name + family without re-deriving
        # on every keystroke.
        self._displays: dict[str, ModelDisplay] = {
            spec: derive_model_display(provider, spec.split(":", 1)[1])
            for spec, provider in self._all_models
        }

        # The "search blob" is the lower-cased text the fuzzy matcher
        # compares against. Pre-computing it once avoids string ops per
        # keystroke per model.
        self._search_blobs: dict[str, str] = {
            spec: " ".join(
                (
                    spec.lower(),
                    self._displays[spec].display_name.lower(),
                    self._displays[spec].family,
                    self._displays[spec].vendor,
                    self._displays[spec].provider_display.lower(),
                )
            )
            for spec, _ in self._all_models
        }

        self._filtered_models: list[tuple[str, str]] = list(self._all_models)
        self._selected_index = self._find_current_model_index()
        self._options_container: Container | None = None
        self._option_widgets: list[ModelOption] = []
        # `_visible_widgets` is kept in sync with `_filtered_models` so
        # `_move_selection` can index by score-order position rather than
        # build-order. Initially equals `_option_widgets` (all visible).
        self._visible_widgets: list[ModelOption] = []
        self._filter_text = ""
        self._smoketest_running = False
        self._current_spec: str | None = None
        if current_model and current_provider:
            self._current_spec = f"{current_provider}:{current_model}"

        # Pre-resolve credentials once per provider so neither the
        # initial build nor selection-move recomputes them.
        providers_in_list = {p for _, p in self._all_models}
        self._creds: dict[str, bool | None] = {
            p: has_provider_credentials(p) for p in providers_in_list
        }

        config = ModelConfig.load()
        self._default_spec: str | None = config.default_model
        self._profiles = get_model_profiles(cli_override=cli_profile_override)

    def _find_current_model_index(self) -> int:
        """Find the index of the current model in the filtered list.

        Returns:
            Index of the current model, or 0 if not found.
        """
        if not self._current_model or not self._current_provider:
            return 0

        current_spec = f"{self._current_provider}:{self._current_model}"
        for i, (model_spec, _) in enumerate(self._filtered_models):
            if model_spec == current_spec:
                return i
        return 0

    def compose(self) -> ComposeResult:
        """Compose the screen layout.

        Yields:
            Widgets for the model selector UI.
        """
        glyphs = get_glyphs()

        with Vertical():
            # Title with current model in provider:model format
            if self._current_model and self._current_provider:
                current_spec = f"{self._current_provider}:{self._current_model}"
                title = f"Select Model (current: {current_spec})"
            elif self._current_model:
                title = f"Select Model (current: {self._current_model})"
            else:
                title = "Select Model"
            yield Static(title, classes="model-selector-title")

            # Search input
            yield Input(
                placeholder="Type to filter or enter provider:model...",
                id="model-filter",
            )

            # Scrollable model list
            with VerticalScroll(classes="model-list"):
                self._options_container = Container(id="model-options")
                yield self._options_container

            # Model detail footer
            yield Static("", classes="model-detail-footer", id="model-detail-footer")

            # Help text
            help_text = (
                f"{glyphs.arrow_up}/{glyphs.arrow_down} navigate"
                f" {glyphs.bullet} Enter select"
                f" {glyphs.bullet} Ctrl+S default"
                f" {glyphs.bullet} Ctrl+T test"
                f" {glyphs.bullet} Ctrl+R refresh"
                f" {glyphs.bullet} Esc cancel"
            )
            yield Static(help_text, classes="model-selector-help")

    async def on_mount(self) -> None:
        """Set up the screen on mount."""
        if _detect_charset_mode() == CharsetMode.ASCII:
            container = self.query_one(Vertical)
            container.styles.border = ("ascii", "green")

        await self._build_widget_set()
        self._update_footer()

        # Focus the filter input
        filter_input = self.query_one("#model-filter", Input)
        filter_input.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter models as user types.

        Runs synchronously because ``_apply_filter`` is O(N) over the
        in-memory widget set and only flips ``.display`` on each option
        — there is no DOM rebuild and no Rich re-render of unchanged
        widgets, so a per-keystroke pass is cheap. Earlier versions
        debounced this via ``set_timer`` to coalesce the expensive full
        rebuild; removing the rebuild also removed the need to defer.
        Running synchronously matters for the test pilot — assertions
        on ``_selected_index`` after ``pilot.press`` see the filtered
        state without waiting for a timer tick (CI flake on Python 3.12
        where the default ``pilot.pause`` window beat the timer).
        """
        self._filter_text = event.value
        self._apply_filter()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key when filter input is focused.

        Args:
            event: The input submitted event.
        """
        event.stop()
        self.action_select()

    def on_model_option_clicked(self, event: ModelOption.Clicked) -> None:
        """Handle click on a model option.

        Args:
            event: The click event with model info.
        """
        self._selected_index = event.index
        self.dismiss((event.model_spec, event.provider))

    def _update_filtered_list(self) -> None:
        """Update the filtered models based on search text using fuzzy matching.

        Results are sorted by match score (best first). Search blobs are
        pre-computed in ``__init__`` so this only does the score loop.
        """
        query = self._filter_text.strip()
        if not query:
            self._filtered_models = list(self._all_models)
            self._selected_index = self._find_current_model_index()
            return

        tokens = query.split()

        try:
            matchers = [Matcher(token, case_sensitive=False) for token in tokens]
            scored: list[tuple[float, str, str]] = []
            for spec, provider in self._all_models:
                blob = self._search_blobs.get(spec, spec.lower())
                scores = [m.match(blob) for m in matchers]
                if all(s > 0 for s in scores):
                    scored.append((min(scores), spec, provider))
        except Exception:
            # graceful fallback if Matcher fails on edge-case input
            logger.warning(
                "Fuzzy matcher failed for query %r, falling back to full list",
                query,
                exc_info=True,
            )
            self._filtered_models = list(self._all_models)
            self._selected_index = self._find_current_model_index()
            return

        self._filtered_models = [
            (spec, provider) for score, spec, provider in sorted(scored, reverse=True)
        ]
        self._selected_index = 0

    async def _build_widget_set(self) -> None:
        """Build the full set of ``ModelOption`` widgets once.

        Called on initial mount and on Ctrl+R refresh. Subsequent filter
        changes flip ``.display`` on existing widgets instead of rebuilding
        — see :meth:`_apply_filter`.
        """
        if not self._options_container:
            return

        await self._options_container.remove_children()
        self._option_widgets = []

        if not self._all_models:
            no_models = Static("[dim]No models discovered[/dim]")
            await self._options_container.mount(no_models)
            self._update_footer()
            return

        # Group by provider, preserving insertion order so models from the
        # same provider cluster together in the visual list.
        by_provider: dict[str, list[tuple[str, str]]] = {}
        for model_spec, provider in self._all_models:
            by_provider.setdefault(provider, []).append((model_spec, provider))

        glyphs = get_glyphs()
        flat_index = 0
        current_spec = self._current_spec

        # Batch-mount all widgets in a single mount call to avoid
        # individual DOM mutations per widget.
        all_widgets: list[Static] = []

        for provider, model_entries in by_provider.items():
            has_creds = self._creds.get(provider)
            if has_creds is True:
                cred_indicator = glyphs.checkmark
            elif has_creds is False:
                cred_indicator = f"{glyphs.warning} missing credentials"
            else:
                cred_indicator = f"{glyphs.question} credentials unknown"
            provider_display = (
                self._displays[model_entries[0][0]].provider_display
                if model_entries
                else provider
            )
            all_widgets.append(
                ProviderHeader(
                    f"[bold]{provider_display}[/bold] [dim]({provider}) "
                    f"{cred_indicator}[/dim]",
                    classes="model-provider-header",
                    provider=provider,
                )
            )

            for model_spec, _prov in model_entries:
                is_current = model_spec == current_spec
                is_selected = flat_index == self._selected_index

                classes = "model-option"
                if is_selected:
                    classes += " model-option-selected"
                if is_current:
                    classes += " model-option-current"

                label = self._format_option_label(
                    model_spec,
                    selected=is_selected,
                    current=is_current,
                    has_creds=has_creds,
                    is_default=model_spec == self._default_spec,
                    status=self._get_model_status(model_spec),
                    display=self._displays.get(model_spec),
                )
                widget = ModelOption(
                    label=label,
                    model_spec=model_spec,
                    provider=provider,
                    index=flat_index,
                    has_creds=has_creds,
                    display=self._displays.get(model_spec),
                    search_blob=self._search_blobs.get(model_spec, ""),
                    classes=classes,
                )
                all_widgets.append(widget)
                self._option_widgets.append(widget)

                flat_index += 1

        await self._options_container.mount(*all_widgets)

        # Reset filtered list to "all" so _apply_filter doesn't think
        # nothing matches before the first keystroke.
        self._filtered_models = list(self._all_models)
        self._visible_widgets = list(self._option_widgets)
        self._apply_filter()

    def _apply_filter(self) -> None:
        """Filter the existing widget set in-place based on ``_filter_text``.

        Hot path: runs on every keystroke (after the debounce). Performance
        depends on this being O(N) over widgets with no DOM mutation
        beyond toggling ``.display`` on each option.
        """
        # Score / order the candidates.
        self._update_filtered_list()
        matched_specs = {spec for spec, _ in self._filtered_models}

        spec_to_widget: dict[str, ModelOption] = {
            w.model_spec: w for w in self._option_widgets
        }

        # Rebuild _visible_widgets in score order so navigation keys
        # follow the user's filter ranking.
        self._visible_widgets = [
            spec_to_widget[spec]
            for spec, _ in self._filtered_models
            if spec in spec_to_widget
        ]

        # Toggle widget visibility. Track per-provider counts so we can
        # hide headers whose group has no visible options.
        visible_options_per_provider: dict[str, int] = {}
        for widget in self._option_widgets:
            visible = widget.model_spec in matched_specs
            widget.display = visible
            if visible:
                visible_options_per_provider[widget.provider] = (
                    visible_options_per_provider.get(widget.provider, 0) + 1
                )

        # Hide provider headers whose group has no visible options.
        if self._options_container is not None:
            for header in self._options_container.query(ProviderHeader):
                header.display = (
                    visible_options_per_provider.get(header.provider, 0) > 0
                )

        if not self._filtered_models:
            self._selected_index = 0
            self._update_footer()
            return

        # _update_filtered_list set _selected_index to 0 when a filter is
        # active. Clamp just in case the caller poked it.
        if self._selected_index >= len(self._filtered_models):
            self._selected_index = 0
        target_spec = self._filtered_models[self._selected_index][0]

        # Re-paint visible widgets' selection styling.
        for w in self._visible_widgets:
            is_selected = w.model_spec == target_spec
            is_current = w.model_spec == self._current_spec
            if is_selected:
                w.add_class("model-option-selected")
            else:
                w.remove_class("model-option-selected")
            w.update(
                self._format_option_label(
                    w.model_spec,
                    selected=is_selected,
                    current=is_current,
                    has_creds=w.has_creds,
                    is_default=w.model_spec == self._default_spec,
                    status=self._get_model_status(w.model_spec),
                    display=w.model_display,
                )
            )

        if target_spec in spec_to_widget:
            try:
                spec_to_widget[target_spec].scroll_visible(animate=False)
            except Exception:
                logger.debug("scroll_visible failed", exc_info=True)

        self._update_footer()

    @staticmethod
    def _format_option_label(
        model_spec: str,
        *,
        selected: bool,
        current: bool,
        has_creds: bool | None,
        is_default: bool = False,
        status: str | None = None,
        display: ModelDisplay | None = None,
    ) -> str:
        """Build the display label for a model option.

        Args:
            model_spec: The `provider:model` string.
            selected: Whether this option is currently highlighted.
            current: Whether this is the active model.
            has_creds: Credential status (True/False/None).
            is_default: Whether this is the configured default model.
            status: Model status from profile (e.g., `'deprecated'`,
                `'beta'`, `'alpha'`). `'deprecated'` renders in red;
                other non-None values render in yellow.
            display: Derived display metadata (display_name + thinking
                support). When provided, the label leads with the
                human-readable name and dims the raw spec.

        Returns:
            Rich-markup label string.
        """
        glyphs = get_glyphs()
        cursor = f"{glyphs.cursor} " if selected else "  "

        # Primary text: human-readable display name when available,
        # otherwise fall back to the raw spec.
        primary = display.display_name if display else model_spec
        if not has_creds:
            primary_text = f"[yellow]{primary}[/yellow]"
        elif is_default:
            primary_text = f"[cyan]{primary}[/cyan]"
        else:
            primary_text = primary

        # Always include the raw spec so power users can read the API id.
        spec_hint = f"  [dim]{model_spec}[/dim]" if display else ""

        suffix = " [dim](current)[/dim]" if current else ""
        default_suffix = " [cyan](default)[/cyan]" if is_default else ""
        if status == "deprecated":
            status_suffix = " [red](deprecated)[/red]"
        elif status:
            status_suffix = f" [yellow]({status})[/yellow]"
        else:
            status_suffix = ""
        thinking_suffix = (
            " [magenta]✨ thinking[/magenta]"
            if display and display.supports_thinking
            else ""
        )
        return (
            f"{cursor}{primary_text}{spec_hint}{suffix}"
            f"{default_suffix}{status_suffix}{thinking_suffix}"
        )

    @staticmethod
    def _format_footer(
        profile_entry: ModelProfileEntry | None,
        glyphs: Glyphs,
    ) -> str:
        """Build the detail footer text for the highlighted model.

        Args:
            profile_entry: Profile data with override tracking, or None.
            glyphs: Glyph set for display characters.

        Returns:
            Rich-markup string for the 4-line footer.
        """
        from bog_agents_cli.textual_adapter import format_token_count

        if profile_entry is None or not profile_entry["profile"]:
            return "[dim]Model profile not available :([/dim]\n\n\n"

        profile = profile_entry["profile"]
        overridden = profile_entry["overridden_keys"]

        def _mark(key: str, text: str) -> str:
            return f"[yellow]*{text}[/yellow]" if key in overridden else text

        def _format_token(key: str, suffix: str) -> str | None:
            """Format a token-count profile key, falling back to the raw value.

            Returns:
                Formatted string with override marker, or None if key absent.
            """
            val = profile.get(key)
            if val is None:
                return None
            try:
                text = f"{format_token_count(int(val))} {suffix}"
            except (ValueError, TypeError, OverflowError):
                text = f"{val} {suffix}"
            return _mark(key, text)

        def _format_flags(keys: list[tuple[str, str]]) -> list[str]:
            """Render boolean profile keys as green (on) or dim (off) labels.

            Returns:
                List of Rich-markup strings for present keys.
            """
            parts: list[str] = []
            for key, label in keys:
                if key in profile:
                    styled = (
                        f"[green]{label}[/green]"
                        if profile[key]
                        else f"[dim]{label}[/dim]"
                    )
                    parts.append(_mark(key, styled))
            return parts

        # Line 1: Context window
        token_keys = [("max_input_tokens", "in"), ("max_output_tokens", "out")]
        ctx_parts = [p for k, s in token_keys if (p := _format_token(k, s)) is not None]
        sep = f" {glyphs.bullet} "
        line1 = f"Context: {sep.join(ctx_parts)}" if ctx_parts else ""

        # Line 2: Input modalities
        modality_keys = [
            ("text_inputs", "text"),
            ("image_inputs", "image"),
            ("audio_inputs", "audio"),
            ("pdf_inputs", "pdf"),
            ("video_inputs", "video"),
        ]
        modality_parts = _format_flags(modality_keys)
        line2 = f"Input: {' '.join(modality_parts)}" if modality_parts else ""

        # Line 3: Capabilities
        capability_keys = [
            ("reasoning_output", "reasoning"),
            ("tool_calling", "tool calling"),
            ("structured_output", "structured output"),
        ]
        cap_parts = _format_flags(capability_keys)
        line3 = f"Capabilities: {' '.join(cap_parts)}" if cap_parts else ""

        # Line 4: Override notice
        displayed_keys = {k for k, _ in token_keys + modality_keys + capability_keys}
        has_visible_override = bool(overridden & displayed_keys)
        line4 = (
            "[dim][yellow]*[/yellow] = override[/dim]" if has_visible_override else ""
        )

        return f"{line1}\n{line2}\n{line3}\n{line4}"

    def _get_model_status(self, model_spec: str) -> str | None:
        """Look up the status field for a model from its profile.

        Args:
            model_spec: The `provider:model` string.

        Returns:
            Status string (e.g., `'deprecated'`) if the model has a profile
            with a `status` key, otherwise None.
        """
        entry = self._profiles.get(model_spec)
        if entry is None:
            return None
        profile = entry.get("profile")
        if not profile:
            return None
        return profile.get("status")

    def _update_footer(self) -> None:
        """Update the detail footer for the currently highlighted model."""
        footer = self.query_one("#model-detail-footer", Static)
        if not self._filtered_models:
            footer.update("[dim]No model selected[/dim]")
            return
        index = min(self._selected_index, len(self._filtered_models) - 1)
        spec, _ = self._filtered_models[index]
        entry = self._profiles.get(spec)
        try:
            text = self._format_footer(entry, get_glyphs())
        except Exception:  # Resilient footer rendering
            logger.debug("Failed to format footer for %s", spec, exc_info=True)
            text = "[dim]Could not load profile details[/dim]\n\n\n"
        # Bedrock-specific hint: surface the deep probe affordance so the
        # 6-step diagnostic ([Package, Credentials, Region, Identity,
        # ListModels, Inference]) is one keystroke away when the user has
        # a Bedrock model highlighted. The Ctrl+T binding is also in the
        # bottom help bar; this line makes it obvious where it leads.
        if spec.startswith(("bedrock:", "bedrock_converse:")):
            if spec in _BEDROCK_PROBED_SPECS:
                hint = (
                    "[bold cyan]Bedrock[/bold cyan] [dim]·[/dim]"
                    " [green]✓[/green] tested this session"
                    " [dim](Ctrl+T to re-run)[/dim]"
                )
            else:
                hint = (
                    "[bold cyan]Bedrock[/bold cyan] [dim]·[/dim] press"
                    " [bold]Ctrl+T[/bold] to run the 6-step probe"
                    " [dim](creds → region → identity → access → inference)[/dim]"
                )
            text = f"{text}\n{hint}"
        footer.update(text)

    def _move_selection(self, delta: int) -> None:
        """Move selection by delta, updating only the affected widgets.

        Args:
            delta: Number of positions to move (-1 for up, +1 for down).
        """
        if not self._filtered_models or not self._visible_widgets:
            return

        count = len(self._visible_widgets)
        old_index = self._selected_index % count
        new_index = (old_index + delta) % count
        self._selected_index = new_index

        # Update the previously selected widget
        old_widget = self._visible_widgets[old_index]
        old_widget.remove_class("model-option-selected")
        old_widget.update(
            self._format_option_label(
                old_widget.model_spec,
                selected=False,
                current=old_widget.model_spec == self._current_spec,
                has_creds=old_widget.has_creds,
                is_default=old_widget.model_spec == self._default_spec,
                status=self._get_model_status(old_widget.model_spec),
                display=old_widget.model_display,
            )
        )

        # Update the newly selected widget
        new_widget = self._visible_widgets[new_index]
        new_widget.add_class("model-option-selected")
        new_widget.update(
            self._format_option_label(
                new_widget.model_spec,
                selected=True,
                current=new_widget.model_spec == self._current_spec,
                has_creds=new_widget.has_creds,
                is_default=new_widget.model_spec == self._default_spec,
                status=self._get_model_status(new_widget.model_spec),
                display=new_widget.model_display,
            )
        )

        # Scroll the selected item into view
        if new_index == 0:
            scroll_container = self.query_one(".model-list", VerticalScroll)
            scroll_container.scroll_home(animate=False)
        else:
            new_widget.scroll_visible()

        self._update_footer()

    def action_move_up(self) -> None:
        """Move selection up."""
        self._move_selection(-1)

    def action_move_down(self) -> None:
        """Move selection down."""
        self._move_selection(1)

    def action_tab_complete(self) -> None:
        """Replace search text with the currently selected model spec."""
        if not self._filtered_models:
            return
        model_spec, _ = self._filtered_models[self._selected_index]
        filter_input = self.query_one("#model-filter", Input)
        filter_input.value = model_spec
        filter_input.cursor_position = len(model_spec)

    def _visible_page_size(self) -> int:
        """Return the number of model options that fit in one visual page.

        Returns:
            Number of model options per page, at least 1.
        """
        default_page_size = 10
        try:
            scroll = self.query_one(".model-list", VerticalScroll)
            height = scroll.size.height
        except Exception:  # Fallback to default page size on any widget query error
            return default_page_size
        if height <= 0:
            return default_page_size

        total_models = len(self._filtered_models)
        if total_models == 0:
            return default_page_size

        # Each provider header = 1 row + margin-top: 1 (first has margin 0)
        num_headers = len(self.query(".model-provider-header"))
        header_rows = max(0, num_headers * 2 - 1) if num_headers else 0
        total_rows = total_models + header_rows
        return max(1, int(height * total_models / total_rows))

    def action_page_up(self) -> None:
        """Move selection up by one visible page."""
        if not self._filtered_models:
            return
        page = self._visible_page_size()
        target = max(0, self._selected_index - page)
        delta = target - self._selected_index
        if delta != 0:
            self._move_selection(delta)

    def action_page_down(self) -> None:
        """Move selection down by one visible page."""
        if not self._filtered_models:
            return
        count = len(self._filtered_models)
        page = self._visible_page_size()
        target = min(count - 1, self._selected_index + page)
        delta = target - self._selected_index
        if delta != 0:
            self._move_selection(delta)

    def action_select(self) -> None:
        """Select the current model and persist it as the default.

        Enter doubles as "set default" — picking a model from the selector
        almost always means the user wants future sessions to use it. The
        explicit Ctrl+S binding is preserved for the case where the user
        wants to change the persisted default without dismissing the
        selector. Use Esc to cancel without picking.
        """
        # If there are filtered results, always select the highlighted model
        if self._filtered_models:
            model_spec, provider = self._filtered_models[self._selected_index]
            ModelSelectorScreen._persist_as_default(model_spec)
            self.dismiss((model_spec, provider))
            return

        # No matches - check if user typed a custom provider:model spec
        filter_input = self.query_one("#model-filter", Input)
        custom_input = filter_input.value.strip()

        if custom_input and ":" in custom_input:
            provider = custom_input.split(":", 1)[0]
            ModelSelectorScreen._persist_as_default(custom_input)
            self.dismiss((custom_input, provider))
        elif custom_input:
            ModelSelectorScreen._persist_as_default(custom_input)
            self.dismiss((custom_input, ""))

    @staticmethod
    def _persist_as_default(model_spec: str) -> None:
        """Save `model_spec` as the user's default model, best-effort.

        Failures are logged but not surfaced — selection should not be
        blocked by a persistence problem (e.g., read-only config dir).
        The next session simply falls back to the previous default.
        """
        try:
            save_default_model(model_spec)
        except Exception:
            logger.warning(
                "Could not persist default model %s", model_spec, exc_info=True
            )

    async def action_set_default(self) -> None:
        """Toggle the highlighted model as the default.

        If the highlighted model is already the default, clears it.
        Otherwise sets it as the new default.
        """
        import asyncio

        if not self._filtered_models or not self._option_widgets:
            return

        model_spec, _provider = self._filtered_models[self._selected_index]
        help_widget = self.query_one(".model-selector-help", Static)

        if model_spec == self._default_spec:
            # Already default — clear it
            if await asyncio.to_thread(clear_default_model):
                self._default_spec = None
                self.call_after_refresh(self._apply_filter)
                help_widget.update("[bold]Default cleared[/bold]")
                self.set_timer(3.0, self._restore_help_text)
            else:
                help_widget.update("[bold red]Failed to clear default[/bold red]")
                self.set_timer(3.0, self._restore_help_text)
        elif await asyncio.to_thread(save_default_model, model_spec):
            self._default_spec = model_spec
            self.call_after_refresh(self._update_display)
            help_widget.update(f"[bold]Default set to {model_spec}[/bold]")
            self.set_timer(3.0, self._restore_help_text)
        else:
            help_widget.update("[bold red]Failed to save default[/bold red]")
            self.set_timer(3.0, self._restore_help_text)

    def _restore_help_text(self) -> None:
        """Restore the default help text after a temporary message."""
        glyphs = get_glyphs()
        help_text = (
            f"{glyphs.arrow_up}/{glyphs.arrow_down} navigate"
            f" {glyphs.bullet} Enter select"
            f" {glyphs.bullet} Ctrl+S default"
            f" {glyphs.bullet} Ctrl+T test"
            f" {glyphs.bullet} Ctrl+R refresh"
            f" {glyphs.bullet} Esc cancel"
        )
        help_widget = self.query_one(".model-selector-help", Static)
        help_widget.update(help_text)

    async def action_refresh_catalog(self) -> None:
        """Reload the model catalog from upstream profiles + cache.

        Clears the in-memory caches, re-derives the list, rebuilds the
        widget set in place. Useful after installing a new provider
        package or after the user adds a custom model to config.toml.
        """
        import asyncio

        help_widget = self.query_one(".model-selector-help", Static)
        help_widget.update("[bold]Refreshing model catalog…[/bold]")
        try:
            await asyncio.to_thread(refresh_available_models)
            # Rebuild internal model list + display metadata.
            self._all_models = []
            for provider, models in get_available_models().items():
                for model in models:
                    spec = f"{provider}:{model}"
                    self._all_models.append((spec, provider))
            self._displays = {
                spec: derive_model_display(provider, spec.split(":", 1)[1])
                for spec, provider in self._all_models
            }
            self._search_blobs = {
                spec: " ".join(
                    (
                        spec.lower(),
                        self._displays[spec].display_name.lower(),
                        self._displays[spec].family,
                        self._displays[spec].vendor,
                        self._displays[spec].provider_display.lower(),
                    )
                )
                for spec, _ in self._all_models
            }
            providers_in_list = {p for _, p in self._all_models}
            self._creds = {p: has_provider_credentials(p) for p in providers_in_list}
            self._profiles = get_model_profiles(cli_override=self._cli_profile_override)
            self._selected_index = self._find_current_model_index()
            await self._build_widget_set()
            help_widget.update(
                f"[bold]Catalog refreshed — {len(self._all_models)} models[/bold]"
            )
        except Exception:
            logger.warning("Failed to refresh model catalog", exc_info=True)
            help_widget.update("[bold red]Refresh failed — see logs[/bold red]")
        self.set_timer(3.0, self._restore_help_text)

    async def action_smoketest(self) -> None:
        """Run a quick connectivity test against the highlighted model.

        Opens a ``SmoketestResult`` modal with credentials / inference /
        thinking steps. Disabled while another smoketest is in flight to
        avoid stacking duplicate calls.
        """
        import asyncio

        if self._smoketest_running:
            return
        if not self._filtered_models:
            return
        model_spec, _provider = self._filtered_models[self._selected_index]

        help_widget = self.query_one(".model-selector-help", Static)
        help_widget.update(f"[bold]Smoketest {model_spec}…[/bold] (Ctrl+T to re-run)")
        self._smoketest_running = True
        try:
            # Lazy import — keeps the cold-start cost of opening the picker low.
            from bog_agents_cli.smoketest import smoketest_model

            result = await asyncio.to_thread(smoketest_model, model_spec)
            help_widget.update(result.summary_markup())
            # Record that this Bedrock spec has been probed in this
            # session so the next focus on the same model shows the
            # ✓-tested hint instead of the press-to-test hint.
            if model_spec.startswith(("bedrock:", "bedrock_converse:")):
                _BEDROCK_PROBED_SPECS.add(model_spec)
                self._update_footer()
        except Exception as exc:
            logger.warning("Smoketest failed for %s", model_spec, exc_info=True)
            help_widget.update(f"[bold red]Smoketest crashed: {exc}[/bold red]")
        finally:
            self._smoketest_running = False
        self.set_timer(8.0, self._restore_help_text)

    def action_cancel(self) -> None:
        """Cancel the selection."""
        self.dismiss(None)
