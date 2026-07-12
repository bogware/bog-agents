"""Pin the shared backend helpers in `bog_agents.backends.utils`.

These primitives (glob compilation, path normalization, v1/v2 `FileData` conversion,
read slicing, regex hinting) are the substrate every backend builds on. They also
carry two behavioral fixes worth pinning explicitly:

1. Grep splits v2 `str` content into LINES — iterating the raw value would walk one
   character at a time.
2. A `FileData` without `modified_at` must not `KeyError` in the glob sort.
"""

from typing import Any

import pytest

from bog_agents.backends.protocol import ReadResult
from bog_agents.backends.utils import (
    EMPTY_CONTENT_WARNING,
    _get_backend_read_file_type,
    _get_file_type,
    _glob_anchor,
    _glob_search_files,
    _grep_search_files,
    _looks_like_regex,
    _normalize_content,
    _paths_overlap,
    _relative_to_root,
    _to_legacy_file_data,
    compile_grep_include_glob,
    compile_recursive_glob,
    create_file_data,
    file_data_to_string,
    grep_matches_from_files,
    regex_literal_hint,
    slice_read_response,
    to_posix_path,
)


class TestCompileRecursiveGlob:
    """`compile_recursive_glob` mirrors `Path.rglob`: the pattern matches at any depth."""

    def test_bare_pattern_matches_top_level_file(self) -> None:
        assert compile_recursive_glob("*.py")("main.py") is True

    def test_bare_pattern_matches_nested_file(self) -> None:
        # The `**/` prefix is the whole point: `*.py` must reach `src/app/main.py`.
        assert compile_recursive_glob("*.py")("src/app/main.py") is True

    def test_non_matching_extension(self) -> None:
        assert compile_recursive_glob("*.py")("src/app/main.ts") is False

    def test_accepts_windows_backslash_relative_path(self) -> None:
        assert compile_recursive_glob("*.py")("src\\app\\main.py") is True

    def test_dotfiles_are_matched(self) -> None:
        # DOTMATCH: wcmatch excludes dotfiles by default, stdlib rglob does not.
        assert compile_recursive_glob("*.yaml")(".github/workflows/ci.yaml") is True

    def test_brace_expansion(self) -> None:
        matcher = compile_recursive_glob("*.{py,ts}")
        assert matcher("src/main.py") is True
        assert matcher("src/main.ts") is True
        assert matcher("src/main.md") is False

    def test_leading_slash_is_stripped(self) -> None:
        assert compile_recursive_glob("/*.py")("src/main.py") is True

    def test_explicit_directory_pattern(self) -> None:
        matcher = compile_recursive_glob("src/**/*.py")
        assert matcher("src/app/main.py") is True
        assert matcher("tests/app/main.py") is False


class TestCompileGrepIncludeGlob:
    """`compile_grep_include_glob` mirrors ripgrep include-glob semantics."""

    def test_directory_pattern_matches_nested_path(self) -> None:
        assert compile_grep_include_glob("src/**/*.py")("src/app/main.py") is True

    def test_directory_pattern_rejects_other_root(self) -> None:
        assert compile_grep_include_glob("src/**/*.py")("tests/app/main.py") is False

    def test_slashless_pattern_matches_basename_at_any_depth(self) -> None:
        matcher = compile_grep_include_glob("*.py")
        assert matcher("main.py") is True
        assert matcher("src/app/main.py") is True

    def test_leading_slash_anchors_to_search_root(self) -> None:
        # `/*.py` narrows: top-level only, not basename-at-any-depth.
        matcher = compile_grep_include_glob("/*.py")
        assert matcher("top.py") is True
        assert matcher("src/app/main.py") is False

    def test_accepts_windows_backslash_relative_path(self) -> None:
        assert compile_grep_include_glob("src/**/*.py")("src\\app\\main.py") is True
        assert compile_grep_include_glob("*.py")("src\\app\\main.py") is True


class TestToPosixPath:
    def test_normalizes_backslashes(self) -> None:
        assert to_posix_path("src\\app\\main.py") == "src/app/main.py"

    def test_forward_slash_path_unchanged(self) -> None:
        assert to_posix_path("src/app/main.py") == "src/app/main.py"

    def test_windows_drive_path(self) -> None:
        assert to_posix_path("C:\\Users\\a\\b.txt") == "C:/Users/a/b.txt"


