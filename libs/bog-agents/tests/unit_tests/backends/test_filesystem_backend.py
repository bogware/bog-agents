import base64
import subprocess
import time
from pathlib import Path

import pytest
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage

from bog_agents.backends import filesystem as filesystem_module
from bog_agents.backends.filesystem import DEFAULT_GLOB_TIMEOUT, FilesystemBackend
from bog_agents.backends.protocol import (
    DEFAULT_GREP_TIMEOUT,
    DeleteResult,
    EditResult,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
    supports_delete,
)
from bog_agents.backends.utils import compile_grep_include_glob
from bog_agents.middleware.filesystem import GLOB_TIMEOUT, FilesystemMiddleware


def write_file(p: Path, content: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_filesystem_backend_normal_mode(tmp_path: Path):
    root = tmp_path
    f1 = root / "a.txt"
    f2 = root / "dir" / "b.py"
    write_file(f1, "hello fs")
    write_file(f2, "print('x')\nhello")

    be = FilesystemBackend(root_dir=str(root), virtual_mode=False)

    # ls_info absolute path - should only list files in root, not subdirectories
    infos = be.ls_info(str(root))
    paths = {i["path"] for i in infos}
    assert str(f1) in paths  # File in root should be listed
    assert str(f2) not in paths  # File in subdirectory should NOT be listed
    assert (str(root / "dir") + "/") in paths  # Directory should be listed

    # read, edit, write
    txt = be.read(str(f1))
    assert "hello fs" in txt
    msg = be.edit(str(f1), "fs", "filesystem", replace_all=False)
    assert isinstance(msg, EditResult) and msg.error is None and msg.occurrences == 1
    msg2 = be.write(str(root / "new.txt"), "new content")
    assert isinstance(msg2, WriteResult) and msg2.error is None and msg2.path.endswith("new.txt")

    # grep_raw
    matches = be.grep_raw("hello", path=str(root))
    assert isinstance(matches, list) and any(m["path"].endswith("a.txt") for m in matches)

    # glob_info
    g = be.glob_info("*.py", path=str(root))
    assert any(i["path"] == str(f2) for i in g)


def test_filesystem_backend_virtual_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path
    f1 = root / "a.txt"
    f2 = root / "dir" / "b.md"
    write_file(f1, "hello virtual")
    write_file(f2, "content")

    monkeypatch.setattr(FilesystemBackend, "_ripgrep_search", lambda *_args, **_kwargs: None)

    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    # ls_info from virtual root - should only list files in root, not subdirectories
    infos = be.ls_info("/")
    paths = {i["path"] for i in infos}
    assert "/a.txt" in paths  # File in root should be listed
    assert "/dir/b.md" not in paths  # File in subdirectory should NOT be listed
    assert "/dir/" in paths  # Directory should be listed

    # read and edit via virtual path
    txt = be.read("/a.txt")
    assert "hello virtual" in txt
    msg = be.edit("/a.txt", "virtual", "virt", replace_all=False)
    assert isinstance(msg, EditResult) and msg.error is None and msg.occurrences == 1

    # write new file via virtual path
    msg2 = be.write("/new.txt", "x")
    assert isinstance(msg2, WriteResult) and msg2.error is None
    assert (root / "new.txt").exists()

    # grep_raw limited to path
    matches = be.grep_raw("virt", path="/")
    assert isinstance(matches, list) and any(m["path"] == "/a.txt" for m in matches)

    # glob_info
    g = be.glob_info("**/*.md", path="/")
    assert any(i["path"] == "/dir/b.md" for i in g)

    # literal search should work with special regex chars like "[" and "("
    matches_bracket = be.grep_raw("[", path="/")
    assert isinstance(matches_bracket, list)  # Should not error, returns empty list or matches

    # path traversal blocked
    with pytest.raises(ValueError, match="traversal"):
        be.read("/../a.txt")


def test_filesystem_backend_grep_falls_back_when_ripgrep_launch_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """grep_raw should fall back to Python search when ripgrep cannot launch."""
    root = tmp_path
    target = root / "a.txt"
    write_file(target, "hello fallback")

    def deny_launch(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        msg = "access denied"
        raise PermissionError(msg)

    monkeypatch.setattr(subprocess, "run", deny_launch)

    backend = FilesystemBackend(root_dir=str(root), virtual_mode=False)
    matches = backend.grep_raw("hello", path=str(root))

    assert isinstance(matches, list)
    assert any(match["path"].endswith("a.txt") for match in matches)


def test_filesystem_backend_ls_nested_directories(tmp_path: Path):
    root = tmp_path

    files = {
        root / "config.json": "config",
        root / "src" / "main.py": "code",
        root / "src" / "utils" / "helper.py": "utils code",
        root / "src" / "utils" / "common.py": "common utils",
        root / "docs" / "readme.md": "documentation",
        root / "docs" / "api" / "reference.md": "api docs",
    }

    for path, content in files.items():
        write_file(path, content)

    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    root_listing = be.ls_info("/")
    root_paths = [fi["path"] for fi in root_listing]
    assert "/config.json" in root_paths
    assert "/src/" in root_paths
    assert "/docs/" in root_paths
    assert "/src/main.py" not in root_paths
    assert "/src/utils/helper.py" not in root_paths

    src_listing = be.ls_info("/src/")
    src_paths = [fi["path"] for fi in src_listing]
    assert "/src/main.py" in src_paths
    assert "/src/utils/" in src_paths
    assert "/src/utils/helper.py" not in src_paths

    utils_listing = be.ls_info("/src/utils/")
    utils_paths = [fi["path"] for fi in utils_listing]
    assert "/src/utils/helper.py" in utils_paths
    assert "/src/utils/common.py" in utils_paths
    assert len(utils_paths) == 2

    empty_listing = be.ls_info("/nonexistent/")
    assert empty_listing == []


def test_filesystem_backend_ls_normal_mode_nested(tmp_path: Path):
    """Test ls_info with nested directories in normal (non-virtual) mode."""
    root = tmp_path

    files = {
        root / "file1.txt": "content1",
        root / "subdir" / "file2.txt": "content2",
        root / "subdir" / "nested" / "file3.txt": "content3",
    }

    for path, content in files.items():
        write_file(path, content)

    be = FilesystemBackend(root_dir=str(root), virtual_mode=False)

    root_listing = be.ls_info(str(root))
    root_paths = [fi["path"] for fi in root_listing]

    assert str(root / "file1.txt") in root_paths
    assert str(root / "subdir") + "/" in root_paths
    assert str(root / "subdir" / "file2.txt") not in root_paths

    subdir_listing = be.ls_info(str(root / "subdir"))
    subdir_paths = [fi["path"] for fi in subdir_listing]
    assert str(root / "subdir" / "file2.txt") in subdir_paths
    assert str(root / "subdir" / "nested") + "/" in subdir_paths
    assert str(root / "subdir" / "nested" / "file3.txt") not in subdir_paths


def test_filesystem_backend_ls_trailing_slash(tmp_path: Path):
    """Test ls_info edge cases for filesystem backend."""
    root = tmp_path

    files = {
        root / "file.txt": "content",
        root / "dir" / "nested.txt": "nested",
    }

    for path, content in files.items():
        write_file(path, content)

    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    listing_with_slash = be.ls_info("/")
    assert len(listing_with_slash) > 0

    listing = be.ls_info("/")
    paths = [fi["path"] for fi in listing]
    assert paths == sorted(paths)

    listing1 = be.ls_info("/dir/")
    listing2 = be.ls_info("/dir")
    assert len(listing1) == len(listing2)
    assert [fi["path"] for fi in listing1] == [fi["path"] for fi in listing2]

    empty = be.ls_info("/nonexistent/")
    assert empty == []


def test_filesystem_backend_intercept_large_tool_result(tmp_path: Path):
    """Test that FilesystemBackend properly handles large tool result interception."""
    root = tmp_path
    rt = ToolRuntime(
        state={"messages": [], "files": {}},
        context=None,
        tool_call_id="test_fs",
        store=None,
        stream_writer=lambda _: None,
        config={},
    )

    middleware = FilesystemMiddleware(
        backend=lambda r: FilesystemBackend(root_dir=str(root), virtual_mode=True),
        tool_token_limit_before_evict=1000,
    )

    large_content = "f" * 5000
    tool_message = ToolMessage(content=large_content, tool_call_id="test_fs_123")
    result = middleware._intercept_large_tool_result(tool_message, rt)

    assert isinstance(result, ToolMessage)
    assert "Tool result too large" in result.content
    assert "/large_tool_results/test_fs_123" in result.content
    saved_file = root / "large_tool_results" / "test_fs_123"
    assert saved_file.exists()
    assert saved_file.read_text() == large_content


def test_filesystem_upload_single_file(tmp_path: Path):
    """Test uploading a single binary file."""
    root = tmp_path
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    test_path = "/test_upload.bin"
    test_content = b"Hello, Binary World!"

    responses = be.upload_files([(test_path, test_content)])

    assert len(responses) == 1
    assert responses[0].path == test_path
    assert responses[0].error is None

    # Verify file exists and content matches
    uploaded_file = root / "test_upload.bin"
    assert uploaded_file.exists()
    assert uploaded_file.read_bytes() == test_content


def test_filesystem_upload_multiple_files(tmp_path: Path):
    """Test uploading multiple files in one call."""
    root = tmp_path
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    files = [
        ("/file1.bin", b"Content 1"),
        ("/file2.bin", b"Content 2"),
        ("/subdir/file3.bin", b"Content 3"),
    ]

    responses = be.upload_files(files)

    assert len(responses) == 3
    for i, (path, _content) in enumerate(files):
        assert responses[i].path == path
        assert responses[i].error is None

    # Verify all files created
    assert (root / "file1.bin").read_bytes() == b"Content 1"
    assert (root / "file2.bin").read_bytes() == b"Content 2"
    assert (root / "subdir" / "file3.bin").read_bytes() == b"Content 3"


def test_filesystem_download_single_file(tmp_path: Path):
    """Test downloading a single file."""
    root = tmp_path
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    # Create a file manually
    test_file = root / "test_download.bin"
    test_content = b"Download me!"
    test_file.write_bytes(test_content)

    responses = be.download_files(["/test_download.bin"])

    assert len(responses) == 1
    assert responses[0].path == "/test_download.bin"
    assert responses[0].content == test_content
    assert responses[0].error is None


def test_filesystem_download_multiple_files(tmp_path: Path):
    """Test downloading multiple files in one call."""
    root = tmp_path
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    # Create several files
    files = {
        root / "file1.txt": b"File 1",
        root / "file2.txt": b"File 2",
        root / "subdir" / "file3.txt": b"File 3",
    }

    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    paths = ["/file1.txt", "/file2.txt", "/subdir/file3.txt"]
    responses = be.download_files(paths)

    assert len(responses) == 3
    assert responses[0].path == "/file1.txt"
    assert responses[0].content == b"File 1"
    assert responses[0].error is None

    assert responses[1].path == "/file2.txt"
    assert responses[1].content == b"File 2"
    assert responses[1].error is None

    assert responses[2].path == "/subdir/file3.txt"
    assert responses[2].content == b"File 3"
    assert responses[2].error is None


def test_filesystem_upload_download_roundtrip(tmp_path: Path):
    """Test upload followed by download for data integrity."""
    root = tmp_path
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    # Test with binary content including special bytes
    test_path = "/roundtrip.bin"
    test_content = bytes(range(256))  # All possible byte values

    # Upload
    upload_responses = be.upload_files([(test_path, test_content)])
    assert upload_responses[0].error is None

    # Download
    download_responses = be.download_files([test_path])
    assert download_responses[0].error is None
    assert download_responses[0].content == test_content


def test_filesystem_download_errors(tmp_path: Path):
    """Test download error handling."""
    root = tmp_path
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    # Test file_not_found
    responses = be.download_files(["/nonexistent.txt"])
    assert len(responses) == 1
    assert responses[0].path == "/nonexistent.txt"
    assert responses[0].content is None
    assert responses[0].error == "file_not_found"

    # Test is_directory
    (root / "testdir").mkdir()
    responses = be.download_files(["/testdir"])
    assert responses[0].error == "is_directory"
    assert responses[0].content is None

    # Test invalid_path (path traversal)
    responses = be.download_files(["/../etc/passwd"])
    assert len(responses) == 1
    assert responses[0].error == "invalid_path"
    assert responses[0].content is None


def test_filesystem_upload_errors(tmp_path: Path):
    """Test upload error handling."""
    root = tmp_path
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    # Test invalid_path (path traversal)
    responses = be.upload_files([("/../bad/path.txt", b"content")])
    assert len(responses) == 1
    assert responses[0].error == "invalid_path"


def test_filesystem_partial_success_upload(tmp_path: Path):
    """Test partial success in batch upload."""
    root = tmp_path
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    files = [
        ("/valid1.txt", b"Valid content 1"),
        ("/../invalid.txt", b"Invalid path"),  # Path traversal
        ("/valid2.txt", b"Valid content 2"),
    ]

    responses = be.upload_files(files)

    assert len(responses) == 3
    # First file should succeed
    assert responses[0].error is None
    assert (root / "valid1.txt").exists()

    # Second file should fail
    assert responses[1].error == "invalid_path"

    # Third file should still succeed (partial success)
    assert responses[2].error is None
    assert (root / "valid2.txt").exists()


def test_filesystem_partial_success_download(tmp_path: Path):
    """Test partial success in batch download."""
    root = tmp_path
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    # Create one valid file
    valid_file = root / "exists.txt"
    valid_content = b"I exist!"
    valid_file.write_bytes(valid_content)

    paths = ["/exists.txt", "/doesnotexist.txt", "/../invalid"]
    responses = be.download_files(paths)

    assert len(responses) == 3

    # First should succeed
    assert responses[0].error is None
    assert responses[0].content == valid_content

    # Second should fail with file_not_found
    assert responses[1].error == "file_not_found"
    assert responses[1].content is None

    # Third should fail with invalid_path
    assert responses[2].error == "invalid_path"
    assert responses[2].content is None


def test_filesystem_upload_to_existing_directory_path(tmp_path: Path):
    """Test uploading to a path where the target is an existing directory.

    This simulates trying to overwrite a directory with a file, which should
    produce an error. For example, if /mydir/ exists as a directory, trying
    to upload a file to /mydir should fail.
    """
    root = tmp_path
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    # Create a directory
    (root / "existing_dir").mkdir()

    # Try to upload a file with the same name as the directory
    # Note: on Unix systems, this will likely succeed but create a different inode
    # The behavior depends on the OS and filesystem. Let's just verify we get a response.
    responses = be.upload_files([("/existing_dir", b"file content")])

    assert len(responses) == 1
    assert responses[0].path == "/existing_dir"
    # Depending on OS behavior, this might succeed or fail
    # We're just documenting the behavior exists


def test_filesystem_upload_parent_is_file(tmp_path: Path):
    """Test uploading to a path where a parent component is a file, not a directory.

    For example, if /somefile.txt exists as a file, trying to upload to
    /somefile.txt/child.txt should fail because somefile.txt is not a directory.
    """
    root = tmp_path
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    # Create a file
    parent_file = root / "parent.txt"
    parent_file.write_text("I am a file, not a directory")

    # Try to upload a file as if parent.txt were a directory
    responses = be.upload_files([("/parent.txt/child.txt", b"child content")])

    assert len(responses) == 1
    assert responses[0].path == "/parent.txt/child.txt"
    # This should produce some kind of error since parent.txt is a file
    assert responses[0].error is not None


def test_filesystem_download_directory_as_file(tmp_path: Path):
    """Test that downloading a directory returns is_directory error.

    This is already tested in test_filesystem_download_errors but we add
    an explicit test case to make it clear this is a supported error scenario.
    """
    root = tmp_path
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    # Create a directory
    (root / "mydir").mkdir()

    # Try to download the directory as if it were a file
    responses = be.download_files(["/mydir"])

    assert len(responses) == 1
    assert responses[0].path == "/mydir"
    assert responses[0].content is None
    assert responses[0].error == "is_directory"


@pytest.mark.parametrize(
    ("pattern", "expected_file"),
    [
        ("def __init__(", "test1.py"),  # Parentheses (not regex grouping)
        ("str | int", "test2.py"),  # Pipe (not regex OR)
        ("[a-z]", "test3.py"),  # Brackets (not character class)
        ("(.*)", "test3.py"),  # Multiple special chars
        ("$19.99", "test4.txt"),  # Dot and $ (not "any character")
        ("user@example", "test4.txt"),  # @ character (literal)
    ],
)
def test_grep_literal_search_with_special_chars(tmp_path: Path, pattern: str, expected_file: str) -> None:
    """Test that grep treats patterns as literal strings, not regex.

    Tests with both ripgrep (if available) and Python fallback.
    """
    root = tmp_path

    # Create test files with special regex characters
    (root / "test1.py").write_text("def __init__(self, arg):\n    pass")
    (root / "test2.py").write_text("@overload\ndef func(x: str | int):\n    return x")
    (root / "test3.py").write_text("pattern = r'[a-z]+'\nregex_chars = '(.*)'")
    (root / "test4.txt").write_text("Price: $19.99\nEmail: user@example.com")

    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    # Test literal search with the pattern (uses ripgrep if available, otherwise Python fallback)
    matches = be.grep_raw(pattern, path="/")
    assert isinstance(matches, list)
    assert any(expected_file in m["path"] for m in matches), f"Pattern '{pattern}' not found in {expected_file}"


class TestToVirtualPath:
    """Tests for FilesystemBackend._to_virtual_path."""

    def test_returns_forward_slash_relative_path(self, tmp_path: Path):
        """Nested path is returned as forward-slash virtual path."""
        (tmp_path / "src").mkdir()
        be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
        result = be._to_virtual_path(tmp_path / "src" / "file.py")
        assert result == "/src/file.py"

    def test_cwd_itself_returns_slash_dot(self, tmp_path: Path):
        """Cwd path returns `/.` since `Path('.').as_posix()` is `'.'`."""
        be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
        result = be._to_virtual_path(tmp_path)
        assert result == "/."

    def test_outside_cwd_raises_value_error(self, tmp_path: Path):
        """Path outside cwd raises ValueError."""
        sub = tmp_path / "sub"
        sub.mkdir()
        be = FilesystemBackend(root_dir=str(sub), virtual_mode=True)
        with pytest.raises(ValueError, match="is not in the subpath of"):
            be._to_virtual_path(tmp_path / "outside.txt")


class TestWindowsPathHandling:
    """Tests that virtual-mode paths always use forward slashes."""

    @pytest.fixture
    def backend(self, tmp_path: Path):
        """Create a backend with nested directories."""
        (tmp_path / "src" / "utils").mkdir(parents=True)
        (tmp_path / "src" / "main.py").write_text("print('main')")
        (tmp_path / "src" / "utils" / "helper.py").write_text("def help(): pass")
        return FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)

    def test_ls_info_paths(self, backend):
        """ls_info should return forward-slash paths."""
        infos = backend.ls_info("/src")
        for info in infos:
            assert "\\" not in info["path"], f"Backslash in ls_info path: {info['path']}"

    def test_glob_info_paths(self, backend):
        """glob_info should return forward-slash paths."""
        result = backend.glob_info("**/*.py", path="/")
        assert isinstance(result, list)
        for info in result:
            assert "\\" not in info["path"], f"Backslash in glob_info path: {info['path']}"

    def test_grep_raw_paths(self, backend):
        """grep_raw should return forward-slash paths."""
        matches = backend.grep_raw("def", path="/")
        assert isinstance(matches, list)
        for m in matches:
            assert "\\" not in m["path"], f"Backslash in grep_raw path: {m['path']}"

    def test_deeply_nested_path(self, tmp_path: Path):
        """Deeply nested paths should still use forward slashes."""
        deep = tmp_path / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "file.txt").write_text("content")
        be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
        infos = be.ls_info("/a/b/c/d")
        for info in infos:
            assert "\\" not in info["path"], f"Backslash in deep path: {info['path']}"


