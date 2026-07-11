"""End to end unit tests that verify that the bog-agents can use file system tools.

At the moment these tests are written against the state backend, but we will need
to extend them to other backends as well.
"""

import sys

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from bog_agents.graph import create_agent
from tests.unit_tests.chat_model import GenericFakeChatModel


def test_parallel_write_file_calls_trigger_list_reducer() -> None:
    """Verify that parallel write_file calls correctly update file state.

    This test ensures that when an agent's model issues multiple `write_file`
    tool calls in parallel, the `_file_data_reducer` correctly handles the
    list of file updates and merges them into the final state.
    It guards against regressions of the `TypeError` that occurred when the
    reducer received a list instead of a dictionary.
    """
    # Fake model will issue two write_file tool calls in a single turn
    fake_model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "/test1.txt", "content": "hello"},
                            "id": "call_write_file_1",
                            "type": "tool_call",
                        },
                        {
                            "name": "write_file",
                            "args": {"file_path": "/test2.txt", "content": "world"},
                            "id": "call_write_file_2",
                            "type": "tool_call",
                        },
                    ],
                ),
                # Final acknowledgment message
                AIMessage(content="I have written the files."),
            ]
        )
    )

    # Create a bog-agents agent with the fake model and a memory saver
    agent = create_agent(
        model=fake_model,
        checkpointer=InMemorySaver(),
    )

    # Invoke the agent, which will trigger the parallel tool calls
    result = agent.invoke(
        {"messages": [HumanMessage(content="Write two files")]},
        config={"configurable": {"thread_id": "test_thread_parallel_writes"}},
    )

    # Verify that both files exist in the final state
    assert "/test1.txt" in result["files"], "File /test1.txt should exist in the final state"
    assert "/test2.txt" in result["files"], "File /test2.txt should exist in the final state"

    # Verify the content of the files
    assert result["files"]["/test1.txt"]["content"] == "hello", "Content of /test1.txt should be 'hello'"
    assert result["files"]["/test2.txt"]["content"] == "world", "Content of /test2.txt should be 'world'"


def test_edit_file_single_replacement() -> None:
    """Verify that edit_file correctly replaces a single occurrence of a string."""
    # Fake model will write a file, then edit it
    fake_model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "/code.py", "content": "def hello():\n    print('hello world')"},
                            "id": "call_write_1",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "edit_file",
                            "args": {
                                "file_path": "/code.py",
                                "old_string": "hello world",
                                "new_string": "hello universe",
                            },
                            "id": "call_edit_1",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(content="I have edited the file."),
            ]
        )
    )

    agent = create_agent(
        model=fake_model,
        checkpointer=InMemorySaver(),
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="Edit the file")]},
        config={"configurable": {"thread_id": "test_thread_edit"}},
    )

    # Verify the file was edited correctly
    assert "/code.py" in result["files"], "File /code.py should exist"
    full_content = result["files"]["/code.py"]["content"]
    assert "hello universe" in full_content, f"Content should be updated, got: {full_content}"
    assert "hello world" not in full_content, "Old content should be replaced"


def test_edit_file_replace_all() -> None:
    """Verify that edit_file with replace_all replaces all occurrences of a string."""
    # Fake model will write a file with repeated content, then edit all occurrences
    fake_model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": "/data.txt",
                                "content": "foo bar foo baz foo",
                            },
                            "id": "call_write_1",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "edit_file",
                            "args": {
                                "file_path": "/data.txt",
                                "old_string": "foo",
                                "new_string": "qux",
                                "replace_all": True,
                            },
                            "id": "call_edit_1",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(content="I have edited all occurrences."),
            ]
        )
    )

    agent = create_agent(
        model=fake_model,
        checkpointer=InMemorySaver(),
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="Edit all occurrences")]},
        config={"configurable": {"thread_id": "test_thread_edit_all"}},
    )

    # Verify all occurrences were replaced
    assert "/data.txt" in result["files"], "File /data.txt should exist"
    content = result["files"]["/data.txt"]["content"]
    assert content == "qux bar qux baz qux", "All occurrences of 'foo' should be replaced with 'qux'"


def test_edit_file_nonexistent_file() -> None:
    """Verify that edit_file returns an error when attempting to edit a nonexistent file."""
    # Fake model will attempt to edit a file that doesn't exist
    fake_model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "edit_file",
                            "args": {
                                "file_path": "/nonexistent.txt",
                                "old_string": "hello",
                                "new_string": "goodbye",
                            },
                            "id": "call_edit_1",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(content="I tried to edit the file."),
            ]
        )
    )

    agent = create_agent(
        model=fake_model,
        checkpointer=InMemorySaver(),
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="Edit nonexistent file")]},
        config={"configurable": {"thread_id": "test_thread_edit_nonexistent"}},
    )

    # Verify the error message in the ToolMessage
    tool_message = result["messages"][-2]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.content == "Error: File '/nonexistent.txt' not found"

    # Verify the file doesn't exist in state
    assert "/nonexistent.txt" not in result.get("files", {}), "Nonexistent file should not be in state"


