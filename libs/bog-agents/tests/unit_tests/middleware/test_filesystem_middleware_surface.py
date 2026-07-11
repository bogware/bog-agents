"""Tests for the `FilesystemMiddleware` surface added in the deepagents-parity wave.

Covers, in order:

1. The stale-`FileData` fix — `middleware.filesystem.FileData` must be the canonical
   v2 backend type (`content: str`), not the pre-v2 `content: list[str]` duplicate.
2. The tool registry (`FsToolName` / `_FS_TOOL_ORDER` / `_build_fs_tools_section`).
3. The `delete` tool, including its recursive-delete permission gate.
4. Constructor kwargs that previously raised `TypeError`
   (`tools=`, `human_message_token_limit_before_evict=`, `_permissions=`,
   `custom_tool_descriptions` as a `Mapping`).
5. The security wiring — `ls`/`glob`/`grep` results are filtered through the
   permission rules on BOTH the sync and async paths.
6. Human-message eviction, and the Street Sweeper invariant it must respect.
7. Prompt templating from the visible tool set + resolved artifacts root.
"""

from __future__ import annotations

import typing
from typing import Any, ClassVar

import pytest
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.types import Command

from bog_agents.backends import CompositeBackend, StateBackend, StoreBackend
from bog_agents.backends.protocol import FileData as BackendFileData
from bog_agents.backends.utils import create_file_data
from bog_agents.middleware.filesystem import (
    _ALL_FS_TOOL_NAMES,
    _FS_TOOL_ORDER,
    DEFAULT_ARTIFACTS_ROOT,
    FILESYSTEM_SYSTEM_PROMPT,
    GREP_TOOL_DESCRIPTION,
    GREP_TOOL_DESCRIPTION_WITHOUT_EXECUTE,
    NUM_CHARS_PER_TOKEN,
    TOO_LARGE_HUMAN_MSG,
    FileData,
    FilesystemMiddleware,
    FilesystemState,
    FsToolName,
    _build_fs_tools_section,
)
from bog_agents.middleware.permissions import FilesystemPermission


def _runtime(state: FilesystemState, tool_call_id: str = "call_1") -> ToolRuntime:
    return ToolRuntime(state=state, context=None, tool_call_id=tool_call_id, store=None, stream_writer=lambda _: None, config={})


def _state(files: dict[str, Any] | None = None) -> FilesystemState:
    return FilesystemState(messages=[], files=files or {})


def _tool(middleware: FilesystemMiddleware, name: str) -> Any:
    return next(tool for tool in middleware.tools if tool.name == name)


# ---------------------------------------------------------------------------
# 1. Stale FileData
# ---------------------------------------------------------------------------


class TestCanonicalFileData:
    def test_filesystem_file_data_is_the_canonical_backend_type(self) -> None:
        """The module-local duplicate is gone; the re-export is the backend's v2 type."""
        assert FileData is BackendFileData

    def test_file_data_content_is_str_not_list(self) -> None:
        """The stale shape declared `content: list[str]`, which is what produced the
        73 live PydanticSerializationUnexpectedValue warnings."""
        assert typing.get_type_hints(FileData)["content"] is str

    def test_state_backed_write_round_trips_v2_file_data(self) -> None:
        middleware = FilesystemMiddleware()
        state = _state()
        result = _tool(middleware, "write_file").invoke(
            {"file_path": "/a.txt", "content": "hello", "runtime": _runtime(state)},
        )
        assert isinstance(result, Command)
        written = result.update["files"]["/a.txt"]
        assert isinstance(written["content"], str)


# ---------------------------------------------------------------------------
# 2. Tool registry
# ---------------------------------------------------------------------------


