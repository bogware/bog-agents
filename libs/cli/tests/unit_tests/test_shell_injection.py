"""Regression tests for the QA-executor shell-injection defence.

The audit (docs/PRINCIPAL_REVIEW.md §2.1) flagged that the QA executor
hands a substituted command string to ``subprocess.Popen(..., shell=True)``
without quoting the substituted variable values. A plan whose vars come
from an attacker-controlled source (env vars, prompts, recorded sessions)
could inject ``; rm -rf $HOME ;`` and escape its argument.

The fix is in two places:

1. ``bog_agents_cli.vars.VarBundle.substitute(..., shell_quote=True)``
   wraps each interpolated value with the platform-appropriate quoter
   (``shlex.quote`` on POSIX; cmd.exe-aware quoter on Windows).
2. ``bog_agents_cli.qa.executor._run_shell_step`` passes
   ``shell_quote=True`` so the rendered command is safe to hand to a
   shell.

These tests pin both halves so a future refactor cannot silently
regress the defence.
"""

from __future__ import annotations

import shlex
import sys

import pytest

from bog_agents_cli.vars import VarBundle, _shell_quote_for_platform


class TestShellQuoteHelper:
    """``_shell_quote_for_platform`` neutralises injection on this platform."""

    @pytest.mark.parametrize(
        "value",
        [
            "; rm -rf $HOME ;",
            "$(whoami)",
            "`whoami`",
            "&& curl evil.example.com",
            "| cat /etc/passwd",
            "> /tmp/pwn",
            "$IFS",
            "'; echo pwned; '",
            '"; echo pwned; "',
            "$(curl evil)",
        ],
    )
    def test_metachars_cannot_escape(self, value: str) -> None:
        """Every common shell metacharacter survives quoting as inert text."""
        quoted = _shell_quote_for_platform(value)
        # The quoted form must be a single shell token. On POSIX,
        # ``shlex.split(quoted)`` returns one element. On Windows the
        # quoter is cmd.exe-aware and shlex won't roundtrip, but POSIX
        # is the meaningful case for CI.
        if sys.platform != "win32":
            tokens = shlex.split(quoted, posix=True)
            assert tokens == [value], (
                f"shlex split {quoted!r} into {tokens!r}, expected [{value!r}]"
            )
        else:
            # On Windows we can only assert that the metacharacters are
            # carat-escaped where they appear outside the quoted body.
            for meta in ("^&", "^|", "^<", "^>", "^(", "^)", "^%"):
                if meta[1] in value:
                    assert meta in quoted, (
                        f"Windows quoter did not carat-escape {meta[1]!r} "
                        f"in {value!r} → {quoted!r}"
                    )

    def test_empty_string_quoted(self) -> None:
        """An empty string still produces a non-empty quoted form."""
        assert _shell_quote_for_platform("") != ""

    def test_plain_string_is_not_mangled(self) -> None:
        """A safe value still survives — quoter is a strict superset."""
        # The result must round-trip back to the original on POSIX. On
        # Windows we only assert the original is contained in the result.
        if sys.platform != "win32":
            assert shlex.split(_shell_quote_for_platform("safe-value")) == [
                "safe-value"
            ]
        else:
            assert "safe-value" in _shell_quote_for_platform("safe-value")


class TestVarBundleShellQuote:
    """``VarBundle.substitute(template, shell_quote=True)`` is injection-safe."""

    def _bundle(self, **values: str) -> VarBundle:
        bundle = VarBundle.from_dict(
            {name: {"type": "string", "default": val} for name, val in values.items()}
        )
        for name, val in values.items():
            bundle.set(name, val)
        return bundle

    def test_injection_value_is_quoted(self) -> None:
        bundle = self._bundle(target="; rm -rf $HOME ;")
        rendered = bundle.substitute("echo ${target}", shell_quote=True)
        # Result must split as exactly ``echo`` + the injected payload —
        # NOT echo plus three separate commands.
        if sys.platform != "win32":
            tokens = shlex.split(rendered)
            assert tokens == ["echo", "; rm -rf $HOME ;"]
        else:
            # On Windows the carat escapes survive cmd.exe parsing; the
            # rendered command must contain ``^&`` somewhere if the
            # input had ``&`` — which "; rm -rf $HOME ;" does not, so
            # the rendered just needs to contain the original payload.
            assert "rm -rf" in rendered

    def test_backtick_command_substitution_is_neutered(self) -> None:
        bundle = self._bundle(target="`whoami`")
        rendered = bundle.substitute("echo ${target}", shell_quote=True)
        if sys.platform != "win32":
            tokens = shlex.split(rendered)
            assert tokens == ["echo", "`whoami`"]

    def test_dollar_paren_substitution_is_neutered(self) -> None:
        bundle = self._bundle(target="$(whoami)")
        rendered = bundle.substitute("echo ${target}", shell_quote=True)
        if sys.platform != "win32":
            tokens = shlex.split(rendered)
            assert tokens == ["echo", "$(whoami)"]

    def test_quote_off_preserves_legacy_behaviour(self) -> None:
        """With ``shell_quote=False`` (default) values pass through verbatim.

        This is the existing contract for HTTP / agent / MCP renderers
        where the value is never re-parsed by a shell. Pinning this
        means the security fix doesn't accidentally break those callers.
        """
        bundle = self._bundle(target="; rm -rf $HOME ;")
        rendered = bundle.substitute("echo ${target}")
        assert rendered == "echo ; rm -rf $HOME ;"

    def test_dict_substitute_propagates_quote_flag(self) -> None:
        bundle = self._bundle(target="`whoami`")
        rendered = bundle.substitute(
            {"cmd": "echo ${target}", "tag": "${target}"}, shell_quote=True
        )
        if sys.platform != "win32":
            assert shlex.split(rendered["cmd"]) == ["echo", "`whoami`"]
            # bare tag must also be quoted
            assert shlex.split(rendered["tag"]) == ["`whoami`"]

    def test_list_substitute_propagates_quote_flag(self) -> None:
        bundle = self._bundle(target="; pwned ;")
        rendered = bundle.substitute(
            ["echo ${target}", "label-${target}"], shell_quote=True
        )
        if sys.platform != "win32":
            assert shlex.split(rendered[0]) == ["echo", "; pwned ;"]


class TestExecutorWiresShellQuoteOn:
    """The executor must call ``substitute(..., shell_quote=True)`` for shell.

    A code-grep regression: someone could delete the ``shell_quote=True``
    kwarg in ``_run_shell_step`` and not break any other test. This test
    pins the wiring by inspecting the source.
    """

    def test_executor_calls_substitute_with_shell_quote(self) -> None:
        import inspect

        from bog_agents_cli.qa import executor

        src = inspect.getsource(executor._run_shell_step)
        assert "shell_quote=True" in src, (
            "qa.executor._run_shell_step must call "
            "bundle.substitute(step.run, shell_quote=True). The shell-"
            "injection defence depends on this. See "
            "docs/PRINCIPAL_REVIEW.md §2.1 and "
            "tests/unit_tests/test_shell_injection.py for context."
        )