def test_edit_file_string_not_found() -> None:
    """Verify that edit_file returns an error when the old_string is not found in the file."""
    # Fake model will write a file, then attempt to edit with a string that doesn't exist
    fake_model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "/test.txt", "content": "hello world"},
                            "id": "call_write_1",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "edit_file",
                            "args": {
                                "file_path": "/test.txt",
                                "old_string": "goodbye",
                                "new_string": "farewell",
                            },
                            "id": "call_edit_1",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(content="I tried to edit the file."),
            ]
        )
    )

    agent = create_agent(
        model=fake_model,
        checkpointer=InMemorySaver(),
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="Edit with non-existent string")]},
        config={"configurable": {"thread_id": "test_thread_edit_not_found"}},
    )

    tool_message = result["messages"][-2]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.content == "Error: String not found in file: 'goodbye'"


def test_grep_finds_written_file() -> None:
    """Verify that grep can find content in a file that was written."""
    # Fake model will write files with specific content, then grep for it
    fake_model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": "/project/main.py",
                                "content": "import os\nimport sys\n\ndef main():\n    print('Hello World')",
                            },
                            "id": "call_write_1",
                            "type": "tool_call",
                        },
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": "/project/utils.py",
                                "content": "def helper():\n    return 42",
                            },
                            "id": "call_write_2",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "grep",
                            "args": {
                                "pattern": "import",
                                "output_mode": "content",
                            },
                            "id": "call_grep_1",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(content="Found the imports."),
            ]
        )
    )

    agent = create_agent(
        model=fake_model,
        checkpointer=InMemorySaver(),
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="Write files and search")]},
        config={"configurable": {"thread_id": "test_thread_grep"}},
    )

    # Verify files were created
    assert "/project/main.py" in result["files"], "File /project/main.py should exist"
    assert "/project/utils.py" in result["files"], "File /project/utils.py should exist"

    # Verify grep found the pattern in messages
    grep_message = result["messages"][-2]
    assert isinstance(grep_message, ToolMessage)
    assert "import" in grep_message.content.lower(), "Grep should find 'import' in the files"
    assert "/project/main.py" in grep_message.content, "Grep should reference the file containing 'import'"


# Parallel edits to the SAME file race each other in StateBackend (and via overlapping
# edits in sandbox/file system backends): each edit reads the same base version and the
# last-writer-wins reducer silently clobbers the others. The FilesystemMiddleware's
# after_model guard now rejects all-but-one conflicting write-class call and asks the
# model to sequence them across turns.
def test_parallel_edit_file_calls() -> None:
    """Verify conflicting parallel edit_file calls on one file are rejected, not clobbered."""
    # Fake model will write a file, then issue multiple edit_file calls in parallel
    fake_model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": "/multi.txt",
                                "content": "line one\nline two\nline three",
                            },
                            "id": "call_write_1",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "edit_file",
                            "args": {
                                "file_path": "/multi.txt",
                                "old_string": "one",
                                "new_string": "1",
                            },
                            "id": "call_edit_1",
                            "type": "tool_call",
                        },
                        {
                            "name": "edit_file",
                            "args": {
                                "file_path": "/multi.txt",
                                "old_string": "two",
                                "new_string": "2",
                            },
                            "id": "call_edit_2",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(content="I have edited the file."),
            ]
        )
    )

    agent = create_agent(
        model=fake_model,
        checkpointer=InMemorySaver(),
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="Edit file in parallel")]},
        config={"configurable": {"thread_id": "test_thread_parallel_edits"}},
    )

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    tool_by_id = {m.tool_call_id: m for m in tool_messages}

    # The second conflicting edit must have been rejected with an error ToolMessage.
    assert "call_edit_2" in tool_by_id, "Expected an error ToolMessage for the rejected parallel edit"
    rejected = tool_by_id["call_edit_2"]
    assert rejected.status == "error"
    assert "same file" in rejected.content
    assert "/multi.txt" in rejected.content

    # The first edit must still have executed (no lost write for the kept call).
    assert "call_edit_1" in tool_by_id, "Expected the first edit to still run"
    assert tool_by_id["call_edit_1"].status != "error"

    # The rejected edit must NOT be present as a live tool call on the rewritten AIMessage.
    edit_ai_messages = [
        m for m in result["messages"] if isinstance(m, AIMessage) and any(tc["id"] in {"call_edit_1", "call_edit_2"} for tc in m.tool_calls)
    ]
    assert len(edit_ai_messages) == 1
    kept_ids = {tc["id"] for tc in edit_ai_messages[0].tool_calls}
    assert kept_ids == {"call_edit_1"}, "The conflicting edit should be stripped from the AIMessage"

    # The surviving edit must have actually applied to the file contents.
    multi_content = result["files"]["/multi.txt"]["content"]
    if isinstance(multi_content, list):
        multi_content = "\n".join(multi_content)
    assert multi_content == "line 1\nline two\nline three"


