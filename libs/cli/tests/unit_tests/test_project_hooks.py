"""Tests for ``bog_agents_cli.project_hooks``."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from bog_agents_cli.project_hooks import (
    HookDecision,
    _discover_event_dir,
    _discover_hooks_dir,
    _list_event_scripts,
    hooks_fingerprint,
    is_hooks_execution_allowed,
    run_hooks,
)

WIN = sys.platform == "win32"


@pytest.fixture(autouse=True)
def _trust_hooks_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behavioral tests below exercise hook *execution*, not the trust gate;
    opt in via the env escape hatch so the deny-by-default gate (P0-8) doesn't
    skip them. The dedicated trust-gate tests clear this explicitly.
    """
    monkeypatch.setenv("BOG_AGENTS_TRUST_PROJECT_HOOKS", "1")


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    if not WIN:
        st = path.stat()
        path.chmod(st.st_mode | stat.S_IXUSR | stat.S_IRUSR)


def test_discover_hooks_dir_returns_none_for_missing(tmp_path: Path) -> None:
    assert _discover_hooks_dir(tmp_path) is None


def test_discover_hooks_dir_returns_path_when_present(tmp_path: Path) -> None:
    target = tmp_path / ".bog-agents" / "hooks"
    target.mkdir(parents=True)
    assert _discover_hooks_dir(tmp_path) == target


def test_event_dir_aliases_resolve(tmp_path: Path) -> None:
    """``pre_tool`` and ``pretool`` are accepted aliases for ``pre-tool``."""
    base = tmp_path / ".bog-agents" / "hooks"
    base.mkdir(parents=True)
    (base / "pre_tool").mkdir()
    assert _discover_event_dir(tmp_path, "pre-tool") == base / "pre_tool"


def test_list_event_scripts_skips_hidden_and_underscored(tmp_path: Path) -> None:
    event_dir = tmp_path / "pre-tool"
    event_dir.mkdir()
    _write_executable(event_dir / "01-real.sh", "#!/usr/bin/env bash\necho '{}'\n")
    _write_executable(event_dir / "_helper.sh", "#!/usr/bin/env bash\necho '{}'\n")
    (event_dir / ".hidden").write_text("nope")
    scripts = _list_event_scripts(event_dir)
    if WIN:
        # On Windows the executable bit isn't honoured; but the .sh suffix
        # qualifies and underscore/dot files are filtered by name.
        names = {p.name for p in scripts}
        assert "01-real.sh" in names
        assert "_helper.sh" not in names
        assert ".hidden" not in names
    else:
        assert [p.name for p in scripts] == ["01-real.sh"]


@pytest.mark.skipif(WIN, reason="POSIX shebang required for this test")
async def test_run_hooks_block_short_circuits_subsequent_scripts(
    tmp_path: Path,
) -> None:
    base = tmp_path / ".bog-agents" / "hooks" / "pre-tool"
    base.mkdir(parents=True)
    _write_executable(
        base / "01-deny.sh",
        '#!/usr/bin/env bash\necho \'{"action":"block","reason":"nope"}\'\n',
    )
    _write_executable(
        base / "02-runs-anyway.sh",
        '#!/usr/bin/env bash\necho \'{"action":"modify","args":{"x":1}}\'\nexit 1\n',
    )
    decision = await run_hooks(
        "pre-tool", {"tool_name": "execute"}, project_root=tmp_path
    )
    assert decision.blocked is True
    assert "nope" in decision.reason
    assert decision.modified_args is None


@pytest.mark.skipif(WIN, reason="POSIX shebang required for this test")
async def test_run_hooks_modify_args_chains(tmp_path: Path) -> None:
    base = tmp_path / ".bog-agents" / "hooks" / "pre-tool"
    base.mkdir(parents=True)
    _write_executable(
        base / "01-rewrite.sh",
        '#!/usr/bin/env bash\necho \'{"action":"modify","args":{"command":"safer"}}\'\n',
    )
    decision = await run_hooks(
        "pre-tool",
        {"tool_name": "execute", "tool_args": {"command": "rm -rf /"}},
        project_root=tmp_path,
    )
    assert decision.allowed
    assert decision.modified_args == {"command": "safer"}


async def test_run_hooks_no_dir_returns_passthrough(tmp_path: Path) -> None:
    decision = await run_hooks("pre-tool", {"x": 1}, project_root=tmp_path)
    assert isinstance(decision, HookDecision)
    assert decision.allowed
    assert decision.modified_args is None
    assert decision.modified_prompt is None


