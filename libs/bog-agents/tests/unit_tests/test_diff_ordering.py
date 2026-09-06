"""ROADMAP #66: proof-ordered diffs."""

from __future__ import annotations

from bog_agents.diff_ordering import (
    FileChange,
    is_muted,
    is_test_path,
    rank_changes,
    render_ordered_stat,
    reorder_unified_diff,
    score,
    split_unified_diff,
)
from bog_agents.evidence import EvidenceBundle, render_evidence_markdown

GIT_DIFF = """diff --git a/uv.lock b/uv.lock
index 1..2 100644
--- a/uv.lock
+++ b/uv.lock
@@ -1,2 +1,3 @@
 a
+b
+c
diff --git a/tests/test_api.py b/tests/test_api.py
index 1..2 100644
--- a/tests/test_api.py
+++ b/tests/test_api.py
@@ -1,1 +1,3 @@
 import x
+def test_new():
+    pass
diff --git a/pkg/api.py b/pkg/api.py
index 1..2 100644
--- a/pkg/api.py
+++ b/pkg/api.py
@@ -1,1 +1,3 @@
 import os
+def public_thing():
+    return 1
diff --git a/README.md b/README.md
index 1..2 100644
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 # Readme
+note
"""


def test_split_counts_and_paths() -> None:
    changes = split_unified_diff(GIT_DIFF)
    assert [c.path for c in changes] == ["uv.lock", "tests/test_api.py", "pkg/api.py", "README.md"]
    assert (changes[2].added, changes[2].removed) == (2, 0)
    assert split_unified_diff("") == []


def test_difflib_style_diff_splits_too() -> None:
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n--- a/y.py\n+++ b/y.py\n@@ -1 +1,2 @@\n one\n+two\n"
    changes = split_unified_diff(diff)
    assert [c.path for c in changes] == ["x.py", "y.py"]
    assert (changes[0].added, changes[0].removed) == (1, 1)


def test_ranking_puts_signatures_first_and_lockfiles_last() -> None:
    ranked = rank_changes(split_unified_diff(GIT_DIFF))
    assert [c.path for c in ranked] == ["pkg/api.py", "README.md", "tests/test_api.py", "uv.lock"]
    assert score(FileChange("uv.lock")) == -80.0
    assert score(FileChange("src/main.py")) == 30.0
    assert score(FileChange("tests/test_x.py", diff="+def test_a():\n")) == -40.0
    assert is_muted("web/__snapshots__/a.snap") is True
    assert is_muted("out/dist/bundle.min.js") is True
    assert is_test_path("src/foo.spec.ts") is True
    assert is_test_path("src/foo.ts") is False


def test_reorder_and_render() -> None:
    reordered = reorder_unified_diff(GIT_DIFF)
    assert reordered.index("pkg/api.py") < reordered.index("README.md") < reordered.index("tests/test_api.py") < reordered.index("uv.lock")
    assert reorder_unified_diff("single block\n") == "single block\n"
    stat = render_ordered_stat(split_unified_diff(GIT_DIFF))
    assert stat.splitlines()[0] == " 1. pkg/api.py  +2/-0"
    assert stat.splitlines()[-1] == " 4. uv.lock  +2/-0  [muted]"
    assert "[test]" in stat


def test_evidence_markdown_lists_files_in_order() -> None:
    bundle = EvidenceBundle(diff_stat=" 4 files changed", diff=GIT_DIFF)
    text = render_evidence_markdown(bundle)
    assert "Files in explanatory order:" in text
    assert text.index(" 1. pkg/api.py") < text.index(" 4. uv.lock")
    assert " 4 files changed" in text