# --- Hardening: S11 (unbounded read OOM guard) + S12 (generic OSError batch resiliency) ---


def test_read_rejects_file_exceeding_max_size(tmp_path: Path):
    """read() must not buffer a file larger than max_file_size_bytes into memory (S11)."""
    root = tmp_path
    big = root / "big.log"
    # max_file_size_mb=0 makes the limit 0 bytes, so any non-empty file trips the guard
    # without writing gigabytes to disk.
    big.write_text("line1\nline2\nline3\n", encoding="utf-8")
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True, max_file_size_mb=0)

    result = be.read("/big.log", limit=100)
    assert "exceeds the maximum readable size" in result
    # Actionable guidance toward grep / tighter range.
    assert "grep" in result.lower()


def test_read_allows_file_within_max_size(tmp_path: Path):
    """read() still works normally for files under the size guard (S11 regression)."""
    root = tmp_path
    small = root / "small.txt"
    small.write_text("hello\nworld\n", encoding="utf-8")
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True, max_file_size_mb=10)

    result = be.read("/small.txt")
    assert "hello" in result
    assert "world" in result


def test_download_files_rejects_file_exceeding_max_size(tmp_path: Path):
    """download_files() must short-circuit oversized files instead of buffering them (S11)."""
    root = tmp_path
    big = root / "big.bin"
    big.write_bytes(b"some bytes")
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True, max_file_size_mb=0)

    responses = be.download_files(["/big.bin"])
    assert len(responses) == 1
    assert responses[0].content is None
    assert responses[0].error == "invalid_path"


