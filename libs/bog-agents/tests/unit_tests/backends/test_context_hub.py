"""Unit tests for `ContextHubBackend` (no network — the langsmith client is faked)."""

from typing import Any
from uuid import uuid4

import pytest
from langsmith.schemas import AgentContext, AgentEntry, FileEntry
from langsmith.utils import LangSmithError, LangSmithNotFoundError

from bog_agents.backends.context_hub import _URL_COMMIT_SUFFIX_RE, ContextHubBackend

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


class FakeClient:
    """Stands in for `langsmith.Client`, recording every push."""

    def __init__(
        self,
        files: dict[str, Any] | None = None,
        commit_hash: str | None = COMMIT_A,
        *,
        pull_error: Exception | None = None,
        push_error: Exception | None = None,
    ) -> None:
        self.files = files if files is not None else {}
        self.commit_hash = commit_hash
        self.pull_error = pull_error
        self.push_error = push_error
        self.pulls = 0
        self.pushes: list[dict[str, Any]] = []

    def pull_agent(self, identifier: str) -> AgentContext:
        self.pulls += 1
        if self.pull_error is not None:
            raise self.pull_error
        return AgentContext(commit_id=uuid4(), commit_hash=self.commit_hash, files=dict(self.files))

    def push_agent(self, identifier: str, *, files: dict[str, Any], parent_commit: str | None) -> str:
        if self.push_error is not None:
            raise self.push_error
        self.pushes.append({"files": files, "parent_commit": parent_commit})
        for path, entry in files.items():
            if entry is None:
                self.files.pop(path, None)
            else:
                self.files[path] = entry
        self.commit_hash = COMMIT_B
        return f"https://smith.langchain.com/hub/{identifier}:{COMMIT_B}"


def _file(content: str) -> FileEntry:
    return FileEntry(type="file", content=content)


def make_backend(files: dict[str, Any] | None = None, **kwargs: Any) -> tuple[ContextHubBackend, FakeClient]:
    client = FakeClient(files=files, **kwargs)
    return ContextHubBackend("acme/notes", client=client), client


@pytest.fixture
def backend() -> ContextHubBackend:
    files = {
        "README.md": _file("hello\nworld\n"),
        "src/main.py": _file("import os\nTODO: fix\n"),
        "src/util.py": _file("def helper():\n    pass\n"),
        "linked-agent": AgentEntry(type="agent", repo_handle="acme/other"),
    }
    return make_backend(files)[0]


# -- URL / commit parsing ----------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://smith.langchain.com/hub/acme/notes:{COMMIT_A}", COMMIT_A),
        ("https://smith.langchain.com/hub/acme/notes:0123abcd", "0123abcd"),
        ("https://smith.langchain.com/hub/acme/notes", None),
        ("https://smith.langchain.com/hub/acme/notes:short", None),
        ("https://smith.langchain.com/hub/acme/notes:0123abc", None),  # 7 chars — below the 8-char floor
        (f"https://smith.langchain.com/hub/acme/notes:{COMMIT_A}/extra", None),
    ],
)
def test_url_commit_suffix_re(url: str, expected: str | None) -> None:
    match = _URL_COMMIT_SUFFIX_RE.search(url)
    assert (match.group(1) if match else None) == expected


def test_commit_hash_tracked_across_pushes() -> None:
    backend, client = make_backend({}, commit_hash=COMMIT_A)

    backend.write("/a.txt", "one")
    assert client.pushes[0]["parent_commit"] == COMMIT_A

    backend.write("/b.txt", "two")
    assert client.pushes[1]["parent_commit"] == COMMIT_B


# -- linked entries / prior commits ------------------------------------------


def test_get_linked_entries(backend: ContextHubBackend) -> None:
    assert backend.get_linked_entries() == {"linked-agent": "acme/other"}
    # Linked entries are not readable as files.
    assert backend.read_file("/linked-agent").error is not None


def test_has_prior_commits_true(backend: ContextHubBackend) -> None:
    assert backend.has_prior_commits() is True