def test_parallel_edit_file_calls_to_different_files_succeed() -> None:
    """Verify non-conflicting parallel edits to DIFFERENT files are not rejected."""
    fake_model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "/a.txt", "content": "alpha"},
                            "id": "call_write_a",
                            "type": "tool_call",
                        },
                        {
                            "name": "write_file",
                            "args": {"file_path": "/b.txt", "content": "beta"},
                            "id": "call_write_b",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "edit_file",
                            "args": {"file_path": "/a.txt", "old_string": "alpha", "new_string": "ALPHA"},
                            "id": "call_edit_a",
                            "type": "tool_call",
                        },
                        {
                            "name": "edit_file",
                            "args": {"file_path": "/b.txt", "old_string": "beta", "new_string": "BETA"},
                            "id": "call_edit_b",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(content="Edited both files."),
            ]
        )
    )

    agent = create_agent(
        model=fake_model,
        checkpointer=InMemorySaver(),
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="Edit two different files in parallel")]},
        config={"configurable": {"thread_id": "test_thread_parallel_diff_files"}},
    )

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    # No edit should be rejected - distinct files do not race.
    assert all(m.status != "error" for m in tool_messages), "Parallel edits to different files must not be rejected"

    # Both edits must have applied.
    def _content(path: str) -> str:
        raw = result["files"][path]["content"]
        return "\n".join(raw) if isinstance(raw, list) else raw

    assert _content("/a.txt") == "ALPHA"
    assert _content("/b.txt") == "BETA"


def test_detect_parallel_write_conflicts_same_file() -> None:
    """Two write-class calls on one (normalized) path keep the first, reject the rest."""
    from bog_agents.middleware.filesystem import _detect_parallel_write_conflicts

    msg = AIMessage(
        content="",
        tool_calls=[
            {"name": "edit_file", "args": {"file_path": "/x.txt", "old_string": "a", "new_string": "b"}, "id": "e1", "type": "tool_call"},
            # Same file, different spelling - normalization must still collide.
            {"name": "write_file", "args": {"file_path": "x.txt", "content": "z"}, "id": "w1", "type": "tool_call"},
        ],
    )
    kept, conflicts = _detect_parallel_write_conflicts(msg)
    assert [tc["id"] for tc in kept] == ["e1"]
    assert len(conflicts) == 1
    assert conflicts[0].tool_call_id == "w1"
    assert conflicts[0].status == "error"


def test_detect_parallel_write_conflicts_distinct_and_reads() -> None:
    """Distinct files and non-write tools never conflict."""
    from bog_agents.middleware.filesystem import _detect_parallel_write_conflicts

    msg = AIMessage(
        content="",
        tool_calls=[
            {"name": "read_file", "args": {"file_path": "/x.txt"}, "id": "r1", "type": "tool_call"},
            {"name": "edit_file", "args": {"file_path": "/x.txt", "old_string": "a", "new_string": "b"}, "id": "e1", "type": "tool_call"},
            {"name": "edit_file", "args": {"file_path": "/y.txt", "old_string": "a", "new_string": "b"}, "id": "e2", "type": "tool_call"},
        ],
    )
    kept, conflicts = _detect_parallel_write_conflicts(msg)
    assert {tc["id"] for tc in kept} == {"r1", "e1", "e2"}
    assert conflicts == []


def test_detect_parallel_write_conflicts_multi_edit_overlap() -> None:
    """multi_edit_file overlapping a prior edit's target on any sub-path conflicts."""
    from bog_agents.middleware.filesystem import _detect_parallel_write_conflicts

    msg = AIMessage(
        content="",
        tool_calls=[
            {"name": "edit_file", "args": {"file_path": "/shared.txt", "old_string": "a", "new_string": "b"}, "id": "e1", "type": "tool_call"},
            {
                "name": "multi_edit_file",
                "args": {
                    "edits": [
                        {"file_path": "/other.txt", "old_string": "a", "new_string": "b"},
                        {"file_path": "/shared.txt", "old_string": "c", "new_string": "d"},
                    ]
                },
                "id": "m1",
                "type": "tool_call",
            },
        ],
    )
    kept, conflicts = _detect_parallel_write_conflicts(msg)
    assert [tc["id"] for tc in kept] == ["e1"]
    assert [c.tool_call_id for c in conflicts] == ["m1"]