class TestToolRegistry:
    def test_fs_tool_name_is_a_superset_of_upstream(self) -> None:
        """An upstream-typed tool list must still type-check against ours, so
        `FsToolName` carries upstream's 7 + `execute` plus bog's two extras."""
        names = set(typing.get_args(FsToolName))
        upstream = {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "delete", "execute"}
        assert upstream <= names
        assert {"multi_edit_file", "read_many_files"} <= names

    def test_all_fs_tool_names_matches_the_constructed_tools(self) -> None:
        middleware = FilesystemMiddleware()
        assert {tool.name for tool in middleware.tools} == set(_ALL_FS_TOOL_NAMES)

    def test_every_ordered_tool_has_a_description_line(self) -> None:
        header, descriptions = _build_fs_tools_section(set(_FS_TOOL_ORDER))
        for name in _FS_TOOL_ORDER:
            assert f"`{name}`" in header
            assert f"- {name}:" in descriptions

    def test_build_fs_tools_section_reflects_only_visible_tools(self) -> None:
        header, descriptions = _build_fs_tools_section({"ls", "read_file"})
        assert header == "`ls`, `read_file`"
        assert "delete" not in descriptions
        assert "grep" not in descriptions

    def test_build_fs_tools_section_ignores_execute(self) -> None:
        """`execute` is prompted by its own section, never the filesystem list."""
        header, _ = _build_fs_tools_section({"ls", "read_file", "execute"})
        assert "execute" not in header


# ---------------------------------------------------------------------------
# 3. The delete tool
# ---------------------------------------------------------------------------


class TestDeleteTool:
    def test_delete_tool_is_registered_after_edit_file(self) -> None:
        names = [tool.name for tool in FilesystemMiddleware().tools]
        assert names.index("delete") == names.index("edit_file") + 1

    def test_delete_removes_the_file(self) -> None:
        middleware = FilesystemMiddleware()
        state = _state({"/a.txt": create_file_data("hello")})
        result = _tool(middleware, "delete").invoke({"file_path": "/a.txt", "runtime": _runtime(state)})
        assert isinstance(result, Command)
        assert result.update["files"]["/a.txt"] is None

    async def test_adelete_removes_the_file(self) -> None:
        middleware = FilesystemMiddleware()
        state = _state({"/a.txt": create_file_data("hello")})
        result = await _tool(middleware, "delete").ainvoke({"file_path": "/a.txt", "runtime": _runtime(state)})
        assert isinstance(result, Command)
        assert result.update["files"]["/a.txt"] is None

    def test_delete_is_recursive(self) -> None:
        middleware = FilesystemMiddleware()
        state = _state({"/dir/a.txt": create_file_data("a"), "/dir/sub/b.txt": create_file_data("b"), "/keep.txt": create_file_data("k")})
        result = _tool(middleware, "delete").invoke({"file_path": "/dir", "runtime": _runtime(state)})
        assert isinstance(result, Command)
        files_update = result.update["files"]
        assert files_update["/dir/a.txt"] is None
        assert files_update["/dir/sub/b.txt"] is None
        assert "/keep.txt" not in files_update

    def test_delete_rejects_a_relative_path(self) -> None:
        middleware = FilesystemMiddleware()
        result = _tool(middleware, "delete").invoke({"file_path": "relative.txt", "runtime": _runtime(_state())})
        assert isinstance(result, str)
        assert result.startswith("Error:")

    def test_delete_is_write_class_so_hitl_and_safetools_gate_it(self) -> None:
        from bog_agents.middleware.filesystem import _WRITE_CLASS_TOOL_NAMES

        assert "delete" in _WRITE_CLASS_TOOL_NAMES

    def test_delete_is_excluded_from_result_eviction(self) -> None:
        from bog_agents.middleware.filesystem import TOOLS_EXCLUDED_FROM_EVICTION

        assert "delete" in TOOLS_EXCLUDED_FROM_EVICTION


