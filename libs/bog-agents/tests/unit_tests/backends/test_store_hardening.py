"""Hardening tests for StoreBackend batch upload/download partial-success contract.

Covers [S13]: a single bad payload (non-UTF-8 upload bytes, or a corrupt store
item on download) must not abort the whole batch. Each failing entry should get
a response with a populated `error` field while sibling entries still succeed,
and response order must stay aligned with input order.
"""

from langchain.tools import ToolRuntime
from langgraph.store.memory import InMemoryStore

from bog_agents.backends.store import StoreBackend


def make_runtime():
    return ToolRuntime(
        state={"messages": []},
        context=None,
        tool_call_id="t13",
        store=InMemoryStore(),
        stream_writer=lambda _: None,
        config={},
    )


def test_upload_files_non_utf8_payload_does_not_poison_batch():
    rt = make_runtime()
    be = StoreBackend(rt, namespace=lambda _ctx: ("filesystem",))

    # Second file carries invalid UTF-8 bytes that cannot be decoded.
    files = [
        ("/good_a.txt", b"hello"),
        ("/bad.bin", b"\xff\xfe\xfa"),
        ("/good_b.txt", b"world"),
    ]
    responses = be.upload_files(files)

    # One response per input, in input order.
    assert [r.path for r in responses] == ["/good_a.txt", "/bad.bin", "/good_b.txt"]
    assert responses[0].error is None
    assert responses[1].error == "invalid_path"
    assert responses[2].error is None

    # Good files were actually persisted; bad file was not.
    assert "hello" in be.read("/good_a.txt")
    assert "world" in be.read("/good_b.txt")
    assert "not found" in be.read("/bad.bin")


def test_download_files_corrupt_item_does_not_poison_batch():
    rt = make_runtime()
    be = StoreBackend(rt, namespace=lambda _ctx: ("filesystem",))

    # Write one good file via the backend.
    assert be.write("/good.txt", "payload").error is None

    # Inject a corrupt store item (missing the required "content"/timestamps).
    store = rt.store
    store.put(("filesystem",), "/corrupt.txt", {"unexpected": "shape"})

    responses = be.download_files(["/good.txt", "/corrupt.txt", "/missing.txt"])

    assert [r.path for r in responses] == ["/good.txt", "/corrupt.txt", "/missing.txt"]
    # Good file downloads cleanly.
    assert responses[0].error is None
    assert responses[0].content is not None and b"payload" in responses[0].content
    # Corrupt item is reported per-file, batch continues.
    assert responses[1].error == "invalid_path"
    assert responses[1].content is None
    # Missing file keeps its distinct error code.
    assert responses[2].error == "file_not_found"
    assert responses[2].content is None
