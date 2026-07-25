"""Daemon entrypoint CLI parsing (DEL-2 / v3 P0-10).

The quickstart and the documented systemd unit use `bog-agents-daemon run
--port 7878`, but the CLI implemented only `start/stop/status` and rejected a
`--port` that followed the subcommand — the unit crash-looped. `run` is now an
alias of `start`, and global flags parse in either position.
"""

from __future__ import annotations

import pytest

import bog_agents_daemon.main as main_mod


def _run(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> dict:
    seen: dict = {}
    monkeypatch.setattr(main_mod, "_cmd_start", lambda port, log_level: seen.update(start=(port, log_level)) or 0)
    monkeypatch.setattr(main_mod, "_cmd_stop", lambda port, *, force, wait_seconds: seen.update(stop=port) or 0)
    monkeypatch.setattr(main_mod, "_cmd_status", lambda: seen.update(status=True) or 0)
    monkeypatch.setattr(main_mod.sys, "argv", ["bog-agents-daemon", *argv])
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    seen["exit"] = exc.value.code
    return seen


class TestDaemonEntrypointCli:
    def test_run_is_alias_of_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _run(monkeypatch, ["run", "--port", "7878"])
        assert seen.get("start") == (7878, "INFO")
        assert seen["exit"] == 0

    def test_start_accepts_port_after_subcommand(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The documented `run --port N` form: a flag AFTER the subcommand.
        assert _run(monkeypatch, ["start", "--port", "9000"]).get("start") == (9000, "INFO")

    def test_port_before_subcommand_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _run(monkeypatch, ["--port", "7878", "start"]).get("start") == (7878, "INFO")

    def test_no_subcommand_defaults_to_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert "start" in _run(monkeypatch, [])

    def test_stop_accepts_port_after_subcommand(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _run(monkeypatch, ["stop", "--port", "5555"]).get("stop") == 5555

    def test_unknown_subcommand_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _run(monkeypatch, ["bogus"])["exit"] == 2


def test_docs_do_not_resurrect_phantom_daemon_subcommands() -> None:
    """Drift guard: the `job`/`runs` command families never existed on the
    `bog-agents-daemon` binary (job management lives on the `bog-agents daemon`
    CLI). Keep them out of the quickstart so it can't crash-loop a stranger."""
    from pathlib import Path

    # libs/daemon/tests/ -> repo root is parents[3].
    quickstart = Path(__file__).resolve().parents[3] / "docs" / "daemon" / "quickstart.md"
    if not quickstart.is_file():
        pytest.skip("quickstart doc not present in this checkout")
    text = quickstart.read_text(encoding="utf-8")
    for phantom in ("bog-agents-daemon job", "bog-agents-daemon runs"):
        assert phantom not in text, f"docs resurrected a phantom command: `{phantom} ...`"
