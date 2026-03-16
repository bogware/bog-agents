"""Benchmarks for CLI startup and import performance.

The CLI defers heavy dependencies (langchain, agent, sessions, etc.) so that
fast-path commands like `--help` and `--version` stay snappy. These tests guard
that invariant: if a top-level import is accidentally re-added, the relevant
test will fail before the regression reaches users.

Run with::

    make benchmark          # uses the `benchmark` pytest marker
    uv run --group test pytest tests/ -m benchmark -v

Each test spawns a **fresh subprocess** so `sys.modules` is clean and measured
times reflect a cold-start import.

If a test fails
~~~~~~~~~~~~~~~~
- **Import isolation failure** — a module in `HEAVY_MODULES` was loaded
    when it shouldn't be. Move the offending import inside the function that
    needs it (see `main.cli_main` for examples of deferred imports).
- **Timing failure** — an import or CLI command exceeded its threshold.
    Profile with `python -X importtime -c "import bog_agents_cli.main"`
    to find the slow import.
- **Deferred-import failure** — a heavy module was *not* loaded when it
    should have been. The deferred import is likely wired incorrectly; check
    that the lazy import path still executes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Modules considered "heavy" — importing any of these at startup defeats
# the purpose of the deferred-import optimisation.
HEAVY_MODULES = frozenset(
    {
        "langchain",
        "langchain.chat_models",
        "langchain_core",
        "langchain_core.messages",
        "langchain_core.language_models",
        "langchain_core.runnables",
        "langchain_openai",
        "langchain_anthropic",
        "bog_agents_cli.agent",
        "bog_agents_cli.sessions",
        "bog_agents_cli.integrations.sandbox_factory",
        "bog_agents_cli.tools",
    }
)


def _run_python(code: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run *code* in a **fresh** Python interpreter and return the result.

    Args:
        code: Python source code to execute.
        timeout: Maximum seconds to wait.

    Returns:
        Completed process with captured stdout/stderr.
    """
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _get_loaded_modules(import_statement: str) -> set[str]:
    """Return the set of ``sys.modules`` keys after executing *import_statement*.

    Runs in a subprocess so the module cache is completely fresh.

    Args:
        import_statement: A valid Python import statement.

    Returns:
        Set of module names present in ``sys.modules``.
    """
    result = _run_python(f"""
        import json, sys
        {import_statement}
        print(json.dumps(sorted(sys.modules.keys())))
    """)
    assert result.returncode == 0, (
        f"Subprocess failed ({result.returncode}):\n{result.stderr}"
    )
    return set(json.loads(result.stdout))


# ---------------------------------------------------------------------------
# Benchmark marker — matches ``make benchmark`` (pytest -m benchmark)
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.benchmark


# ---------------------------------------------------------------------------
# 1. Module-level import isolation
#
# Verify that importing lightweight entry-point modules does NOT pull in the
# heavy langchain / agent / sessions stack.
# ---------------------------------------------------------------------------


class TestImportIsolation:
    """Guard that lightweight entry points don't pull in the heavy stack.

    Without deferred imports, `bog-agents --help` takes 3+ seconds because
    langchain, agent, and sessions all load eagerly. These tests catch any
    accidental top-level import that would re-introduce that latency.
    """

    @pytest.mark.parametrize(
        "import_stmt",
        [
            "from bog_agents_cli.main import parse_args",
            "from bog_agents_cli.main import check_cli_dependencies",
            "import bog_agents_cli.ui",
            "import bog_agents_cli.skills.commands",
        ],
        ids=[
            "main.parse_args",
            "main.check_cli_dependencies",
            "ui",
            "skills.commands",
        ],
    )
    def test_no_heavy_imports_on_lightweight_path(self, import_stmt: str) -> None:
        """Importing lightweight CLI modules must not load heavy deps.

        Args:
            import_stmt: The import statement to test in isolation.
        """
        loaded = _get_loaded_modules(import_stmt)
        leaked = HEAVY_MODULES & loaded
        assert not leaked, (
            f"Heavy modules loaded when running `{import_stmt}`: {sorted(leaked)}"
        )


# ---------------------------------------------------------------------------
# 2. CLI command timing
#
# Measure wall-clock time for common "fast-path" CLI invocations that should
# NOT need the agent/LLM stack.
# ---------------------------------------------------------------------------


