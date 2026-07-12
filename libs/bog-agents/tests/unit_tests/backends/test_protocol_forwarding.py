"""Pin the `BackendProtocol` forwarding matrix between the legacy and structured APIs.

The base class forwards between two generations of the read/list/search surface via
override detection:

- structured: `ls` / `read_file` / `grep` / `glob` / `delete`
- legacy: `ls_info` / `read` / `grep_raw` / `glob_info`

A backend implementing only one generation must remain reachable through the other,
a backend implementing *neither* must raise `NotImplementedError` (never recurse),
and the legacy names must keep warning. These tests fail loudly if any of that
forwarding is removed.
"""

import dataclasses
import typing
from typing import Any

import pytest

from bog_agents._api.deprecation import reset_deprecation_dedupe
from bog_agents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    FileInfo,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    _resolve_backend,
    _supports_delete,
    supports_delete,
)
from bog_agents.backends.utils import create_file_data

_LS_ENTRIES: list[FileInfo] = [{"path": "/src/main.py", "is_dir": False, "size": 12}]
_GREP_MATCHES: list[GrepMatch] = [{"path": "/src/main.py", "line": 2, "text": "beta"}]
_GLOB_MATCHES: list[FileInfo] = [{"path": "/src/main.py"}]
_FILE_CONTENT = "alpha\nbeta\ngamma"


@pytest.fixture(autouse=True)
def _reset_deprecation_flags() -> None:
    """Clear the `@deprecated` once-per-process dedupe so warning assertions are order-independent."""
    reset_deprecation_dedupe(
        BackendProtocol.ls_info,
        BackendProtocol.als_info,
        BackendProtocol.grep_raw,
        BackendProtocol.agrep_raw,
        BackendProtocol.glob_info,
        BackendProtocol.aglob_info,
    )


class LegacyOnlyBackend(BackendProtocol):
    """Backend from before the structured API: implements only the legacy names."""

    def ls_info(self, path: str) -> list[FileInfo]:
        return list(_LS_ENTRIES)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        return f"     1\t{_FILE_CONTENT.splitlines()[0]}"

    def grep_raw(self, pattern: str, path: str | None = None, glob: str | None = None) -> list[GrepMatch] | str:
        if pattern == "boom":
            return "Error: grep exploded"
        return list(_GREP_MATCHES)

    def glob_info(self, pattern: str, path: str | None = "/") -> list[FileInfo]:
        return list(_GLOB_MATCHES)