class TestDeletePermissionGate:
    """A recursive delete must not be able to nuke a denied subtree.

    Before this wave agents could only `rm` via `execute`, which bypasses the
    permission rules entirely.
    """

    @staticmethod
    def _middleware() -> FilesystemMiddleware:
        return FilesystemMiddleware(_permissions=[FilesystemPermission(operations=["read", "write"], paths=["/secrets/**"], mode="deny")])

    def test_delete_of_the_denied_path_itself_is_blocked(self) -> None:
        state = _state({"/secrets/prod.env": create_file_data("API_KEY=sk-live")})
        result = _tool(self._middleware(), "delete").invoke({"file_path": "/secrets/prod.env", "runtime": _runtime(state)})
        assert isinstance(result, str)
        assert "permission denied" in result
        assert "/secrets/**" in result

    def test_delete_of_root_cannot_nuke_the_denied_subtree(self) -> None:
        """`delete("/")` does not literally match `/secrets/**`, so an exact-path
        check would let it through and destroy the denied subtree."""
        state = _state({"/secrets/prod.env": create_file_data("API_KEY=sk-live"), "/work/a.txt": create_file_data("a")})
        result = _tool(self._middleware(), "delete").invoke({"file_path": "/", "runtime": _runtime(state)})
        assert isinstance(result, str)
        assert "permission denied" in result

    async def test_adelete_of_root_cannot_nuke_the_denied_subtree(self) -> None:
        state = _state({"/secrets/prod.env": create_file_data("API_KEY=sk-live")})
        result = await _tool(self._middleware(), "delete").ainvoke({"file_path": "/", "runtime": _runtime(state)})
        assert isinstance(result, str)
        assert "permission denied" in result

    def test_delete_of_an_allowed_sibling_still_works(self) -> None:
        state = _state({"/work/a.txt": create_file_data("a")})
        result = _tool(self._middleware(), "delete").invoke({"file_path": "/work/a.txt", "runtime": _runtime(state)})
        assert isinstance(result, Command)


# ---------------------------------------------------------------------------
# 4. Constructor drift
# ---------------------------------------------------------------------------


class TestConstructorKwargs:
    """Each of these raised `TypeError` before this wave."""

    def test_tools_allowlist_restricts_the_visible_set(self) -> None:
        middleware = FilesystemMiddleware(tools=["ls", "read_file", "grep"])
        assert {tool.name for tool in middleware.tools} == {"ls", "read_file", "grep"}

    def test_tools_all_is_equivalent_to_the_default(self) -> None:
        assert {t.name for t in FilesystemMiddleware(tools="all").tools} == {t.name for t in FilesystemMiddleware().tools}

    def test_tools_none_defaults_to_every_tool(self) -> None:
        assert {tool.name for tool in FilesystemMiddleware(tools=None).tools} == set(_ALL_FS_TOOL_NAMES)

    def test_tools_list_must_include_read_file(self) -> None:
        with pytest.raises(ValueError, match="read_file must be included"):
            FilesystemMiddleware(tools=["ls"])

    def test_human_message_token_limit_before_evict_defaults_to_50k(self) -> None:
        assert FilesystemMiddleware()._human_message_token_limit_before_evict == 50000

    def test_human_message_token_limit_before_evict_is_settable(self) -> None:
        assert FilesystemMiddleware(human_message_token_limit_before_evict=None)._human_message_token_limit_before_evict is None

    def test_permissions_are_accepted_and_default_to_empty(self) -> None:
        rule = FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="deny")
        assert FilesystemMiddleware(_permissions=[rule])._permissions == [rule]
        assert FilesystemMiddleware()._permissions == []

    def test_custom_tool_descriptions_accepts_any_mapping(self) -> None:
        from types import MappingProxyType

        middleware = FilesystemMiddleware(custom_tool_descriptions=MappingProxyType({"ls": "Charmander"}))
        assert _tool(middleware, "ls").description == "Charmander"

    def test_custom_tool_descriptions_covers_delete(self) -> None:
        middleware = FilesystemMiddleware(custom_tool_descriptions={"delete": "Squirtle"})
        assert _tool(middleware, "delete").description == "Squirtle"


# ---------------------------------------------------------------------------
# 5. Security wiring: bulk-tool result filtering
# ---------------------------------------------------------------------------


