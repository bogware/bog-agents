"""First-run UX guard.

If a user installs ``bog-agents-cli``, hits no API key, and runs in a
non-interactive context (``-p``, piped stdin, daemon, CI), the error
must be **actionable** — name the env var to set and where to get help.

Without this test the message can silently regress to a stack trace or
a terse "no model" line. We assert the user-facing string contains the
specific recovery hints we promise in the docs.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bog_agents_cli.config import _run_setup_wizard
from bog_agents_cli.model_config import ModelConfigError


class TestNonInteractiveSetupRefusal:
    def test_non_tty_stdin_raises_with_actionable_message(self) -> None:
        """Non-interactive contexts get an actionable raise instead of a hang.

        The wizard must NOT prompt — it must raise with the env-var
        hints inline so a piped or CI run gets a useful message.
        """
        with patch("sys.stdin.isatty", return_value=False):
            with pytest.raises(ModelConfigError) as exc_info:
                _run_setup_wizard()

        message = str(exc_info.value)
        # The error must name at least one common provider env var
        # AND point at the interactive wizard as the alternative.
        assert "ANTHROPIC_API_KEY" in message, message
        assert "OPENAI_API_KEY" in message, message
        assert "bog-agents" in message.lower(), message
        # No traceback noise should be in the message itself.
        assert "Traceback" not in message

    def test_tty_path_does_not_short_circuit(self) -> None:
        """TTY path proceeds past the early return into the wizard body.

        We force a synthetic exception from inside ``Prompt.ask`` so
        the function aborts on the first real interaction; the key
        assertion is that the abort is NOT the env-var refusal
        message (which would prove the early return fired wrongly).
        """
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch(
                "rich.prompt.Prompt.ask",
                side_effect=KeyboardInterrupt(),
            ),
        ):
            with pytest.raises((KeyboardInterrupt, ModelConfigError)) as exc_info:
                _run_setup_wizard()
            assert "ANTHROPIC_API_KEY" not in str(exc_info.value)