def test_path_traversal_returns_error_message() -> None:
    """Verify that path traversal attempts return error messages instead of crashing."""
    fake_model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "edit_file",
                            "args": {
                                "file_path": "./question/..",
                                "old_string": "test",
                                "new_string": "replaced",
                            },
                            "id": "call_path_traversal",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(content="I see there was an error with the path."),
            ]
        )
    )

    agent = create_agent(
        model=fake_model,
        checkpointer=InMemorySaver(),
    )

    # This should NOT raise an exception - it should return an error message
    result = agent.invoke(
        {"messages": [HumanMessage(content="Edit a file with bad path")]},
        config={"configurable": {"thread_id": "test_path_traversal"}},
    )

    # Find the ToolMessage in the result
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) >= 1, "Expected at least one ToolMessage"

    # The tool message should contain an error about path traversal
    error_message = tool_messages[0].content
    assert error_message == "Error: Path traversal not allowed: ./question/.."


@pytest.mark.skipif(sys.platform == "win32", reason="validate_path accepts Windows absolute paths on Windows; rejection is Linux-only behavior")
def test_windows_absolute_path_returns_error_message() -> None:
    """Verify that Windows absolute paths return error messages instead of crashing."""
    fake_model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {
                                "file_path": "C:\\Users\\test\\file.txt",
                            },
                            "id": "call_windows_path",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(content="I see there was an error with the path."),
            ]
        )
    )

    agent = create_agent(
        model=fake_model,
        checkpointer=InMemorySaver(),
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="Read a file with Windows path")]},
        config={"configurable": {"thread_id": "test_windows_path"}},
    )

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) >= 1, "Expected at least one ToolMessage"

    error_message = tool_messages[0].content
    expected_error = (
        "Error: Windows absolute paths are not supported: C:\\Users\\test\\file.txt. "
        "Please use virtual paths starting with / (e.g., /workspace/file.txt)"
    )
    assert error_message == expected_error


def test_tilde_path_returns_error_message() -> None:
    """Verify that tilde paths return error messages instead of crashing."""
    fake_model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": "~/secret.txt",
                                "content": "secret data",
                            },
                            "id": "call_tilde_path",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(content="I see there was an error with the path."),
            ]
        )
    )

    agent = create_agent(
        model=fake_model,
        checkpointer=InMemorySaver(),
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="Write a file with tilde path")]},
        config={"configurable": {"thread_id": "test_tilde_path"}},
    )

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) >= 1, "Expected at least one ToolMessage"

    error_message = tool_messages[0].content
    assert error_message == "Error: Path traversal not allowed: ~/secret.txt"


def test_ls_with_invalid_path_returns_error_message() -> None:
    """Verify that ls tool with invalid path returns error message instead of crashing."""
    fake_model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "ls",
                            "args": {
                                "path": "../../../etc",
                            },
                            "id": "call_ls_invalid",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(content="I see there was an error with the path."),
            ]
        )
    )

    agent = create_agent(
        model=fake_model,
        checkpointer=InMemorySaver(),
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="List directory with invalid path")]},
        config={"configurable": {"thread_id": "test_ls_invalid_path"}},
    )

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) >= 1, "Expected at least one ToolMessage"

    error_message = tool_messages[0].content
    assert error_message == "Error: Path traversal not allowed: ../../../etc"


def test_deny_rule_filters_pathless_grep_results_end_to_end() -> None:
    """A `deny` permission rule must filter denied paths out of a pathless grep.

    Regression for the wiring gap where `create_agent(permissions=...)` built the
    filesystem tools without threading `_permissions` into `FilesystemMiddleware`, so the
    result filters never ran. A pathless `grep` (no `path` argument, so the argument-side
    deny check has nothing to match) would return matches -- including file contents --
    from a denied subtree. See PARITY.md wave 1B.
    """
    from bog_agents.middleware.permissions import FilesystemPermission

    fake_model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "/secrets/prod.env", "content": "API_KEY=sk-live-deadbeef"},
                            "id": "call_write_secret",
                            "type": "tool_call",
                        },
                        {
                            "name": "write_file",
                            "args": {"file_path": "/work/app.py", "content": "# API_KEY is read from env"},
                            "id": "call_write_work",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "grep",
                            "args": {"pattern": "API_KEY", "output_mode": "content"},
                            "id": "call_grep_pathless",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
    )

    agent = create_agent(
        model=fake_model,
        checkpointer=InMemorySaver(),
        permissions=[FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="deny")],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="write and search")]},
        config={"configurable": {"thread_id": "test_thread_deny_grep"}},
    )

    grep_message = result["messages"][-2]
    assert isinstance(grep_message, ToolMessage)
    assert "/secrets/prod.env" not in grep_message.content, "grep must not leak a denied path"
    assert "sk-live-deadbeef" not in grep_message.content, "grep must not leak denied file contents"
    # The non-denied hit must still come through -- the filter must not over-block.
    assert "/work/app.py" in grep_message.content, "grep must still return allowed matches"