class TestCLIStartupTime:
    """End-to-end wall-clock check for commands that should never need the LLM stack.

    Complements `TestImportIsolation` with a user-facing timing gate: even if
    individual imports stay light, a slow composition of many small imports
    could still hurt perceived startup.
    """

    @staticmethod
    def _time_cli_command(args: str) -> float:
        """Return wall-clock seconds to run `python -m bog_agents_cli <args>`.

        Args:
            args: CLI arguments string (e.g., `"--help"`).

        Returns:
            Elapsed wall-clock time in seconds.
        """
        code = f"""
            import time, subprocess, sys
            start = time.perf_counter()
            subprocess.run(
                [sys.executable, "-m", "bog_agents_cli", {args!r}],
                capture_output=True,
                text=True,
                timeout=30,
            )
            elapsed = time.perf_counter() - start
            print(elapsed)
        """
        result = _run_python(code)
        assert result.returncode == 0, f"Timing harness failed:\n{result.stderr}"
        return float(result.stdout.strip())

    def test_help_under_threshold(self) -> None:
        """`bog-agents --help` should complete well under 1 s.

        Catches regressions where a heavy import is accidentally re-added at
        module level.
        """
        elapsed = self._time_cli_command("--help")
        assert elapsed < 1, f"`bog-agents --help` took {elapsed:.2f}s — expected < 1s"

    def test_version_under_threshold(self) -> None:
        """`bog-agents --version` should complete well under 1 s."""
        elapsed = self._time_cli_command("--version")
        assert elapsed < 1, (
            f"`bog-agents --version` took {elapsed:.2f}s — expected < 1s"
        )


# ---------------------------------------------------------------------------
# 3. Import time measurement
#
# Measure absolute import times for key modules so regressions show up
# clearly in `pytest --durations`.
# ---------------------------------------------------------------------------


class TestImportTiming:
    """Catch order-of-magnitude import regressions in key modules.

    The 1 s threshold catches meaningful regressions while
    `pytest --durations` surfaces the numbers for trend analysis.
    """

    @pytest.mark.parametrize(
        "module",
        [
            "bog_agents_cli.main",
            "bog_agents_cli.ui",
            "bog_agents_cli.config",
            "bog_agents_cli.skills.commands",
            "bog_agents_cli.tool_display",
        ],
        ids=[
            "main",
            "ui",
            "config",
            "skills.commands",
            "tool_display",
        ],
    )
    def test_module_import_time(self, module: str) -> None:
        """Import *module* in a fresh process and assert it finishes quickly.

        Args:
            module: Fully qualified module name to import.
        """
        code = f"""
            import time
            start = time.perf_counter()
            import {module}
            elapsed = time.perf_counter() - start
            print(elapsed)
        """
        result = _run_python(code)
        assert result.returncode == 0, f"Failed to import {module}:\n{result.stderr}"
        elapsed = float(result.stdout.strip())
        assert elapsed < 1, f"Importing {module} took {elapsed:.2f}s — expected < 1s"


# ---------------------------------------------------------------------------
# 4. Deferred import paths
#
# Verify that heavy modules ARE loaded once we actually exercise code paths
# that need them. This ensures the deferred imports are wired correctly and
# nothing is silently broken.
# ---------------------------------------------------------------------------


class TestDeferredImportsWork:
    """Verify the heavy modules *do* load when the code paths that need them run.

    Deferred imports can silently break (e.g., a renamed module, a missing
    re-export). Without these tests, the failure would surface only at
    runtime when a user starts a session — not in CI.
    """

    def test_agent_import_loads_langchain(self) -> None:
        """Importing ``bog_agents_cli.agent`` should pull in langchain."""
        loaded = _get_loaded_modules("import bog_agents_cli.agent")
        langchain_modules = {m for m in loaded if m.startswith("langchain")}
        assert langchain_modules, (
            "`bog_agents_cli.agent` should transitively load `langchain` modules"
        )

    def test_sessions_import_available(self) -> None:
        """`bog_agents_cli.sessions` should be importable."""
        result = _run_python("import bog_agents_cli.sessions")
        assert result.returncode == 0, (
            f"Cannot import `bog_agents_cli.sessions`:\n{result.stderr}"
        )

    def test_tool_display_loads_sdk_backends(self) -> None:
        """`tool_display` should load SDK backends."""
        loaded = _get_loaded_modules("import bog_agents_cli.tool_display")
        assert "bog_agents.backends" in loaded, (
            "`tool_display` should import SDK `backends` for `DEFAULT_EXECUTE_TIMEOUT`"
        )