def test_has_prior_commits_false_for_missing_repo() -> None:
    backend, client = make_backend(pull_error=LangSmithNotFoundError("no such repo"))

    assert backend.has_prior_commits() is False
    assert backend.get_linked_entries() == {}
    assert backend.ls("/").entries == []
    # A missing repo is cached as empty — it is not re-pulled on every call.
    assert client.pulls == 1


def test_first_write_to_missing_repo_has_no_parent_commit() -> None:
    backend, client = make_backend(pull_error=LangSmithNotFoundError("no such repo"))

    assert backend.write("/a.txt", "one").path == "/a.txt"
    assert client.pushes[0]["parent_commit"] is None


# -- read --------------------------------------------------------------------


def test_read_file(backend: ContextHubBackend) -> None:
    result = backend.read_file("/README.md")
    assert result.error is None
    assert result.file_data is not None
    assert result.file_data["content"] == "hello\nworld\n"


def test_read_file_offset_and_limit(backend: ContextHubBackend) -> None:
    result = backend.read_file("/src/main.py", offset=1, limit=1)
    assert result.file_data is not None
    assert result.file_data["content"] == "TODO: fix\n"


def test_read_file_offset_past_eof(backend: ContextHubBackend) -> None:
    result = backend.read_file("/README.md", offset=99)
    assert result.error is not None
    assert "exceeds file length" in result.error


def test_read_file_not_found(backend: ContextHubBackend) -> None:
    assert "not found" in (backend.read_file("/nope.md").error or "")


def test_read_renders_line_numbers_via_protocol_forwarding(backend: ContextHubBackend) -> None:
    # `read` is not overridden — the protocol synthesizes it from `read_file`.
    rendered = backend.read("/README.md")
    assert "1\thello" in rendered
    assert "2\tworld" in rendered


# -- write / edit / delete ---------------------------------------------------


def test_write_creates_file() -> None:
    backend, client = make_backend({})

    result = backend.write("/notes/todo.md", "buy milk")

    assert result.path == "/notes/todo.md"
    assert result.files_update is None  # external storage
    assert client.files["notes/todo.md"].content == "buy milk"

    read = backend.read_file("/notes/todo.md")
    assert read.file_data is not None
    assert read.file_data["content"] == "buy milk"
    assert read.file_data["encoding"] == "utf-8"


def test_write_overwrites_existing_file(backend: ContextHubBackend) -> None:
    assert backend.write("/README.md", "replaced").error is None

    result = backend.read_file("/README.md")
    assert result.file_data is not None
    assert result.file_data["content"] == "replaced"


def test_edit_replaces_and_commits(backend: ContextHubBackend) -> None:
    result = backend.edit("/src/main.py", "TODO: fix", "DONE")

    assert result.occurrences == 1
    read = backend.read_file("/src/main.py")
    assert read.file_data is not None
    assert read.file_data["content"] == "import os\nDONE\n"


def test_edit_missing_file(backend: ContextHubBackend) -> None:
    assert "not found" in (backend.edit("/nope.py", "a", "b").error or "")


def test_edit_ambiguous_without_replace_all() -> None:
    backend, _ = make_backend({"a.txt": _file("x\nx\n")})

    assert "appears 2 times" in (backend.edit("/a.txt", "x", "y").error or "")
    assert backend.edit("/a.txt", "x", "y", replace_all=True).occurrences == 2


def test_edit_with_base_content_ignores_cache(backend: ContextHubBackend) -> None:
    result = backend.edit(
        "/src/main.py",
        "chained",
        "final",
        base_content={"content": "chained\n", "encoding": "utf-8"},
    )

    assert result.occurrences == 1
    read = backend.read_file("/src/main.py")
    assert read.file_data is not None
    assert read.file_data["content"] == "final\n"


def test_delete_file(backend: ContextHubBackend) -> None:
    result = backend.delete("/README.md")

    assert result.path == "/README.md"
    assert result.deleted_paths == ["/README.md"]
    assert backend.read_file("/README.md").error is not None


def test_delete_directory_is_recursive(backend: ContextHubBackend) -> None:
    result = backend.delete("/src")

    assert result.deleted_paths == ["/src/main.py", "/src/util.py"]
    assert backend.ls("/src").entries == []
    assert backend.read_file("/README.md").error is None