def test_download_files_generic_oserror_does_not_abort_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A plain OSError (e.g. ELOOP/ENXIO/EIO) on one file must not abort the batch (S12)."""
    import os as _os

    root = tmp_path
    good1 = root / "good1.txt"
    good2 = root / "good2.txt"
    bad = root / "bad.dev"
    good1.write_text("a", encoding="utf-8")
    good2.write_text("b", encoding="utf-8")
    bad.write_text("c", encoding="utf-8")

    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    real_open = _os.open

    def fake_open(path: object, *args: object, **kwargs: object) -> int:
        # Raise a *generic* OSError (not one of the explicitly-caught subclasses)
        # only for the bad file, mimicking O_NOFOLLOW on a symlink / device node.
        if str(path).endswith("bad.dev"):
            raise OSError("simulated ELOOP")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(_os, "open", fake_open)

    responses = be.download_files(["/good1.txt", "/bad.dev", "/good2.txt"])

    assert len(responses) == 3
    assert responses[0].error is None
    assert responses[0].content == b"a"
    # The offending file degrades gracefully rather than raising out of the batch.
    assert responses[1].error == "invalid_path"
    assert responses[1].content is None
    # Subsequent file still processed (partial-success contract preserved).
    assert responses[2].error is None
    assert responses[2].content == b"b"


def test_edit_preserves_original_on_mid_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P12: a crash during edit()'s write must leave the original file intact.

    The legacy in-place writer opened the target O_TRUNC and then wrote, so an
    interrupt/ENOSPC after the truncate emptied the file. The temp-file +
    os.replace path must keep the original content if the write blows up.
    """
    import os as _os

    root = tmp_path
    target = root / "data.txt"
    original = "ORIGINAL CONTENT THAT MUST SURVIVE\nline2\n"
    write_file(target, original)

    be = FilesystemBackend(root_dir=str(root), virtual_mode=False)

    real_replace = _os.replace

    def boom_replace(src: object, dst: object, *args: object, **kwargs: object) -> None:
        raise OSError("simulated ENOSPC during replace")

    monkeypatch.setattr(_os, "replace", boom_replace)

    result = be.edit(str(target), "ORIGINAL", "MUTATED", replace_all=False)

    # The edit reports the failure...
    assert isinstance(result, EditResult)
    assert result.error is not None
    # ...and crucially the original file is byte-for-byte intact (not truncated/emptied).
    assert target.read_text(encoding="utf-8") == original

    # No stray temp file left behind.
    monkeypatch.setattr(_os, "replace", real_replace)
    leftovers = [p.name for p in root.iterdir() if p.name != "data.txt"]
    assert leftovers == [], f"temp file leaked: {leftovers}"