class TestBulkResultFiltering:
    """The exploit: a *pathless* bulk call has no path argument for the tool-call
    boundary check to match a rule against, so `grep(pattern="API_KEY")` used to
    return the matching lines straight out of a denied `/secrets`.
    """

    DENY_SECRETS: ClassVar[list[FilesystemPermission]] = [FilesystemPermission(operations=["read", "write"], paths=["/secrets/**"], mode="deny")]

    @staticmethod
    def _files() -> dict[str, Any]:
        return {
            "/secrets/prod.env": create_file_data("API_KEY=sk-live-deadbeef"),
            "/work/notes.txt": create_file_data("API_KEY placeholder"),
        }

    def test_pathless_grep_does_not_leak_denied_file_contents(self) -> None:
        middleware = FilesystemMiddleware(_permissions=self.DENY_SECRETS)
        result = _tool(middleware, "grep").invoke(
            {"pattern": "API_KEY", "output_mode": "content", "runtime": _runtime(_state(self._files()))},
        )
        assert "sk-live-deadbeef" not in result
        assert "/secrets/prod.env" not in result
        assert "/work/notes.txt" in result

    async def test_pathless_agrep_does_not_leak_denied_file_contents(self) -> None:
        middleware = FilesystemMiddleware(_permissions=self.DENY_SECRETS)
        result = await _tool(middleware, "grep").ainvoke(
            {"pattern": "API_KEY", "output_mode": "content", "runtime": _runtime(_state(self._files()))},
        )
        assert "sk-live-deadbeef" not in result
        assert "/secrets/prod.env" not in result

    def test_ls_hides_denied_entries(self) -> None:
        """`ls("/secrets")` is not itself denied — the literal path `/secrets` does not
        match the pattern `/secrets/**` — so the listing's *entries* must be filtered."""
        middleware = FilesystemMiddleware(_permissions=self.DENY_SECRETS)
        result = _tool(middleware, "ls").invoke({"path": "/secrets", "runtime": _runtime(_state(self._files()))})
        assert "/secrets/prod.env" not in result

    async def test_als_hides_denied_entries(self) -> None:
        middleware = FilesystemMiddleware(_permissions=self.DENY_SECRETS)
        result = await _tool(middleware, "ls").ainvoke({"path": "/secrets", "runtime": _runtime(_state(self._files()))})
        assert "/secrets/prod.env" not in result

    def test_ls_keeps_allowed_entries(self) -> None:
        middleware = FilesystemMiddleware(_permissions=self.DENY_SECRETS)
        result = _tool(middleware, "ls").invoke({"path": "/work", "runtime": _runtime(_state(self._files()))})
        assert "/work/notes.txt" in result

    def test_glob_hides_denied_matches(self) -> None:
        middleware = FilesystemMiddleware(_permissions=self.DENY_SECRETS)
        result = _tool(middleware, "glob").invoke({"pattern": "**/*", "runtime": _runtime(_state(self._files()))})
        assert "/secrets/prod.env" not in result
        assert "/work/notes.txt" in result

    async def test_aglob_hides_denied_matches(self) -> None:
        middleware = FilesystemMiddleware(_permissions=self.DENY_SECRETS)
        result = await _tool(middleware, "glob").ainvoke({"pattern": "**/*", "runtime": _runtime(_state(self._files()))})
        assert "/secrets/prod.env" not in result

    def test_interrupt_mode_entries_pass_through_unfiltered(self) -> None:
        """`interrupt` means ask-on-access, not hide — and the HITL gate already
        fired before the result existed."""
        middleware = FilesystemMiddleware(
            _permissions=[FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="interrupt")],
        )
        result = _tool(middleware, "glob").invoke({"pattern": "**/*", "runtime": _runtime(_state(self._files()))})
        assert "/secrets/prod.env" in result

    def test_no_permissions_configured_is_a_no_op(self) -> None:
        result = _tool(FilesystemMiddleware(), "glob").invoke({"pattern": "**/*", "runtime": _runtime(_state(self._files()))})
        assert "/secrets/prod.env" in result