class TestNormalizeContent:
    """`_normalize_content` is the single v1 -> v2 conversion point."""

    def test_v2_str_content_passthrough(self) -> None:
        assert _normalize_content({"content": "alpha\nbeta", "encoding": "utf-8"}) == "alpha\nbeta"

    def test_tolerates_legacy_list_content_and_joins_it(self) -> None:
        with pytest.warns(DeprecationWarning, match="v1 format"):
            result = _normalize_content({"content": ["alpha", "beta", "gamma"]})
        assert result == "alpha\nbeta\ngamma"

    def test_legacy_list_content_warns_but_does_not_raise(self) -> None:
        with pytest.warns(DeprecationWarning, match="v1 format"):
            _normalize_content({"content": ["alpha"]})

    def test_v2_content_does_not_warn(self) -> None:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            assert _normalize_content({"content": "alpha"}) == "alpha"

    def test_missing_content_key_defaults_to_empty_string(self) -> None:
        assert _normalize_content({}) == ""

    def test_file_data_to_string_delegates(self) -> None:
        assert file_data_to_string({"content": "x", "encoding": "utf-8"}) == "x"


class TestToLegacyFileData:
    """`_to_legacy_file_data` converts v2 back down to the v1 storage shape."""

    def test_round_trips_v2_to_v1(self) -> None:
        v2 = create_file_data("alpha\nbeta\ngamma")
        v1 = _to_legacy_file_data(v2)

        assert v1["content"] == ["alpha", "beta", "gamma"]
        assert "encoding" not in v1
        assert v1["created_at"] == v2["created_at"]
        assert v1["modified_at"] == v2["modified_at"]

        with pytest.warns(DeprecationWarning, match="v1 format"):
            assert _normalize_content(v1) == "alpha\nbeta\ngamma"

    def test_already_v1_content_passes_through_as_a_copy(self) -> None:
        original = ["alpha", "beta"]
        v1 = _to_legacy_file_data({"content": original})
        assert v1["content"] == original
        assert v1["content"] is not original

    def test_omits_absent_timestamps(self) -> None:
        v1 = _to_legacy_file_data({"content": "x", "encoding": "utf-8"})
        assert "created_at" not in v1
        assert "modified_at" not in v1


class TestSliceReadResponse:
    def test_slices_requested_window(self) -> None:
        file_data = create_file_data("l1\nl2\nl3\nl4\nl5")
        assert slice_read_response(file_data, offset=1, limit=2) == "l2\nl3\n"

    def test_slice_to_end_has_no_trailing_newline(self) -> None:
        file_data = create_file_data("l1\nl2\nl3")
        assert slice_read_response(file_data, offset=0, limit=100) == "l1\nl2\nl3"

    def test_offset_beyond_file_length_returns_read_result_error(self) -> None:
        file_data = create_file_data("l1\nl2")
        result = slice_read_response(file_data, offset=9, limit=10)
        assert isinstance(result, ReadResult)
        assert result.error is not None
        assert "exceeds file length" in result.error

    def test_empty_content_returns_content_not_error(self) -> None:
        assert slice_read_response({"content": "", "encoding": "utf-8"}, offset=0, limit=10) == ""

    def test_crlf_is_normalized_to_lf_in_the_window(self) -> None:
        result = slice_read_response({"content": "l1\r\nl2\r\nl3", "encoding": "utf-8"}, offset=0, limit=2)
        assert result == "l1\nl2\n"

    def test_accepts_legacy_v1_file_data(self) -> None:
        with pytest.warns(DeprecationWarning, match="v1 format"):
            result = slice_read_response({"content": ["l1", "l2", "l3"]}, offset=1, limit=1)
        assert result == "l2\n"


class TestRegexLiteralHint:
    @pytest.mark.parametrize("pattern", ["foo|bar", "def .*self", "a.+b", r"\.py", r"\d+", r"\bword\b"])
    def test_detects_regex_signals(self, pattern: str) -> None:
        assert _looks_like_regex(pattern) is True
        hint = regex_literal_hint(pattern)
        assert hint is not None
        assert "literal" in hint

    @pytest.mark.parametrize("pattern", ["self.tools", "def __init__(self):", "arr[0]", "TODO", "x?y", "^start"])
    def test_literal_code_searches_are_not_flagged(self, pattern: str) -> None:
        assert _looks_like_regex(pattern) is False
        assert regex_literal_hint(pattern) is None


class TestRelativeToRoot:
    def test_root_search_strips_leading_slash(self) -> None:
        assert _relative_to_root("/src/app/main.py", "/") == "src/app/main.py"

    def test_subdirectory_search_root(self) -> None:
        assert _relative_to_root("/src/app/main.py", "/src") == "app/main.py"

    def test_exact_file_search_root_returns_basename(self) -> None:
        assert _relative_to_root("/src/main.py", "/src/main.py") == "main.py"


