"""ROADMAP #67: self-review memo, --since-last / --effort, dispositions → lessons, the /self-review body."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from bog_agents_cli import self_review_memo as memo
from bog_agents_cli.self_review_controller import (
    generate_self_review_prompt,
    parse_self_review_args,
    run_resolve,
    run_self_review,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


class TestParser:
    def test_flags_before_and_after_scope(self) -> None:
        t = parse_self_review_args("--since-last --effort high --branch main --fix")
        assert (t.scope, t.ref, t.fix, t.since_last, t.effort) == (
            "branch",
            "main",
            True,
            True,
            "high",
        )
        t = parse_self_review_args(
            "--branch develop --since-last"
        )  # flags after --branch used to be dropped
        assert t.since_last and t.scope == "branch"
        t = parse_self_review_args("--effort=custom:'never flag docstrings' --staged")
        assert t.scope == "staged" and t.effort == "custom:never flag docstrings"
        assert parse_self_review_args("").scope == "working"
        with pytest.raises(ValueError, match="unknown effort"):
            parse_self_review_args("--effort maximal")

    def test_prompt_carries_effort_and_lessons(self) -> None:
        target = parse_self_review_args("--effort high")
        text = generate_self_review_prompt(
            target,
            lessons="## Rulings from previous reviews (do not repeat these)\n\n- a.py:3: false positive",
        )
        assert "Effort: HIGH" in text and "Rulings from previous reviews" in text
        assert "Effort:" not in generate_self_review_prompt(parse_self_review_args(""))


class TestMemo:
    def test_fingerprint_tracks_the_review_text(self, repo: Path) -> None:
        assert memo.diff_fingerprint(memo.review_diff_text(repo)) == ""  # clean tree
        (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        working = memo.diff_fingerprint(memo.review_diff_text(repo))
        assert working
        (repo / "new.py").write_text("y = 1\n", encoding="utf-8")
        assert (
            memo.diff_fingerprint(memo.review_diff_text(repo)) != working
        )  # untracked files count
        _git(repo, "add", "a.py")
        staged = memo.diff_fingerprint(memo.review_diff_text(repo, scope="staged"))
        assert staged == working  # the same change, now staged, hashes the same text
        assert memo.current_branch(repo) == "main"

    def test_round_trip_skip_and_markers(self, tmp_path: Path) -> None:
        record = memo.SelfReviewMemo(
            branch="feat/x",
            scope="working",
            base="",
            diff_sha="a" * 64,
            effort="default",
        )
        path = memo.save_memo(tmp_path, record)
        assert path.parent == memo.memo_dir(tmp_path) and path.name == "feat_x.json"
        loaded = memo.load_memo(tmp_path, "feat/x")
        assert loaded is not None and loaded.diff_sha == "a" * 64
        assert memo.should_skip(loaded, diff_sha="a" * 64, effort="default")
        assert not memo.should_skip(
            loaded, diff_sha="a" * 64, effort="high"
        )  # asking for more effort → review again
        assert not memo.should_skip(loaded, diff_sha="b" * 64, effort="default")
        assert memo.should_skip(
            memo.SelfReviewMemo("b", "working", "", "c" * 64, effort="high"),
            diff_sha="c" * 64,
            effort="default",
        )
        assert memo.load_memo(tmp_path, "nope") is None
        marker = memo.marker_comment("a" * 64)
        assert (
            marker == "<!-- bog-review:aaaaaaaaaaaa -->"
            and memo.parse_marker(f"body\n{marker}\nmore") == "a" * 12
        )
        assert memo.parse_marker("no marker") is None
        assert memo.normalize_effort('custom: "be strict"') == "custom:be strict"
        assert memo.effort_rule("default") == "" and "user" in memo.effort_rule(
            "custom:no nits"
        )


class TestDispositions:
    def test_record_load_and_lessons(self, tmp_path: Path) -> None:
        memo.record_disposition(
            tmp_path,
            "a.py:12",
            "incorrect",
            note="that branch is unreachable",
            branch="main",
        )
        memo.record_disposition(tmp_path, "b.py:3", "addressed")
        memo.record_disposition(tmp_path, "c.py:8", "wontfix", note="legacy API")
        with pytest.raises(ValueError, match="disposition must be one of"):
            memo.record_disposition(tmp_path, "x", "maybe")
        rows = memo.load_dispositions(tmp_path)
        assert [r.finding_id for r in rows] == ["a.py:12", "b.py:3", "c.py:8"]
        block = memo.lessons_block(rows)
        assert (
            "c.py:8: accepted as-is — legacy API" in block
            and "a.py:12: false positive" in block
            and "b.py:3" not in block
        )
        assert memo.lessons_block([]) == ""


class _Mount:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def __call__(self, widget: object) -> None:
        self.messages.append(str(widget))


class TestSelfReviewBody:
    async def test_memo_skip_and_resolve(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli.widgets import messages

        monkeypatch.setattr(messages, "AppMessage", lambda text: f"APP:{text}")
        sent: list[str] = []

        async def _send(prompt: str) -> None:
            sent.append(prompt)

        app = SimpleNamespace(
            _cwd=str(repo), _mount_message=_Mount(), _send_prompt_to_agent=_send
        )
        (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
        await run_self_review(app, "--since-last")
        assert len(sent) == 1 and "Self-Review Gate" in sent[0]
        assert "bog-review:" in app._mount_message.messages[-1]
        await run_self_review(app, "--since-last")
        assert (
            len(sent) == 1
            and "Skipped: this exact diff" in app._mount_message.messages[-1]
        )
        await run_self_review(
            app, "--since-last --effort high"
        )  # more effort → runs again
        assert len(sent) == 2 and "Effort: HIGH" in sent[1]
        await run_self_review(app, "")  # no --since-last → always runs
        assert len(sent) == 3
        await run_resolve(app, "a.py:1 incorrect the constant is intentional")
        assert "Recorded a.py:1 as incorrect" in app._mount_message.messages[-1]
        await run_self_review(app, "")
        assert "a.py:1: false positive — the constant is intentional" in sent[-1]
        await run_resolve(app, "")
        assert (
            "Usage: /finding" in app._mount_message.messages[-1]
            and "a.py:1: incorrect" in app._mount_message.messages[-1]
        )
        await run_self_review(app, "--effort nope")
        assert "unknown effort" in app._mount_message.messages[-1]