class TestExactToolDenyChecks:
    """Defense in depth under `FilesystemPermissionsMiddleware`, which is wired
    separately and may be absent."""

    DENY: ClassVar[list[FilesystemPermission]] = [FilesystemPermission(operations=["read", "write"], paths=["/secrets/**"], mode="deny")]

    def test_read_file_is_denied(self) -> None:
        middleware = FilesystemMiddleware(_permissions=self.DENY)
        state = _state({"/secrets/prod.env": create_file_data("API_KEY=sk-live")})
        result = _tool(middleware, "read_file").invoke({"file_path": "/secrets/prod.env", "runtime": _runtime(state)})
        assert result == "Error: permission denied for read on /secrets/prod.env"

    async def test_aread_file_is_denied(self) -> None:
        middleware = FilesystemMiddleware(_permissions=self.DENY)
        state = _state({"/secrets/prod.env": create_file_data("API_KEY=sk-live")})
        result = await _tool(middleware, "read_file").ainvoke({"file_path": "/secrets/prod.env", "runtime": _runtime(state)})
        assert result == "Error: permission denied for read on /secrets/prod.env"

    def test_write_file_is_denied(self) -> None:
        middleware = FilesystemMiddleware(_permissions=self.DENY)
        result = _tool(middleware, "write_file").invoke({"file_path": "/secrets/x.env", "content": "x", "runtime": _runtime(_state())})
        assert result == "Error: permission denied for write on /secrets/x.env"

    async def test_awrite_file_is_denied(self) -> None:
        middleware = FilesystemMiddleware(_permissions=self.DENY)
        result = await _tool(middleware, "write_file").ainvoke({"file_path": "/secrets/x.env", "content": "x", "runtime": _runtime(_state())})
        assert result == "Error: permission denied for write on /secrets/x.env"

    def test_edit_file_is_denied(self) -> None:
        middleware = FilesystemMiddleware(_permissions=self.DENY)
        state = _state({"/secrets/prod.env": create_file_data("A=1")})
        result = _tool(middleware, "edit_file").invoke(
            {"file_path": "/secrets/prod.env", "old_string": "A=1", "new_string": "A=2", "runtime": _runtime(state)},
        )
        assert result == "Error: permission denied for write on /secrets/prod.env"

    async def test_aedit_file_is_denied(self) -> None:
        middleware = FilesystemMiddleware(_permissions=self.DENY)
        state = _state({"/secrets/prod.env": create_file_data("A=1")})
        result = await _tool(middleware, "edit_file").ainvoke(
            {"file_path": "/secrets/prod.env", "old_string": "A=1", "new_string": "A=2", "runtime": _runtime(state)},
        )
        assert result == "Error: permission denied for write on /secrets/prod.env"

    def test_grep_scoped_to_a_denied_directory_is_denied(self) -> None:
        """A rule that names the directory itself blocks the call at the argument."""
        middleware = FilesystemMiddleware(
            _permissions=[FilesystemPermission(operations=["read"], paths=["/secrets", "/secrets/**"], mode="deny")],
        )
        state = _state({"/secrets/prod.env": create_file_data("API_KEY=sk-live")})
        result = _tool(middleware, "grep").invoke({"pattern": "API_KEY", "path": "/secrets", "runtime": _runtime(state)})
        assert result == "Error: permission denied for read on /secrets"

    def test_grep_scoped_to_a_denied_subtree_leaks_nothing(self) -> None:
        """Even when only `/secrets/**` is denied — so `path="/secrets"` itself does not
        match and the call proceeds — the *results* must still be redacted."""
        middleware = FilesystemMiddleware(_permissions=self.DENY)
        state = _state({"/secrets/prod.env": create_file_data("API_KEY=sk-live")})
        result = _tool(middleware, "grep").invoke(
            {"pattern": "API_KEY", "path": "/secrets", "output_mode": "content", "runtime": _runtime(state)},
        )
        assert "sk-live" not in result
        assert "/secrets/prod.env" not in result

    def test_allowed_paths_are_untouched(self) -> None:
        middleware = FilesystemMiddleware(_permissions=self.DENY)
        result = _tool(middleware, "write_file").invoke({"file_path": "/work/a.txt", "content": "x", "runtime": _runtime(_state())})
        assert isinstance(result, Command)


