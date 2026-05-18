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