class TestGlobAnchorAndOverlap:
    @pytest.mark.parametrize(
        ("pattern", "expected"),
        [
            ("/secrets/**", "/secrets"),
            ("/a/*/b", "/a"),
            ("/**/secrets", "/"),
            ("/*/foo", "/"),
            ("/a/b/c.txt", "/a/b/c.txt"),
        ],
    )
    def test_glob_anchor(self, pattern: str, expected: str) -> None:
        assert _glob_anchor(pattern) == expected

    def test_overlap_is_component_wise_not_substring(self) -> None:
        assert _paths_overlap("/secret", "/secrets") is False
        assert _paths_overlap("/secrets/key", "/secrets") is True
        assert _paths_overlap("/secrets", "/secrets/key") is True
        assert _paths_overlap("/anything", "/") is True


class TestFileTypeClassification:
    def test_text_default(self) -> None:
        assert _get_file_type("/src/main.py") == "text"

    def test_known_multimodal_extensions(self) -> None:
        assert _get_file_type("/a.png") == "image"
        assert _get_file_type("/a.MP4") == "video"
        assert _get_file_type("/a.pdf") == "file"

    def test_mkv_is_text_for_the_shared_map_but_video_for_backend_reads(self) -> None:
        # Backends must take the BINARY path for .mkv or they corrupt the bytes.
        assert _get_file_type("/clip.mkv") == "text"
        assert _get_backend_read_file_type("/clip.mkv") == "video"

    def test_backend_read_type_matches_shared_map_otherwise(self) -> None:
        assert _get_backend_read_file_type("/src/main.py") == "text"
        assert _get_backend_read_file_type("/a.png") == "image"


class TestGrepSplitsLinesNotCharacters:
    """Regression: v2 stores `content` as `str`; iterating it would walk one character at a time."""

    def test_grep_matches_from_files_reports_whole_lines(self) -> None:
        files: dict[str, Any] = {"/src/main.py": create_file_data("alpha\nbeta\ngamma")}
        matches = grep_matches_from_files(files, "beta", "/")

        assert matches == [{"path": "/src/main.py", "line": 2, "text": "beta"}]

    def test_grep_matches_from_files_line_numbers_are_1_indexed_by_line(self) -> None:
        files: dict[str, Any] = {"/a.txt": create_file_data("x\nx\nx\nneedle")}
        matches = grep_matches_from_files(files, "needle", "/")
        assert [m["line"] for m in matches] == [4]

    def test_grep_search_files_content_mode_reports_whole_lines(self) -> None:
        files: dict[str, Any] = {"/a.py": create_file_data("import os\nprint('hi')")}
        output = _grep_search_files(files, "import", "/", output_mode="content")
        assert output == "/a.py:\n  1: import os"

    def test_grep_include_glob_filters_files(self) -> None:
        files: dict[str, Any] = {
            "/src/main.py": create_file_data("needle"),
            "/src/main.ts": create_file_data("needle"),
        }
        matches = grep_matches_from_files(files, "needle", "/", glob="*.py")
        assert [m["path"] for m in matches] == ["/src/main.py"]


class TestFileDataWithoutModifiedAtDoesNotKeyError:
    """Regression: `modified_at` is `NotRequired` — hand-built fixtures must not blow up the sort."""

    def test_glob_search_files_tolerates_missing_modified_at(self) -> None:
        files: dict[str, Any] = {
            "/a.py": {"content": "x", "encoding": "utf-8"},
            "/b.py": {"content": "y", "encoding": "utf-8", "modified_at": "2026-01-01T00:00:00+00:00"},
        }
        result = _glob_search_files(files, "*.py", "/")
        assert sorted(result.split("\n")) == ["/a.py", "/b.py"]

    def test_glob_search_files_sorts_stamped_entries_most_recent_first(self) -> None:
        files: dict[str, Any] = {
            "/old.py": {"content": "x", "modified_at": "2020-01-01T00:00:00+00:00"},
            "/new.py": {"content": "y", "modified_at": "2026-01-01T00:00:00+00:00"},
        }
        assert _glob_search_files(files, "*.py", "/").split("\n") == ["/new.py", "/old.py"]


class TestEmptyContentWarningIsStable:
    """The empty-file sentinel is asserted by name across backends and the read middleware."""

    def test_message(self) -> None:
        assert EMPTY_CONTENT_WARNING == "System reminder: File exists but has empty contents"