# ---------------------------------------------------------------------------
# 6. Human-message eviction
# ---------------------------------------------------------------------------


class _Request:
    """Minimal `ModelRequest` stand-in for the eviction helpers."""

    def __init__(self, messages: list[Any], runtime: ToolRuntime) -> None:
        self.messages = messages
        self.runtime = runtime


class TestHumanMessageEviction:
    LIMIT = 10  # tokens -> 40 chars

    @staticmethod
    def _huge(chars: int) -> str:
        return "\n".join("payload line" for _ in range(chars // 12 + 1))

    def _middleware(self) -> FilesystemMiddleware:
        return FilesystemMiddleware(human_message_token_limit_before_evict=self.LIMIT)

    def test_oversized_human_message_is_offloaded_and_previewed(self) -> None:
        middleware = self._middleware()
        state = _state()
        big = self._huge(500)
        request = _Request([HumanMessage(content=big, id="h1")], _runtime(state))

        evicted = middleware._evict_human_messages(request)  # type: ignore[arg-type]

        assert evicted is not None
        assert big not in evicted[0].content
        assert "Message content too large" in evicted[0].content
        assert "/conversation_history/" in evicted[0].content

    async def test_async_path_offloads_too(self) -> None:
        middleware = self._middleware()
        big = self._huge(500)
        request = _Request([HumanMessage(content=big, id="h1")], _runtime(_state()))

        evicted = await middleware._aevict_human_messages(request)  # type: ignore[arg-type]

        assert evicted is not None
        assert big not in evicted[0].content
        assert TOO_LARGE_HUMAN_MSG.split("{", 1)[0].strip() in evicted[0].content

    def test_small_messages_are_left_alone(self) -> None:
        middleware = self._middleware()
        request = _Request([HumanMessage(content="hi", id="h1")], _runtime(_state()))
        assert middleware._evict_human_messages(request) is None  # type: ignore[arg-type]

    def test_eviction_can_be_disabled(self) -> None:
        middleware = FilesystemMiddleware(human_message_token_limit_before_evict=None)
        request = _Request([HumanMessage(content=self._huge(500), id="h1")], _runtime(_state()))
        assert middleware._evict_human_messages(request) is None  # type: ignore[arg-type]

    def test_default_limit_lets_a_normal_prompt_through(self) -> None:
        middleware = FilesystemMiddleware()
        request = _Request([HumanMessage(content="x" * (NUM_CHARS_PER_TOKEN * 1000), id="h1")], _runtime(_state()))
        assert middleware._evict_human_messages(request) is None  # type: ignore[arg-type]

    def test_street_sweeper_invariant_count_and_order_are_unchanged(self) -> None:
        """The sweep may rewrite message TEXT only. Changing the count or order
        desynchronizes SummarizationMiddleware's cutoff indices and
        AnthropicPromptCachingMiddleware's stable prefix."""
        middleware = self._middleware()
        original = [
            SystemMessage(content="sys", id="s1"),
            HumanMessage(content=self._huge(500), id="h1"),
            AIMessage(content="ok", id="a1"),
            HumanMessage(content="small follow-up", id="h2"),
        ]
        request = _Request(list(original), _runtime(_state()))

        evicted = middleware._evict_human_messages(request)  # type: ignore[arg-type]

        assert evicted is not None
        assert len(evicted) == len(original)
        assert [type(m) for m in evicted] == [type(m) for m in original]
        assert [m.id for m in evicted] == [m.id for m in original]
        # Only the oversized message's text changed.
        assert evicted[0].content == original[0].content
        assert evicted[1].content != original[1].content
        assert evicted[2].content == original[2].content
        assert evicted[3].content == original[3].content

    def test_canonical_history_in_state_is_not_mutated(self) -> None:
        """Eviction is a view transformation: the request is reshaped, state is not."""
        middleware = self._middleware()
        big = self._huge(500)
        message = HumanMessage(content=big, id="h1")
        request = _Request([message], _runtime(_state()))

        middleware._evict_human_messages(request)  # type: ignore[arg-type]

        assert message.content == big

    def test_offload_path_is_stable_across_calls(self) -> None:
        """A content digest (not a random uuid) keys the offload, so re-rendering the
        same message on a later turn rewrites one file instead of accumulating one
        artifact per model call."""
        middleware = self._middleware()
        big = self._huge(500)
        first = middleware._evict_human_messages(_Request([HumanMessage(content=big, id="h1")], _runtime(_state())))  # type: ignore[arg-type]
        second = middleware._evict_human_messages(_Request([HumanMessage(content=big, id="h1")], _runtime(_state())))  # type: ignore[arg-type]
        assert first is not None
        assert second is not None
        assert first[0].content == second[0].content


# ---------------------------------------------------------------------------
# 7. Prompt templating
# ---------------------------------------------------------------------------


class _PromptRequest:
    """Minimal `ModelRequest` stand-in for `_prepare_request`."""

    def __init__(self, tools: list[Any], runtime: ToolRuntime) -> None:
        self.tools = tools
        self.runtime = runtime
        self.system_message = SystemMessage(content="base")
        self.messages: list[Any] = []

    def override(self, **kwargs: Any) -> _PromptRequest:
        for key, value in kwargs.items():
            setattr(self, key, value)
        return self


class TestPromptTemplating:
    def test_module_constant_stays_pre_rendered_for_existing_importers(self) -> None:
        assert "## Filesystem Tools" in FILESYSTEM_SYSTEM_PROMPT
        assert DEFAULT_ARTIFACTS_ROOT in FILESYSTEM_SYSTEM_PROMPT
        assert "{tool_header}" not in FILESYSTEM_SYSTEM_PROMPT

    def test_module_constant_lists_every_default_tool(self) -> None:
        for name in _FS_TOOL_ORDER:
            assert f"`{name}`" in FILESYSTEM_SYSTEM_PROMPT

    def test_prompt_names_the_delete_tool(self) -> None:
        assert "delete: delete a file or directory (recursively) from the filesystem" in FILESYSTEM_SYSTEM_PROMPT

    def test_prompt_renders_from_the_visible_tool_set(self) -> None:
        middleware = FilesystemMiddleware(tools=["ls", "read_file"])
        request = _PromptRequest(list(middleware.tools), _runtime(_state()))

        middleware._prepare_request(request)  # type: ignore[arg-type]

        rendered = str(request.system_message.content)
        assert "## Filesystem Tools `ls`, `read_file`" in rendered
        assert "- grep: search for text within files" not in rendered
        assert "- delete:" not in rendered

    def test_prompt_uses_the_resolved_artifacts_root_not_a_hardcoded_one(self) -> None:
        """`artifacts_root` used to be ignored: the prompt always claimed
        `/large_tool_results` even when the backend offloaded elsewhere."""
        middleware = FilesystemMiddleware(artifacts_root="/artifacts")
        request = _PromptRequest(list(middleware.tools), _runtime(_state()))

        middleware._prepare_request(request)  # type: ignore[arg-type]

        rendered = str(request.system_message.content)
        assert "/artifacts/<tool_call_id>" in rendered
        assert "/large_tool_results" not in rendered

    def test_composite_artifacts_root_reaches_the_prompt(self) -> None:
        state = _state()
        backend = CompositeBackend(
            default=StateBackend(_runtime(state)),
            routes={"/memories/": StoreBackend(_runtime(state))},
            artifacts_root="/composite_artifacts",
        )
        middleware = FilesystemMiddleware(backend=backend)
        request = _PromptRequest(list(middleware.tools), _runtime(state))

        middleware._prepare_request(request)  # type: ignore[arg-type]

        assert "/composite_artifacts/<tool_call_id>" in str(request.system_message.content)

    def test_grep_description_drops_the_rg_fallback_without_execute(self) -> None:
        """A StateBackend cannot execute, so pointing the model at `rg` would be a lie."""
        middleware = FilesystemMiddleware()
        request = _PromptRequest(list(middleware.tools), _runtime(_state()))

        middleware._prepare_request(request)  # type: ignore[arg-type]

        grep = next(tool for tool in request.tools if tool.name == "grep")
        assert grep.description == GREP_TOOL_DESCRIPTION_WITHOUT_EXECUTE
        assert "rg" not in grep.description

    def test_grep_description_keeps_the_rg_fallback_with_execute(self) -> None:
        assert "rg '<regex>'" in GREP_TOOL_DESCRIPTION

    def test_grep_description_is_hardened_against_regex_misuse(self) -> None:
        for text in (GREP_TOOL_DESCRIPTION, GREP_TOOL_DESCRIPTION_WITHOUT_EXECUTE):
            assert text.startswith("Search for a LITERAL text pattern across files (NOT regex).")
            assert "There is no\n  `|` alternation" in text
            assert "Do not use wildcards (`.*`) or escapes (`\\.`)" in text

    def test_custom_grep_description_is_never_overwritten(self) -> None:
        middleware = FilesystemMiddleware(custom_tool_descriptions={"grep": "Charmander"})
        request = _PromptRequest(list(middleware.tools), _runtime(_state()))

        middleware._prepare_request(request)  # type: ignore[arg-type]

        assert next(tool for tool in request.tools if tool.name == "grep").description == "Charmander"

    def test_execute_is_filtered_out_when_the_backend_cannot_execute(self) -> None:
        middleware = FilesystemMiddleware()
        request = _PromptRequest(list(middleware.tools), _runtime(_state()))

        middleware._prepare_request(request)  # type: ignore[arg-type]

        assert "execute" not in {tool.name for tool in request.tools}


class TestReadFileDescription:
    def test_examples_cite_the_real_param_name(self) -> None:
        """The examples used to say `read_file(path, ...)`; the parameter is `file_path`."""
        from bog_agents.middleware.filesystem import READ_FILE_TOOL_DESCRIPTION

        assert "read_file(path," not in READ_FILE_TOOL_DESCRIPTION
        assert 'read_file(file_path="...", limit=100)' in READ_FILE_TOOL_DESCRIPTION


class TestWriteFileDescription:
    def test_typo_is_gone_and_overwrite_semantics_are_stated(self) -> None:
        from bog_agents.middleware.filesystem import WRITE_FILE_TOOL_DESCRIPTION

        assert "create the a new file" not in WRITE_FILE_TOOL_DESCRIPTION
        assert "replaces it entirely if it does" in WRITE_FILE_TOOL_DESCRIPTION


class TestGrepRegexHint:
    def test_zero_match_regex_pattern_gets_a_literal_hint(self) -> None:
        middleware = FilesystemMiddleware()
        state = _state({"/a.txt": create_file_data("hello world")})
        result = _tool(middleware, "grep").invoke({"pattern": "foo|bar", "runtime": _runtime(state)})
        assert "grep matches literal text, not regex" in result

    def test_zero_match_literal_pattern_gets_no_hint(self) -> None:
        middleware = FilesystemMiddleware()
        state = _state({"/a.txt": create_file_data("hello world")})
        result = _tool(middleware, "grep").invoke({"pattern": "nothing_here", "runtime": _runtime(state)})
        assert "literal text, not regex" not in result

    def test_a_fully_redacted_result_gets_no_regex_hint(self) -> None:
        """Matches existed but were all denied — that has nothing to do with regex
        syntax, so hinting at it would send the model down the wrong path."""
        middleware = FilesystemMiddleware(
            _permissions=[FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="deny")],
        )
        state = _state({"/secrets/a.txt": create_file_data("foo|bar")})
        result = _tool(middleware, "grep").invoke({"pattern": "foo|bar", "runtime": _runtime(state)})
        assert "literal text, not regex" not in result