class NewOnlyBackend(BackendProtocol):
    """Backend written against the structured API only."""

    def ls(self, path: str) -> LsResult:
        if path == "/missing":
            return LsResult(error="Error: directory not found")
        return LsResult(entries=list(_LS_ENTRIES))

    def read_file(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if file_path == "/missing.txt":
            return ReadResult(error="Error: File '/missing.txt' not found")
        return ReadResult(file_data=create_file_data(_FILE_CONTENT))

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        if pattern == "boom":
            return GrepResult(error="Error: grep exploded")
        return GrepResult(matches=list(_GREP_MATCHES))

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        if pattern == "boom":
            return GlobResult(error="Error: glob exploded")
        return GlobResult(matches=list(_GLOB_MATCHES))

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(
            path=file_path,
            files_update={file_path: None},
            deleted_paths=[file_path, f"{file_path}/nested.txt"],
        )


class NeitherBackend(BackendProtocol):
    """Implements neither generation — the infinite-recursion trap."""


class TestLegacyOnlyBackendReachableViaStructuredAPI:
    """A backend that only implements `ls_info`/`grep_raw`/`glob_info` works through `ls`/`grep`/`glob`."""

    def test_ls_forwards_to_ls_info(self) -> None:
        backend = LegacyOnlyBackend()
        with pytest.warns(DeprecationWarning, match="ls_info"):
            result = backend.ls("/src")
        assert isinstance(result, LsResult)
        assert result.error is None
        assert result.entries == _LS_ENTRIES

    def test_grep_forwards_to_grep_raw(self) -> None:
        backend = LegacyOnlyBackend()
        with pytest.warns(DeprecationWarning, match="grep_raw"):
            result = backend.grep("beta")
        assert isinstance(result, GrepResult)
        assert result.error is None
        assert result.matches == _GREP_MATCHES
        assert result.truncated is False

    def test_grep_forwards_grep_raw_error_string_into_error_field(self) -> None:
        backend = LegacyOnlyBackend()
        with pytest.warns(DeprecationWarning, match="grep_raw"):
            result = backend.grep("boom")
        assert result.error == "Error: grep exploded"
        assert result.matches is None

    def test_glob_forwards_to_glob_info(self) -> None:
        backend = LegacyOnlyBackend()
        with pytest.warns(DeprecationWarning, match="glob_info"):
            result = backend.glob("*.py")
        assert isinstance(result, GlobResult)
        assert result.error is None
        assert result.matches == _GLOB_MATCHES

    def test_read_file_is_not_synthesizable_from_read(self) -> None:
        # Documented asymmetry: a line-numbered string cannot be reversed into FileData.
        backend = LegacyOnlyBackend()
        with pytest.raises(NotImplementedError):
            backend.read_file("/src/main.py")


class TestNewOnlyBackendReachableViaLegacyAPI:
    """A backend that only implements `ls`/`grep`/`glob`/`read_file` works through the legacy names."""

    def test_ls_info_forwards_to_ls(self) -> None:
        backend = NewOnlyBackend()
        with pytest.warns(DeprecationWarning, match="ls_info"):
            entries = backend.ls_info("/src")
        assert entries == _LS_ENTRIES

    def test_ls_info_raises_when_ls_reports_error(self) -> None:
        # The legacy list-shape cannot express an error, so it must not silently return [].
        backend = NewOnlyBackend()
        with pytest.warns(DeprecationWarning, match="ls_info"), pytest.raises(NotImplementedError):
            backend.ls_info("/missing")

    def test_grep_raw_forwards_to_grep(self) -> None:
        backend = NewOnlyBackend()
        with pytest.warns(DeprecationWarning, match="grep_raw"):
            matches = backend.grep_raw("beta")
        assert matches == _GREP_MATCHES

    def test_grep_raw_returns_error_string_when_grep_errors(self) -> None:
        backend = NewOnlyBackend()
        with pytest.warns(DeprecationWarning, match="grep_raw"):
            result = backend.grep_raw("boom")
        assert result == "Error: grep exploded"

    def test_glob_info_forwards_to_glob(self) -> None:
        backend = NewOnlyBackend()
        with pytest.warns(DeprecationWarning, match="glob_info"):
            matches = backend.glob_info("*.py")
        assert matches == _GLOB_MATCHES

    def test_glob_info_raises_when_glob_reports_error(self) -> None:
        backend = NewOnlyBackend()
        with pytest.warns(DeprecationWarning, match="glob_info"), pytest.raises(NotImplementedError):
            backend.glob_info("boom")

    def test_read_renders_read_file(self) -> None:
        backend = NewOnlyBackend()
        rendered = backend.read("/src/main.py")
        assert isinstance(rendered, str)
        assert rendered.splitlines() == [
            "     1\talpha",
            "     2\tbeta",
            "     3\tgamma",
        ]

    def test_read_offset_shifts_rendered_line_numbers(self) -> None:
        # `read` renders starting at offset+1 — the sliced window keeps its true line numbers.
        backend = NewOnlyBackend()
        rendered = backend.read("/src/main.py", offset=2, limit=1)
        assert rendered.startswith("     3\t")

    def test_read_propagates_read_file_error_string(self) -> None:
        backend = NewOnlyBackend()
        assert backend.read("/missing.txt") == "Error: File '/missing.txt' not found"


class TestNeitherGenerationRaisesWithoutRecursing:
    """The trap: mutual forwarding must terminate in `NotImplementedError`, not `RecursionError`.

    `pytest.raises(NotImplementedError)` is the assertion — a `RecursionError` would
    not be caught and the test would fail.
    """

    def test_ls(self) -> None:
        with pytest.raises(NotImplementedError):
            NeitherBackend().ls("/")

    def test_ls_info(self) -> None:
        with pytest.warns(DeprecationWarning, match="ls_info"), pytest.raises(NotImplementedError):
            NeitherBackend().ls_info("/")

    def test_grep(self) -> None:
        with pytest.raises(NotImplementedError):
            NeitherBackend().grep("x")

    def test_grep_raw(self) -> None:
        with pytest.warns(DeprecationWarning, match="grep_raw"), pytest.raises(NotImplementedError):
            NeitherBackend().grep_raw("x")

    def test_glob(self) -> None:
        with pytest.raises(NotImplementedError):
            NeitherBackend().glob("*.py")

    def test_glob_info(self) -> None:
        with pytest.warns(DeprecationWarning, match="glob_info"), pytest.raises(NotImplementedError):
            NeitherBackend().glob_info("*.py")

    def test_read(self) -> None:
        with pytest.raises(NotImplementedError):
            NeitherBackend().read("/f.txt")

    def test_read_file(self) -> None:
        with pytest.raises(NotImplementedError):
            NeitherBackend().read_file("/f.txt")

    def test_delete(self) -> None:
        with pytest.raises(NotImplementedError):
            NeitherBackend().delete("/f.txt")

    async def test_als(self) -> None:
        with pytest.raises(NotImplementedError):
            await NeitherBackend().als("/")

    async def test_aread(self) -> None:
        with pytest.raises(NotImplementedError):
            await NeitherBackend().aread("/f.txt")

    async def test_aglob(self) -> None:
        with pytest.raises(NotImplementedError):
            await NeitherBackend().aglob("*.py")


class TestReadStillReturnsStr:
    """`read` is the rendered form and must keep its `str` contract — annotation and runtime."""

    def test_annotation_is_str(self) -> None:
        hints = typing.get_type_hints(BackendProtocol.read)
        assert hints["return"] is str

    def test_runtime_type_is_str_on_new_only_backend(self) -> None:
        assert type(NewOnlyBackend().read("/src/main.py")) is str

    def test_runtime_type_is_str_on_legacy_only_backend(self) -> None:
        assert type(LegacyOnlyBackend().read("/src/main.py")) is str

    async def test_aread_annotation_and_runtime_type_are_str(self) -> None:
        assert typing.get_type_hints(BackendProtocol.aread)["return"] is str
        assert type(await NewOnlyBackend().aread("/src/main.py")) is str


class TestSupportsDelete:
    """`supports_delete` detects the optional `delete` override without invoking it."""

    def test_backend_with_delete(self) -> None:
        backend = NewOnlyBackend()
        assert supports_delete(backend) is True
        assert _supports_delete(backend) is True

    def test_backend_without_delete(self) -> None:
        backend = LegacyOnlyBackend()
        assert supports_delete(backend) is False
        assert _supports_delete(backend) is False

    def test_unsupported_backend_delete_raises_rather_than_returning_none(self) -> None:
        with pytest.raises(NotImplementedError):
            LegacyOnlyBackend().delete("/f.txt")


class TestDeleteResult:
    """`DeleteResult` carries `files_update` — bog-specific; upstream deepagents' does not."""

    def test_files_update_field_exists(self) -> None:
        names = {f.name for f in dataclasses.fields(DeleteResult)}
        assert {"error", "path", "files_update", "deleted_paths"} <= names

    def test_delete_round_trips_files_update_and_deleted_paths(self) -> None:
        result = NewOnlyBackend().delete("/dir")
        assert result.error is None
        assert result.path == "/dir"
        assert result.files_update == {"/dir": None}
        assert result.deleted_paths == ["/dir", "/dir/nested.txt"]

    def test_deleted_paths_defaults_to_a_fresh_list(self) -> None:
        first = DeleteResult()
        second = DeleteResult()
        assert first.deleted_paths == []
        first.deleted_paths.append("/x")
        assert second.deleted_paths == []


class TestAsyncForwarding:
    """The async wrappers must ride the same forwarding matrix as their sync twins."""

    async def test_als_on_legacy_only_backend(self) -> None:
        with pytest.warns(DeprecationWarning, match="ls_info"):
            result = await LegacyOnlyBackend().als("/src")
        assert result.entries == _LS_ENTRIES

    async def test_als_on_new_only_backend(self) -> None:
        result = await NewOnlyBackend().als("/src")
        assert result.entries == _LS_ENTRIES

    async def test_agrep_on_legacy_only_backend(self) -> None:
        with pytest.warns(DeprecationWarning, match="grep_raw"):
            result = await LegacyOnlyBackend().agrep("beta")
        assert result.matches == _GREP_MATCHES

    async def test_agrep_on_new_only_backend(self) -> None:
        result = await NewOnlyBackend().agrep("beta")
        assert result.matches == _GREP_MATCHES

    async def test_aglob_on_legacy_only_backend(self) -> None:
        with pytest.warns(DeprecationWarning, match="glob_info"):
            result = await LegacyOnlyBackend().aglob("*.py")
        assert result.matches == _GLOB_MATCHES

    async def test_aglob_on_new_only_backend(self) -> None:
        result = await NewOnlyBackend().aglob("*.py")
        assert result.matches == _GLOB_MATCHES

    async def test_aread_file_on_new_only_backend(self) -> None:
        result = await NewOnlyBackend().aread_file("/src/main.py")
        assert result.error is None
        assert result.file_data is not None
        assert result.file_data["content"] == _FILE_CONTENT

    async def test_aread_file_on_legacy_only_backend_raises(self) -> None:
        # Same asymmetry as the sync path: `read` cannot synthesize `read_file`.
        with pytest.raises(NotImplementedError):
            await LegacyOnlyBackend().aread_file("/src/main.py")

    async def test_aread_renders_from_aread_file_on_new_only_backend(self) -> None:
        rendered = await NewOnlyBackend().aread("/src/main.py")
        assert rendered.splitlines()[1] == "     2\tbeta"

    async def test_aread_uses_overridden_sync_read_on_legacy_only_backend(self) -> None:
        assert await LegacyOnlyBackend().aread("/src/main.py") == "     1\talpha"

    async def test_als_info_forwards_to_als(self) -> None:
        with pytest.warns(DeprecationWarning, match="als_info"):
            entries = await NewOnlyBackend().als_info("/src")
        assert entries == _LS_ENTRIES

    async def test_als_info_raises_when_als_reports_error(self) -> None:
        with pytest.warns(DeprecationWarning, match="als_info"), pytest.raises(NotImplementedError):
            await NewOnlyBackend().als_info("/missing")

    async def test_agrep_raw_forwards_to_agrep(self) -> None:
        with pytest.warns(DeprecationWarning, match="agrep_raw"):
            matches = await NewOnlyBackend().agrep_raw("beta")
        assert matches == _GREP_MATCHES

    async def test_agrep_raw_returns_error_string(self) -> None:
        with pytest.warns(DeprecationWarning, match="agrep_raw"):
            result = await NewOnlyBackend().agrep_raw("boom")
        assert result == "Error: grep exploded"

    async def test_aglob_info_forwards_to_aglob(self) -> None:
        with pytest.warns(DeprecationWarning, match="aglob_info"):
            matches = await NewOnlyBackend().aglob_info("*.py")
        assert matches == _GLOB_MATCHES

    async def test_adelete_forwards_to_delete(self) -> None:
        result = await NewOnlyBackend().adelete("/dir")
        assert result.deleted_paths == ["/dir", "/dir/nested.txt"]
        assert result.files_update == {"/dir": None}


class TestResolveBackend:
    """`_resolve_backend` accepts an instance or a `(runtime) -> backend` factory."""

    def test_instance_passthrough(self) -> None:
        backend = NewOnlyBackend()
        assert _resolve_backend(backend, typing.cast("Any", object())) is backend

    def test_factory_is_called_with_runtime(self) -> None:
        backend = NewOnlyBackend()
        seen: list[object] = []

        def factory(runtime: Any) -> BackendProtocol:
            seen.append(runtime)
            return backend

        runtime = object()
        assert _resolve_backend(factory, typing.cast("Any", runtime)) is backend
        assert seen == [runtime]
