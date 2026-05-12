"""Smoke + behaviour tests for the slash-command "killer feature" modules.

These cover the modules added in the killer-features branch:
``handoff``, ``release_train``, ``imagine``, ``devil``, ``squad``,
``scratch``, ``proxy_tools``, ``dream``, ``whisper``. Each module is
tested at the boundary that's most likely to regress.

No real LLM calls are made; we test parsers, persistence round-trips,
and slash-command registry shape.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# /release-train
# ---------------------------------------------------------------------------


class TestReleaseTrainParser:
    """``parse_commit_line`` correctly buckets Conventional Commits."""

    def test_feat_with_scope(self) -> None:
        from bog_agents_cli.release_train import parse_commit_line

        c = parse_commit_line("abcd123 feat(cli): add /scratch command")
        assert c.sha == "abcd123"
        assert c.type == "features"
        assert c.scope == "cli"
        assert c.subject == "add /scratch command"
        assert c.breaking is False

    def test_breaking_feat(self) -> None:
        from bog_agents_cli.release_train import parse_commit_line

        c = parse_commit_line("deadbeef feat(sdk)!: rip out legacy_feature_flags")
        assert c.type == "breaking"
        assert c.breaking is True
        assert c.scope == "sdk"

    def test_fix_without_scope(self) -> None:
        from bog_agents_cli.release_train import parse_commit_line

        c = parse_commit_line("0001 fix: handle empty input")
        assert c.type == "fixes"
        assert c.scope == ""

    def test_unknown_format_falls_to_other(self) -> None:
        from bog_agents_cli.release_train import parse_commit_line

        c = parse_commit_line("aaaa just a free-form commit message")
        assert c.type == "other"

    def test_pr_number_extracted(self) -> None:
        from bog_agents_cli.release_train import parse_commit_line

        c = parse_commit_line("aaaa feat(cli): land the picker overhaul (#76)")
        assert c.pr_number == 76


class TestReleaseTrainResolveRange:
    def test_explicit_range_passthrough(self, tmp_path: Path) -> None:
        from bog_agents_cli.release_train import resolve_range

        from_ref, to_ref, label = resolve_range("v0.8.5..v0.8.6", cwd=tmp_path)
        assert from_ref == "v0.8.5"
        assert to_ref == "v0.8.6"
        assert label == "v0.8.5..v0.8.6"

    def test_invalid_range_raises(self, tmp_path: Path) -> None:
        from bog_agents_cli.release_train import resolve_range

        with pytest.raises(ValueError, match="invalid range"):
            resolve_range("..v0.8.6", cwd=tmp_path)


# ---------------------------------------------------------------------------
# /imagine
# ---------------------------------------------------------------------------


class TestImagineParseArgs:
    def test_n_and_prompt(self) -> None:
        from bog_agents_cli.imagine import parse_args

        n, problem = parse_args("4 how should we cache PR diffs?", "")
        assert n == 4
        assert problem == "how should we cache PR diffs?"

    def test_no_n_falls_back_to_default(self) -> None:
        from bog_agents_cli.imagine import parse_args

        n, problem = parse_args("how should we cache?", "")
        assert n == 3
        assert problem == "how should we cache?"

    def test_n_clamped_to_max(self) -> None:
        from bog_agents_cli.imagine import ANGLES, parse_args

        n, _ = parse_args("99 something", "")
        assert n == len(ANGLES)

    def test_n_floored_at_2(self) -> None:
        from bog_agents_cli.imagine import parse_args

        n, _ = parse_args("1 alone", "")
        assert n == 2

    def test_empty_arg_falls_back_to_transcript(self) -> None:
        from bog_agents_cli.imagine import parse_args

        n, problem = parse_args("", "what should we do about caching?")
        assert n == 3
        assert "caching" in problem

    def test_empty_with_no_transcript_raises(self) -> None:
        from bog_agents_cli.imagine import parse_args

        with pytest.raises(ValueError, match="no problem"):
            parse_args("", "")


# ---------------------------------------------------------------------------
# /squad
# ---------------------------------------------------------------------------


class TestSquadConfigRoundTrip:
    def test_default_personas_are_three(self, tmp_path: Path) -> None:
        from bog_agents_cli.squad import load_squad, write_default_squad

        cfg = tmp_path / "squad.toml"
        write_default_squad(cfg)
        personas = load_squad(cfg)
        assert {p.name for p in personas} == {"Alice", "Bob", "Carol"}

    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        from bog_agents_cli.squad import load_squad

        personas = load_squad(tmp_path / "absent.toml")
        assert len(personas) == 3

    def test_overwrite_protection(self, tmp_path: Path) -> None:
        from bog_agents_cli.squad import write_default_squad

        cfg = tmp_path / "squad.toml"
        write_default_squad(cfg)
        with pytest.raises(FileExistsError):
            write_default_squad(cfg)

    def test_overwrite_true(self, tmp_path: Path) -> None:
        from bog_agents_cli.squad import write_default_squad

        cfg = tmp_path / "squad.toml"
        write_default_squad(cfg)
        write_default_squad(cfg, overwrite=True)
        assert cfg.exists()

    def test_load_string_value(self, tmp_path: Path) -> None:
        """Personas in ``[personas]`` can be raw strings (not just tables)."""
        from bog_agents_cli.squad import load_squad

        cfg = tmp_path / "squad.toml"
        cfg.write_text(
            '[personas]\nalice = "You are a code reviewer."\n', encoding="utf-8"
        )
        personas = load_squad(cfg)
        assert len(personas) == 1
        assert personas[0].name == "alice"
        assert "code reviewer" in personas[0].system_prompt


# ---------------------------------------------------------------------------
# /proxy
# ---------------------------------------------------------------------------


class TestProxyDefinition:
    def test_validate_rejects_undeclared_arg(self) -> None:
        from bog_agents_cli.proxy_tools import ProxyDefinition, ProxyError

        d = ProxyDefinition(
            name="find",
            description="search",
            command="rg -n -- {pattern} .",
            args=[],
        )
        with pytest.raises(ProxyError, match="undeclared args"):
            d.validate()

    def test_validate_rejects_bad_name(self) -> None:
        from bog_agents_cli.proxy_tools import ProxyDefinition, ProxyError

        d = ProxyDefinition(name="9bad", description="x", command="echo hi")
        with pytest.raises(ProxyError, match="must match"):
            d.validate()

    def test_render_command_quotes_values(self) -> None:
        from bog_agents_cli.proxy_tools import ProxyDefinition, render_command

        d = ProxyDefinition(
            name="search",
            description="search the repo",
            command="rg -n -- {pattern} .",
            args=["pattern"],
        )
        rendered = render_command(d, {"pattern": "; rm -rf $HOME ;"})
        if sys.platform != "win32":
            tokens = shlex.split(rendered)
            assert "; rm -rf $HOME ;" in tokens
            assert tokens[0] == "rg"
            assert tokens[1] == "-n"

    def test_render_command_missing_arg_raises(self) -> None:
        from bog_agents_cli.proxy_tools import (
            ProxyDefinition,
            ProxyError,
            render_command,
        )

        d = ProxyDefinition(
            name="search",
            description="x",
            command="rg {pattern}",
            args=["pattern"],
        )
        with pytest.raises(ProxyError, match="requires args"):
            render_command(d, {})


class TestProxyParseAddArgs:
    def test_minimal_form(self) -> None:
        from bog_agents_cli.proxy_tools import parse_add_args

        d = parse_add_args(
            '--name listpods --cmd "kubectl get pods" --desc "List pods in current ns"'
        )
        assert d.name == "listpods"
        assert d.command == "kubectl get pods"
        assert d.description == "List pods in current ns"
        assert d.args == []
        assert d.timeout_seconds == 30

    def test_with_args_and_timeout(self) -> None:
        from bog_agents_cli.proxy_tools import parse_add_args

        d = parse_add_args(
            '--name search --cmd "rg -n -- {pattern} ." '
            '--desc "Search repo" --args pattern --timeout 12'
        )
        assert d.args == ["pattern"]
        assert d.timeout_seconds == 12

    def test_missing_name_raises(self) -> None:
        from bog_agents_cli.proxy_tools import ProxyError, parse_add_args

        with pytest.raises(ProxyError, match="--name"):
            parse_add_args('--cmd "ls" --desc "list"')


# ---------------------------------------------------------------------------
# /scratch
# ---------------------------------------------------------------------------


class TestScratchIndex:
    def test_find_by_exact_id(self) -> None:
        from bog_agents_cli.scratch import ScratchEntry, ScratchIndex

        e1 = ScratchEntry(scratch_id="aaaa1111", label="a", branch="b", path="/x")
        e2 = ScratchEntry(scratch_id="bbbb2222", label="b", branch="b", path="/y")
        index = ScratchIndex(entries=[e1, e2])
        assert index.find("aaaa1111") is e1
        assert index.find("bbbb2222") is e2

    def test_find_by_unique_prefix(self) -> None:
        from bog_agents_cli.scratch import ScratchEntry, ScratchIndex

        e1 = ScratchEntry(scratch_id="aaaa1111", label="a", branch="b", path="/x")
        e2 = ScratchEntry(scratch_id="bbbb2222", label="b", branch="b", path="/y")
        index = ScratchIndex(entries=[e1, e2])
        assert index.find("aaaa") is e1
        assert index.find("") is None

    def test_save_load_roundtrip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli import scratch as scratch_mod

        monkeypatch.setattr(scratch_mod, "scratch_dir", lambda: tmp_path)
        idx = scratch_mod.ScratchIndex(
            entries=[
                scratch_mod.ScratchEntry(
                    scratch_id="aabbccdd",
                    label="trial",
                    branch="scratch/aabbccdd",
                    path=str(tmp_path / "aabbccdd"),
                    parent_repo=str(tmp_path),
                    created_at=1.0,
                )
            ]
        )
        scratch_mod.save_index(idx)
        loaded = scratch_mod.load_index()
        assert len(loaded.entries) == 1
        assert loaded.entries[0].scratch_id == "aabbccdd"

    def test_load_missing_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli import scratch as scratch_mod

        monkeypatch.setattr(scratch_mod, "scratch_dir", lambda: tmp_path)
        loaded = scratch_mod.load_index()
        assert loaded.entries == []

    def test_load_malformed_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli import scratch as scratch_mod

        monkeypatch.setattr(scratch_mod, "scratch_dir", lambda: tmp_path)
        (tmp_path / "index.json").write_text("not-json", encoding="utf-8")
        loaded = scratch_mod.load_index()
        assert loaded.entries == []


# ---------------------------------------------------------------------------
# /dream
# ---------------------------------------------------------------------------


class TestDreamScanner:
    def test_finds_python_todo(self, tmp_path: Path) -> None:
        from bog_agents_cli.dream import DreamConfig, scan_for_todos

        (tmp_path / "module.py").write_text(
            "def f():\n    # TODO: handle empty input\n    return None\n",
            encoding="utf-8",
        )
        hits = scan_for_todos(
            tmp_path,
            DreamConfig(extensions=[".py"], excluded_paths=[]),
            max_hits=10,
        )
        assert len(hits) == 1
        assert "empty input" in hits[0].label

    def test_finds_typescript_fixme(self, tmp_path: Path) -> None:
        from bog_agents_cli.dream import DreamConfig, scan_for_todos

        (tmp_path / "x.ts").write_text(
            "// FIXME: race condition under high load\nfunction f() {}\n",
            encoding="utf-8",
        )
        hits = scan_for_todos(
            tmp_path,
            DreamConfig(extensions=[".ts"], excluded_paths=[]),
            max_hits=10,
        )
        assert len(hits) == 1
        assert "race condition" in hits[0].label

    def test_respects_extension_filter(self, tmp_path: Path) -> None:
        from bog_agents_cli.dream import DreamConfig, scan_for_todos

        (tmp_path / "should_be_skipped.rb").write_text(
            "# TODO: ruby file\n", encoding="utf-8"
        )
        (tmp_path / "found.py").write_text("# TODO: python\n", encoding="utf-8")
        hits = scan_for_todos(
            tmp_path,
            DreamConfig(extensions=[".py"], excluded_paths=[]),
            max_hits=10,
        )
        paths = [h.path.name for h in hits]
        assert "found.py" in paths
        assert "should_be_skipped.rb" not in paths

    def test_dream_config_round_trip(self, tmp_path: Path) -> None:
        from bog_agents_cli.dream import (
            DreamConfig,
            load_dream_config,
            save_dream_config,
        )

        cfg_path = tmp_path / "dream.toml"
        cfg = DreamConfig(
            model="anthropic:claude-haiku-4-5",
            n_targets=5,
            extensions=[".py", ".go"],
        )
        save_dream_config(cfg, path=cfg_path)
        loaded = load_dream_config(cfg_path)
        assert loaded.model == "anthropic:claude-haiku-4-5"
        assert loaded.n_targets == 5
        assert ".go" in loaded.extensions


# ---------------------------------------------------------------------------
# /whisper
# ---------------------------------------------------------------------------


class TestWhisperSession:
    def test_append_bounded(self, tmp_path: Path) -> None:
        from bog_agents_cli import whisper as wmod

        session = wmod.WhisperSession(
            started_at=0.0, duration_seconds=60.0, cwd=tmp_path
        )
        # 250 > _MAX_EVENTS = 200, so the buffer should cap.
        for i in range(250):
            session.append("edit", f"file{i}.py")
        assert len(session.events) == wmod._MAX_EVENTS
        # Oldest entries dropped — first surviving event has high index.
        first_idx = int(session.events[0].detail[len("file") : -3])
        assert first_idx >= 50

    def test_render_events_for_prompt_with_no_events(self, tmp_path: Path) -> None:
        from bog_agents_cli.whisper import (
            WhisperSession,
            render_events_for_prompt,
        )

        session = WhisperSession(started_at=0.0, duration_seconds=60.0, cwd=tmp_path)
        body = render_events_for_prompt(session, duration_minutes=10)
        assert "no observable activity" in body

    def test_render_events_for_prompt_with_events(self, tmp_path: Path) -> None:
        from bog_agents_cli.whisper import (
            WhisperSession,
            render_events_for_prompt,
        )

        session = WhisperSession(started_at=0.0, duration_seconds=60.0, cwd=tmp_path)
        session.append("edit", "a.py")
        session.append("commit", "abc123 added thing")
        body = render_events_for_prompt(session, duration_minutes=10)
        assert "edit: a.py" in body
        assert "commit: abc123 added thing" in body


# ---------------------------------------------------------------------------
# /handoff
# ---------------------------------------------------------------------------


class TestHandoffRender:
    def test_includes_branch_and_diff_when_present(self) -> None:
        from bog_agents_cli.feature_helpers import GitContext, TranscriptEntry
        from bog_agents_cli.handoff import render_session_for_handoff

        git = GitContext(
            branch="feat/x",
            head_sha="abc12345",
            is_dirty=True,
            modified_files=["a.py", "b.py"],
            recent_commits=["abc123 first commit"],
            diff_summary=" a.py | 4 ++--",
        )
        transcript = [
            TranscriptEntry(role="user", text="please refactor X"),
            TranscriptEntry(role="assistant", text="ok, I split it into Y/Z"),
        ]
        body = render_session_for_handoff(transcript, git)
        assert "feat/x" in body
        assert "abc12345" in body
        assert "a.py" in body
        assert "abc123 first commit" in body
        assert "refactor X" in body

    def test_author_voice_emits_hint(self) -> None:
        from bog_agents_cli.feature_helpers import GitContext
        from bog_agents_cli.handoff import render_session_for_handoff

        body = render_session_for_handoff(
            [], GitContext(branch="main"), author_voice="Bob"
        )
        assert "Bob" in body
        assert "voice" in body.lower()


# ---------------------------------------------------------------------------
# Slash-command registration smoke test
# ---------------------------------------------------------------------------


class TestSlashCommandRegistry:
    """The new commands are present, unique, and have wired-up handlers."""

    def test_all_new_commands_registered(self) -> None:
        from bog_agents_cli.commands import COMMANDS

        names = {cmd.spec.name for cmd in COMMANDS}
        for expected in (
            "/scratch",
            "/proxy",
            "/imagine",
            "/devil",
            "/squad",
            "/dream",
            "/release-train",
            "/whisper",
            "/handoff",
        ):
            assert expected in names, f"missing slash command: {expected}"

    def test_no_duplicate_command_names(self) -> None:
        from bog_agents_cli.commands import COMMANDS

        names = [cmd.spec.name for cmd in COMMANDS]
        dupes = [n for n in names if names.count(n) > 1]
        assert len(names) == len(set(names)), f"duplicate command names: {dupes}"

    def test_all_handlers_resolve_on_app(self) -> None:
        """Every registered command must reference a real method on BogAgentsApp."""
        from bog_agents_cli.app import BogAgentsApp
        from bog_agents_cli.commands import COMMANDS

        for cmd in COMMANDS:
            handler = cmd.handler_method
            if not handler:
                continue
            assert hasattr(BogAgentsApp, handler), (
                f"{cmd.spec.name} → BogAgentsApp.{handler} not found"
            )