def test_edit_mid_write_failure_during_write_preserves_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P12: a failure while writing the temp file (before replace) must not touch the original."""
    import os as _os

    root = tmp_path
    target = root / "data.txt"
    original = "KEEP ME\n"
    write_file(target, original)

    be = FilesystemBackend(root_dir=str(root), virtual_mode=False)

    real_write = _os.write
    real_fsync = _os.fsync

    # Force the data-write phase to fail (fsync is the easiest deterministic hook
    # that runs after the bytes are buffered but before replace).
    def boom_fsync(fd: int) -> None:
        raise OSError("simulated I/O error during fsync")

    monkeypatch.setattr(_os, "fsync", boom_fsync)

    result = be.edit(str(target), "KEEP", "DROP", replace_all=False)

    assert isinstance(result, EditResult)
    assert result.error is not None
    assert target.read_text(encoding="utf-8") == original

    monkeypatch.setattr(_os, "fsync", real_fsync)
    leftovers = [p.name for p in root.iterdir() if p.name != "data.txt"]
    assert leftovers == [], f"temp file leaked: {leftovers}"
    # sanity: real os.write/os.fsync untouched
    assert _os.write is real_write


def test_edit_succeeds_and_replaces_content(tmp_path: Path) -> None:
    """P12: the happy path still produces the edited content (no regression)."""
    root = tmp_path
    target = root / "data.txt"
    write_file(target, "alpha beta gamma\n")

    be = FilesystemBackend(root_dir=str(root), virtual_mode=False)
    result = be.edit(str(target), "beta", "DELTA", replace_all=False)

    assert isinstance(result, EditResult)
    assert result.error is None
    assert result.occurrences == 1
    assert target.read_text(encoding="utf-8") == "alpha DELTA gamma\n"
    # No temp residue.
    assert [p.name for p in root.iterdir()] == ["data.txt"]


def test_upload_files_preserves_original_on_replace_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P12: upload_files overwriting an existing file must not lose it on a mid-write crash."""
    import os as _os

    root = tmp_path
    target = root / "blob.bin"
    original = b"\x00\x01ORIGINAL-BYTES\x02"
    target.write_bytes(original)

    be = FilesystemBackend(root_dir=str(root), virtual_mode=False)

    real_replace = _os.replace

    def boom_replace(src: object, dst: object, *args: object, **kwargs: object) -> None:
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(_os, "replace", boom_replace)

    responses = be.upload_files([(str(target), b"NEW-CONTENT")])

    assert len(responses) == 1
    assert responses[0].error is not None
    # Original bytes survive the failed overwrite.
    assert target.read_bytes() == original

    monkeypatch.setattr(_os, "replace", real_replace)
    leftovers = [p.name for p in root.iterdir() if p.name != "blob.bin"]
    assert leftovers == [], f"temp file leaked: {leftovers}"


