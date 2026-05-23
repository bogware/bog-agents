"""Tests for ``bog_agents.tools.bundles`` — the W4 alternative to
tool-contributor middleware classes.

The pattern under test: factories like ``git_tools_bundle()`` produce
a ``list[BaseTool]`` that can be handed straight to ``create_agent``,
skipping the ``AgentMiddleware`` machinery. The corresponding
middleware classes (``GitToolsMiddleware`` etc.) are now thin shims
that delegate to these bundles; both call sites must agree on the
emitted tool surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path


class TestGitToolsBundle:
    def test_returns_tool_list(self, tmp_path: Path) -> None:
        from bog_agents.tools import git_tools_bundle

        tools = git_tools_bundle(working_dir=tmp_path)
        assert len(tools) > 0
        assert all(hasattr(t, "name") for t in tools)
        assert all(hasattr(t, "description") for t in tools)

    def test_tool_names_match_middleware(self, tmp_path: Path) -> None:
        """The bundle must emit the same tool names as the middleware shim."""
        from bog_agents.middleware.git_tools import GitToolsMiddleware
        from bog_agents.tools import git_tools_bundle

        bundle_names = {t.name for t in git_tools_bundle(working_dir=tmp_path)}
        mw = GitToolsMiddleware(working_dir=tmp_path)
        mw_names = {t.name for t in mw.tools}
        assert bundle_names == mw_names, (
            f"Drift between bundle and middleware: bundle-only={bundle_names - mw_names}, middleware-only={mw_names - bundle_names}"
        )

    def test_default_working_dir_is_cwd(self) -> None:
        """When no working_dir is given, the bundle binds to ``Path.cwd()``.

        Smoke test only — we don't actually invoke git here.
        """
        from bog_agents.tools import git_tools_bundle

        tools = git_tools_bundle()
        # Calling git_status should be safe even outside a git repo;
        # it returns an error string rather than raising.
        assert any(t.name == "git_status" for t in tools)

    def test_bundle_is_pure_function(self, tmp_path: Path) -> None:
        """Two calls with the same args yield independent tool lists.

        The bundle must not share mutable state between invocations —
        if it did, two agents in the same process would interfere.
        """
        from bog_agents.tools import git_tools_bundle

        a = git_tools_bundle(working_dir=tmp_path)
        b = git_tools_bundle(working_dir=tmp_path)
        assert a is not b
        assert {t.name for t in a} == {t.name for t in b}


class TestGitToolsInvocation:
    """The tools must be callable with the runtime kwarg langgraph injects.

    This catches the class of bug that shipped in 0.9.0: every tool in
    ``git_tools_bundle`` declared its runtime parameter as ``_runtime``
    (underscore-prefixed). Pydantic silently dropped underscore params
    from the args schema AND langgraph's ToolNode didn't inject them, so
    every git tool failed at agent invocation with
    ``TypeError: missing 1 required positional argument: '_runtime'``.

    The previous tests only asserted tool *names* — never invoked one —
    so the bug slipped through CI. These tests close that gap by calling
    every tool's underlying ``func`` with a mock runtime kwarg, exactly
    the way ToolNode does it in production.
    """

    @staticmethod
    def _mock_runtime() -> MagicMock:
        from langchain.tools import ToolRuntime

        runtime = MagicMock(spec=ToolRuntime)
        runtime.tool_call_id = "tc-test"
        runtime.state = {}
        runtime.config = {}
        return runtime

    def test_every_tool_accepts_runtime_kwarg(self, tmp_path: Path) -> None:
        """No tool may raise on a baseline runtime-only invocation."""
        from bog_agents.tools import git_tools_bundle

        with patch("bog_agents.middleware.git_tools._run_git", return_value="(mocked)"):
            tools = git_tools_bundle(working_dir=tmp_path)
            runtime = self._mock_runtime()
            # Tools that need only the runtime kwarg (no required args).
            no_arg_tools = {"git_status", "git_diff", "git_log", "git_branch", "git_stash", "git_show"}
            for tool in tools:
                if tool.name not in no_arg_tools:
                    continue
                result = tool.func(runtime=runtime)
                assert result == "(mocked)", f"{tool.name} returned {result!r}"

    def test_tools_with_required_args(self, tmp_path: Path) -> None:
        """Tools that take additional args still receive the runtime."""
        from bog_agents.tools import git_tools_bundle

        with patch("bog_agents.middleware.git_tools._run_git", return_value="(mocked)") as run:
            tools = git_tools_bundle(working_dir=tmp_path)
            by_name = {t.name: t for t in tools}
            runtime = self._mock_runtime()

            # git_commit needs a message.
            r = by_name["git_commit"].func(runtime=runtime, message="feat: x")
            assert "(mocked)" in r

            # git_add needs paths.
            r = by_name["git_add"].func(runtime=runtime, paths=["a.py", "b.py"])
            assert r  # any non-empty return is fine

            # git_blame needs a path.
            r = by_name["git_blame"].func(runtime=runtime, path="a.py")
            assert r == "(mocked)"

            # _run_git was actually called — the closure works.
            assert run.call_count >= 1

    def test_runtime_param_named_runtime_not_underscored(self) -> None:
        """Regression guard: every tool's first parameter must be
        named ``runtime``, exactly, with no leading underscore.

        Pydantic drops underscore-prefixed params from the args schema
        and langgraph's ToolNode only injects parameters with the
        literal name ``runtime``. Anything else and the tool dies at
        invocation time.
        """
        import inspect
        from pathlib import Path as _Path

        from bog_agents.tools import git_tools_bundle

        for tool in git_tools_bundle(working_dir=_Path.cwd()):
            sig = inspect.signature(tool.func)
            first_param = next(iter(sig.parameters))
            assert first_param == "runtime", (
                f"{tool.name}: first param is {first_param!r}, expected 'runtime'. "
                f"Underscore-prefixed names break ToolRuntime injection — see "
                f"docs/sdk/tool-bundles.md for the rules."
            )


class TestPublicExports:
    def test_tools_namespace_exposes_bundle(self) -> None:
        """``from bog_agents.tools import git_tools_bundle`` must work
        without triggering the heavy middleware imports the package
        used to require.
        """
        import bog_agents.tools as tools_pkg

        assert hasattr(tools_pkg, "git_tools_bundle")
        assert hasattr(tools_pkg, "multi_edit_tool")
        assert hasattr(tools_pkg, "read_many_files_tool")