@pytest.mark.skipif(WIN, reason="POSIX shebang required for this test")
async def test_run_hooks_user_prompt_modify_returns_new_prompt(tmp_path: Path) -> None:
    base = tmp_path / ".bog-agents" / "hooks" / "user-prompt"
    base.mkdir(parents=True)
    _write_executable(
        base / "01-prepend.sh",
        '#!/usr/bin/env bash\necho \'{"action":"modify","prompt":"PREFIX: hi"}\'\n',
    )
    decision = await run_hooks("user-prompt", {"prompt": "hi"}, project_root=tmp_path)
    assert decision.modified_prompt == "PREFIX: hi"


async def test_run_hooks_uses_bog_agents_project_root_env_for_default_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no project_root is passed, env override picks the dir."""
    monkeypatch.setenv("BOG_AGENTS_PROJECT_ROOT", str(tmp_path))
    decision = await run_hooks("pre-tool", {"x": 1})
    # Empty project — no hooks registered.
    assert decision.allowed


# ---------------------------------------------------------------------------
# Trust gate (REVIEW.md v2 P0-8) — hooks from an untrusted cloned repo must
# NOT execute until the user trusts them.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(WIN, reason="POSIX shebang required for this test")
async def test_untrusted_project_hooks_do_not_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Clear the autouse trust env so the real deny-by-default gate applies.
    monkeypatch.delenv("BOG_AGENTS_TRUST_PROJECT_HOOKS", raising=False)
    # Point the trust store at an isolated, empty config.
    from bog_agents_cli import mcp_trust

    monkeypatch.setattr(mcp_trust, "_DEFAULT_CONFIG_PATH", tmp_path / "trust.toml")

    base = tmp_path / ".bog-agents" / "hooks" / "pre-tool"
    base.mkdir(parents=True)
    _write_executable(
        base / "01-deny.sh",
        '#!/usr/bin/env bash\necho \'{"action":"block","reason":"evil"}\'\n',
    )
    # The malicious block hook must be SKIPPED (allowed passthrough), not run.
    decision = await run_hooks(
        "pre-tool", {"tool_name": "execute"}, project_root=tmp_path
    )
    assert decision.blocked is False
    assert decision.allowed


@pytest.mark.skipif(WIN, reason="POSIX shebang required for this test")
async def test_trusted_project_hooks_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOG_AGENTS_TRUST_PROJECT_HOOKS", raising=False)
    from bog_agents_cli import mcp_trust

    cfg = tmp_path / "trust.toml"
    monkeypatch.setattr(mcp_trust, "_DEFAULT_CONFIG_PATH", cfg)

    base = tmp_path / ".bog-agents" / "hooks" / "pre-tool"
    base.mkdir(parents=True)
    _write_executable(
        base / "01-deny.sh",
        '#!/usr/bin/env bash\necho \'{"action":"block","reason":"policy"}\'\n',
    )
    # Trust the project at its current hook fingerprint, then the hook runs.
    fp = hooks_fingerprint(tmp_path)
    root = str(tmp_path.resolve())  # noqa: ASYNC240 — tmp_path in a test, not real async I/O
    assert mcp_trust.trust_project_hooks(root, fp, config_path=cfg)
    assert is_hooks_execution_allowed(tmp_path)
    decision = await run_hooks(
        "pre-tool", {"tool_name": "execute"}, project_root=tmp_path
    )
    assert decision.blocked is True
    assert "policy" in decision.reason


@pytest.mark.skipif(WIN, reason="POSIX shebang required for this test")
async def test_editing_a_hook_revokes_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOG_AGENTS_TRUST_PROJECT_HOOKS", raising=False)
    from bog_agents_cli import mcp_trust

    cfg = tmp_path / "trust.toml"
    monkeypatch.setattr(mcp_trust, "_DEFAULT_CONFIG_PATH", cfg)

    base = tmp_path / ".bog-agents" / "hooks" / "pre-tool"
    base.mkdir(parents=True)
    script = base / "01.sh"
    _write_executable(script, '#!/usr/bin/env bash\necho \'{"action":"allow"}\'\n')
    root = str(tmp_path.resolve())  # noqa: ASYNC240 — tmp_path in a test, not real async I/O
    mcp_trust.trust_project_hooks(root, hooks_fingerprint(tmp_path), config_path=cfg)
    assert is_hooks_execution_allowed(tmp_path)
    # Tamper with the script — trust must no longer hold.
    _write_executable(
        script, '#!/usr/bin/env bash\necho \'{"action":"block","reason":"x"}\'\n'
    )
    assert is_hooks_execution_allowed(tmp_path) is False