def test_upload_files_atomic_overwrite_succeeds(tmp_path: Path) -> None:
    """P12: upload_files happy path overwrites an existing file with the new bytes."""
    root = tmp_path
    target = root / "blob.bin"
    target.write_bytes(b"old")

    be = FilesystemBackend(root_dir=str(root), virtual_mode=False)
    responses = be.upload_files([(str(target), b"brand-new")])

    assert responses[0].error is None
    assert target.read_bytes() == b"brand-new"
    assert [p.name for p in root.iterdir()] == ["blob.bin"]


def test_atomic_write_refuses_symlink_destination(tmp_path: Path) -> None:
    """P12: O_NOFOLLOW protection preserved — _atomic_write must not overwrite a symlink target.

    Skipped on platforms without O_NOFOLLOW (Windows), where the guard is a no-op
    by design.
    """
    import os as _os

    if not hasattr(_os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW unavailable on this platform")

    root = tmp_path
    secret = root / "secret.txt"
    secret.write_text("do-not-touch", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")

    be = FilesystemBackend(root_dir=str(root), virtual_mode=False)

    with pytest.raises(OSError, match="symlink"):
        be._atomic_write(link, "evil")

    # The symlink target is untouched.
    assert secret.read_text(encoding="utf-8") == "do-not-touch"


# --- Python grep fallback: include-glob semantics (nested + Windows separators) ---


@pytest.fixture
def python_fallback_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FilesystemBackend:
    """Backend whose grep always takes the Python fallback path, over a nested tree."""
    write_file(tmp_path / "src" / "app" / "main.py", "needle in nested python file\n")
    write_file(tmp_path / "top.py", "needle at the top level\n")
    write_file(tmp_path / "src" / "app" / "notes.txt", "needle in a text file\n")

    monkeypatch.setattr(FilesystemBackend, "_ripgrep_search", lambda *_args, **_kwargs: None)
    return FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)


def test_python_search_include_glob_matches_nested_file(python_fallback_backend: FilesystemBackend) -> None:
    """`*.py` must match a file at any depth, not just the search root.

    Regression: `_python_search` matched the include glob against the whole
    root-relative path, so `*.py` (no separator) never matched `src/app/main.py`.
    Ripgrep includes it, so grep results silently differed depending on whether
    `rg` happened to be installed.
    """
    matches = python_fallback_backend.grep_raw("needle", path="/", glob="*.py")

    paths = {m["path"] for m in matches}
    assert "/src/app/main.py" in paths
    assert "/top.py" in paths
    # The include glob still excludes non-matching files.
    assert "/src/app/notes.txt" not in paths


def test_python_search_include_glob_with_directory_component(python_fallback_backend: FilesystemBackend) -> None:
    """A glob with a `/` matches against the path relative to the search root."""
    matches = python_fallback_backend.grep_raw("needle", path="/", glob="src/**/*.py")

    paths = {m["path"] for m in matches}
    assert paths == {"/src/app/main.py"}


def test_grep_include_glob_matcher_normalizes_windows_separators() -> None:
    r"""The include-glob matcher must accept the backslash paths Windows produces.

    Regression: `_python_search` fed `str(fp.relative_to(root))` straight to
    `wcglob.globmatch`. On Windows that is `src\app\main.py`, which never matches
    a POSIX-style glob on a POSIX host — so the same backend gave different grep
    results per platform. `compile_grep_include_glob` normalizes separators, and
    the backend now goes through it.
    """
    windows_rel_path = "src" + chr(92) + "app" + chr(92) + "main.py"

    assert compile_grep_include_glob("src/**/*.py")(windows_rel_path)
    assert compile_grep_include_glob("*.py")(windows_rel_path)


def test_python_search_returns_untruncated_on_clean_walk(python_fallback_backend: FilesystemBackend) -> None:
    """A search that completes within its budget reports `truncated=False`."""
    result = python_fallback_backend.grep("needle", path="/")

    assert isinstance(result, GrepResult)
    assert result.error is None
    assert result.truncated is False
    assert result.matches


def test_python_search_is_bounded_by_wall_clock_budget(python_fallback_backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_python_search` must abandon a slow walk instead of running unbounded."""
    real_monotonic = time.monotonic
    # First call sets the deadline; every later call is far past it.
    calls = {"n": 0}

    def creeping_monotonic() -> float:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_monotonic()
        return real_monotonic() + DEFAULT_GREP_TIMEOUT + 1

    monkeypatch.setattr(filesystem_module.time, "monotonic", creeping_monotonic)

    result = python_fallback_backend.grep("needle", path="/")

    assert result.truncated is True
    # Partial results stay usable rather than being discarded.
    assert result.error is None


def test_ripgrep_search_uses_default_grep_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ripgrep subprocess must use the shared `DEFAULT_GREP_TIMEOUT`, not a hardcoded 30s."""
    write_file(tmp_path / "a.txt", "hello\n")
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    be.grep("hello", path="/")

    assert seen["timeout"] == DEFAULT_GREP_TIMEOUT


# --- Structured (Result-returning) API ---


def test_ls_returns_ls_result(tmp_path: Path) -> None:
    """`ls` returns an `LsResult`; a missing directory yields empty entries, not an error."""
    write_file(tmp_path / "a.txt", "a")
    (tmp_path / "dir").mkdir()
    be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)

    result = be.ls("/")
    assert isinstance(result, LsResult)
    assert result.error is None
    assert {e["path"] for e in result.entries or []} == {"/a.txt", "/dir/"}

    missing = be.ls("/nope")
    assert missing.error is None
    assert missing.entries == []


def test_read_file_returns_sliced_file_data(tmp_path: Path) -> None:
    """`read_file` returns raw sliced `FileData`, not the line-numbered rendering."""
    write_file(tmp_path / "f.txt", "one\ntwo\nthree\nfour\n")
    be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)

    result = be.read_file("/f.txt", offset=1, limit=2)
    assert isinstance(result, ReadResult)
    assert result.error is None
    assert result.file_data is not None
    assert result.file_data["content"] == "two\nthree\n"
    assert result.file_data["encoding"] == "utf-8"

    # The rendered legacy view still numbers from the requested offset.
    rendered = be.read("/f.txt", offset=1, limit=2)
    assert "     2\ttwo" in rendered
    assert "     3\tthree" in rendered


def test_read_file_missing_and_offset_errors(tmp_path: Path) -> None:
    """`read_file` reports failures through `error` rather than raising."""
    write_file(tmp_path / "f.txt", "one\n")
    be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)

    assert "not found" in (be.read_file("/missing.txt").error or "")
    assert "exceeds file length" in (be.read_file("/f.txt", offset=50).error or "")


def test_read_file_empty_file(tmp_path: Path) -> None:
    """An empty file yields empty content, which the legacy view renders as the reminder."""
    write_file(tmp_path / "empty.txt", "")
    be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)

    result = be.read_file("/empty.txt")
    assert result.error is None
    assert result.file_data is not None
    assert result.file_data["content"] == ""
    assert "empty contents" in be.read("/empty.txt")


def test_grep_and_glob_return_results(tmp_path: Path) -> None:
    """`grep` / `glob` return `GrepResult` / `GlobResult` with a `truncated` flag."""
    write_file(tmp_path / "src" / "main.py", "import os\n")
    be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)

    grep_result = be.grep("import", path="/")
    assert isinstance(grep_result, GrepResult)
    assert grep_result.truncated is False
    assert any(m["path"] == "/src/main.py" for m in grep_result.matches or [])

    glob_result = be.glob("*.py", path="/")
    assert isinstance(glob_result, GlobResult)
    assert glob_result.truncated is False
    assert [m["path"] for m in glob_result.matches or []] == ["/src/main.py"]

    # `path=None` falls back to the backend root.
    assert be.glob("*.py").matches == glob_result.matches

    # A missing search root is an empty result, not an error.
    assert be.glob("*.py", path="/nope").matches == []


def test_glob_is_bounded_by_wall_clock_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`glob` must abandon a slow walk and report partial results as truncated."""
    write_file(tmp_path / "a.py", "a")
    write_file(tmp_path / "b.py", "b")
    be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)

    real_monotonic = time.monotonic
    calls = {"n": 0}

    def creeping_monotonic() -> float:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_monotonic()
        return real_monotonic() + DEFAULT_GLOB_TIMEOUT + 1

    monkeypatch.setattr(filesystem_module.time, "monotonic", creeping_monotonic)

    result = be.glob("*.py", path="/")
    assert result.truncated is True


def test_glob_backend_budget_below_middleware_deadline() -> None:
    """The backend's own budget must expire before the middleware abandons the call.

    Otherwise the middleware's timeout fires first and the partial results the
    backend was about to return are thrown away.
    """
    assert DEFAULT_GLOB_TIMEOUT < GLOB_TIMEOUT


# --- Binary read path ---


def test_read_file_binary_returns_base64(tmp_path: Path) -> None:
    """Non-text files are returned whole as base64 rather than UTF-8 decoded."""
    raw = bytes(range(256))
    (tmp_path / "img.png").write_bytes(raw)
    be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)

    result = be.read_file("/img.png")
    assert result.error is None
    assert result.file_data is not None
    assert result.file_data["encoding"] == "base64"
    assert base64.standard_b64decode(result.file_data["content"]) == raw


def test_read_file_rejects_oversized_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A binary file above `MAX_BINARY_BYTES` is refused instead of being buffered."""
    (tmp_path / "img.png").write_bytes(b"\x00" * 64)
    monkeypatch.setattr(filesystem_module, "MAX_BINARY_BYTES", 8)
    be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)

    result = be.read_file("/img.png")
    assert result.file_data is None
    assert "exceeds the maximum readable size" in (result.error or "")


def test_read_file_treats_mkv_as_binary(tmp_path: Path) -> None:
    """`.mkv` is classified as video by `_get_backend_read_file_type`, so it must not text-decode."""
    (tmp_path / "clip.mkv").write_bytes(b"\x1f\x43\xb6\x75\xff\xfe")
    be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)

    result = be.read_file("/clip.mkv")
    assert result.error is None
    assert result.file_data is not None
    assert result.file_data["encoding"] == "base64"


def test_legacy_read_refuses_binary(tmp_path: Path) -> None:
    """The rendered `read` must not dump base64 into the caller's context."""
    (tmp_path / "img.png").write_bytes(b"\x89PNG\r\n")
    be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)

    assert "is binary" in be.read("/img.png")


# --- delete ---


def test_supports_delete() -> None:
    """`supports_delete` detects the override so callers can guard on it."""
    assert supports_delete(FilesystemBackend(root_dir=".", virtual_mode=True))


def test_delete_file(tmp_path: Path) -> None:
    """Deleting a file unlinks it and reports it in `deleted_paths`."""
    write_file(tmp_path / "f.txt", "bye")
    be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)

    result = be.delete("/f.txt")
    assert isinstance(result, DeleteResult)
    assert result.error is None
    assert result.path == "/f.txt"
    assert result.files_update is None
    assert result.deleted_paths == ["/f.txt"]
    assert not (tmp_path / "f.txt").exists()


def test_delete_directory_is_recursive(tmp_path: Path) -> None:
    """Deleting a directory removes it and everything under it."""
    write_file(tmp_path / "pkg" / "a.py", "a")
    write_file(tmp_path / "pkg" / "sub" / "b.py", "b")
    write_file(tmp_path / "keep.txt", "keep")
    be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)

    result = be.delete("/pkg")
    assert result.error is None
    assert result.path == "/pkg"
    assert result.deleted_paths == ["/pkg/a.py", "/pkg/sub/b.py"]
    assert not (tmp_path / "pkg").exists()
    assert (tmp_path / "keep.txt").exists()


def test_delete_missing_path_errors(tmp_path: Path) -> None:
    """Deleting a nonexistent path is an error, not a silent no-op."""
    be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)

    result = be.delete("/nope.txt")
    assert result.path is None
    assert "not found" in (result.error or "")


def test_delete_cannot_escape_root(tmp_path: Path) -> None:
    """A traversal path is refused by `_resolve_path` and reported as an error."""
    outside = tmp_path / "outside.txt"
    outside.write_text("do-not-touch", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    be = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    result = be.delete("/../outside.txt")
    assert result.path is None
    assert "traversal" in (result.error or "")
    assert outside.read_text(encoding="utf-8") == "do-not-touch"


def test_delete_symlink_does_not_follow_into_target(tmp_path: Path) -> None:
    """Deleting a symlink removes the link, never the directory it points at."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")

    be = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    result = be.delete("/link")

    assert result.error is None
    assert not link.exists()
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_write_overwrites_existing_file(tmp_path: Path) -> None:
    """`write` overwrites an existing file rather than erroring.

    This is the upstream deepagents contract and must agree with every other backend --
    a `CompositeBackend` mixing a `FilesystemBackend` default with a `StateBackend` would
    otherwise accept or reject the same `write` call based only on the routed prefix.
    """
    backend = FilesystemBackend(root_dir=str(tmp_path))
    (tmp_path / "notes.txt").write_text("original", encoding="utf-8")

    result = backend.write("/notes.txt", "replaced")

    assert result.error is None
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "replaced"


def test_write_overwrite_does_not_follow_symlink(tmp_path: Path) -> None:
    """Overwriting must not write *through* a symlink (O_NOFOLLOW survives the guard removal)."""
    backend = FilesystemBackend(root_dir=str(tmp_path))
    target = tmp_path / "target.txt"
    target.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")

    result = backend.write("/link.txt", "attacker")

    assert result.error is not None
    assert target.read_text(encoding="utf-8") == "secret"