def test_delete_missing_path(backend: ContextHubBackend) -> None:
    assert "not found" in (backend.delete("/nope").error or "")


# -- ls / grep / glob --------------------------------------------------------


def test_ls_root_is_non_recursive(backend: ContextHubBackend) -> None:
    entries = backend.ls("/").entries or []

    assert sorted((e["path"], e["is_dir"]) for e in entries) == [
        ("/README.md", False),
        ("/src/", True),
    ]


def test_ls_subdirectory(backend: ContextHubBackend) -> None:
    entries = backend.ls("/src").entries or []

    assert sorted(e["path"] for e in entries) == ["/src/main.py", "/src/util.py"]
    assert all(e["is_dir"] is False for e in entries)


def test_grep_is_literal(backend: ContextHubBackend) -> None:
    matches = backend.grep("TODO").matches or []

    assert [(m["path"], m["line"], m["text"]) for m in matches] == [("/src/main.py", 2, "TODO: fix")]
    # Literal, not regex: `.*` is searched verbatim.
    assert (backend.grep("TO.*DO").matches or []) == []


def test_grep_path_and_glob_filters(backend: ContextHubBackend) -> None:
    assert (backend.grep("hello", path="/src").matches or []) == []
    assert len(backend.grep("def", glob="*.py").matches or []) == 1
    assert (backend.grep("def", glob="*.md").matches or []) == []


def test_glob(backend: ContextHubBackend) -> None:
    matches = backend.glob("**/*.py").matches or []

    assert sorted(m["path"] for m in matches) == ["/src/main.py", "/src/util.py"]
    assert all(m["size"] > 0 for m in matches)
    assert sorted(m["path"] for m in backend.glob("*.py", path="/src").matches or []) == ["/src/main.py", "/src/util.py"]
    assert (backend.glob("*.rs").matches or []) == []


# -- bulk transfer -----------------------------------------------------------


def test_upload_files_one_commit_rejects_binary() -> None:
    backend, client = make_backend({})

    responses = backend.upload_files([("/a.txt", b"alpha"), ("/bin.dat", b"\xff\xfe\x00"), ("/b.txt", b"beta")])

    assert [r.error for r in responses] == [None, "invalid_path", None]
    assert len(client.pushes) == 1
    assert sorted(client.pushes[0]["files"]) == ["a.txt", "b.txt"]


def test_download_files(backend: ContextHubBackend) -> None:
    responses = backend.download_files(["/README.md", "/nope.md"])

    assert responses[0].content == b"hello\nworld\n"
    assert responses[1].error == "file_not_found"


# -- hub outages -------------------------------------------------------------


def test_pull_failure_surfaces_as_error_on_every_read_path() -> None:
    backend, _ = make_backend(pull_error=LangSmithError("boom"))

    assert "Hub unavailable" in (backend.read_file("/a.txt").error or "")
    assert "Hub unavailable" in (backend.ls("/").error or "")
    assert "Hub unavailable" in (backend.grep("x").error or "")
    assert "Hub unavailable" in (backend.glob("*").error or "")
    assert "Hub unavailable" in (backend.delete("/a.txt").error or "")
    assert "Hub unavailable" in (backend.write("/a.txt", "x").error or "")
    assert "Hub unavailable" in (backend.edit("/a.txt", "x", "y").error or "")
    assert "Hub unavailable" in (backend.download_files(["/a.txt"])[0].error or "")
    assert "Hub unavailable" in (backend.upload_files([("/a.txt", b"x")])[0].error or "")


def test_push_failure_invalidates_cache() -> None:
    backend, client = make_backend({"a.txt": _file("one")}, push_error=LangSmithError("boom"))

    assert "Hub unavailable" in (backend.write("/a.txt", "two").error or "")
    assert backend._cache is None

    # Recovery: the next call re-pulls rather than serving a stale tree.
    client.push_error = None
    assert backend.write("/a.txt", "two").error is None
    assert client.pulls == 2
