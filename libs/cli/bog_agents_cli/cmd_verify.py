"""bog-agents verify — run a project's typecheck/lint/tests once.

Detects the project type (Node, Python, Rust, Go, Java) by looking for
signature files in the working directory, then runs the canonical
verification chain. Saves a `verification_summary.md` artifact the
agent can `read_file` and quote in its final report.

Per-project override: a `.bog-agents/verify.sh` (POSIX) or
`.bog-agents/verify.cmd` (Windows) file overrides auto-detection
entirely. The script gets `cwd=<project_root>` and its stdout/stderr
are captured into the summary verbatim.

Exit codes:
  0  — all configured checks passed
  1  — at least one check failed
  2  — no project type detected and no override script
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess  # noqa: S404
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass
class CheckResult:
    """Single verification step result."""

    name: str
    """Human-readable label, e.g. 'typecheck' or 'tests'."""

    command: str
    """The shell command that was run."""

    exit_code: int
    """Subprocess exit code; 0 == success."""

    duration_seconds: float
    """Wall time the check took."""

    output: str
    """Combined stdout + stderr, truncated for the summary file."""

    skipped_reason: str = ""
    """If non-empty, the check was skipped and no command ran."""


@dataclass
class ProjectProfile:
    """Auto-detected project verification profile."""

    language: str
    """Primary language label (node, python, rust, go, java, mixed, unknown)."""

    typecheck: str = ""
    """Typecheck command; empty when not applicable."""

    lint: str = ""
    """Lint command; empty when not applicable."""

    test: str = ""
    """Test command; empty when not applicable."""

    detected_via: list[str] = field(default_factory=list)
    """Indicator files that drove the detection (relative paths)."""


def _which(cmd: str) -> bool:
    """Return True when *cmd* is on PATH."""
    return shutil.which(cmd) is not None


def detect_project_profile(root: Path) -> ProjectProfile:
    """Detect the project's verification commands from indicator files.

    Order matters: first-detected wins for "language", but we still
    surface all matched indicators in `detected_via` so users can see
    why a particular profile was chosen.

    Args:
        root: Project root directory.

    Returns:
        Best-effort verification profile.
    """
    indicators = []
    typecheck = ""
    lint = ""
    test = ""
    language = "unknown"

    pkg_json = root / "package.json"
    pyproject = root / "pyproject.toml"
    cargo_toml = root / "Cargo.toml"
    go_mod = root / "go.mod"
    pom_xml = root / "pom.xml"
    setup_py = root / "setup.py"

    if pkg_json.is_file():
        indicators.append("package.json")
        language = "node"
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = pkg.get("scripts") or {}
            dev_deps = pkg.get("devDependencies") or {}
            deps = pkg.get("dependencies") or {}
        except (json.JSONDecodeError, OSError):
            scripts, dev_deps, deps = {}, {}, {}

        runner = "npx"
        # Detect package manager
        if (root / "pnpm-lock.yaml").is_file():
            runner = "pnpm exec"
        elif (root / "yarn.lock").is_file():
            runner = "yarn"
        elif (root / "bun.lockb").is_file():
            runner = "bun x"

        if "typecheck" in scripts:
            typecheck = (
                "npm run typecheck" if runner == "npx" else f"{runner} run typecheck"
            )
        elif (
            "tsconfig.json" in [p.name for p in root.iterdir() if p.is_file()]
            or "typescript" in dev_deps
            or "typescript" in deps
        ):
            typecheck = f"{runner} tsc --noEmit"

        if "lint" in scripts:
            lint = "npm run lint" if runner == "npx" else f"{runner} run lint"

        if "test" in scripts:
            test = "npm test" if runner == "npx" else f"{runner} test"

    elif pyproject.is_file() or setup_py.is_file():
        indicators.append("pyproject.toml" if pyproject.is_file() else "setup.py")
        language = "python"
        text = ""
        if pyproject.is_file():
            try:
                text = pyproject.read_text(encoding="utf-8")
            except OSError:
                text = ""

        if "[tool.uv]" in text or (root / "uv.lock").is_file():
            runner = "uv run"
        elif "[tool.poetry]" in text:
            runner = "poetry run"
        else:
            runner = ""

        prefix = f"{runner} " if runner else ""
        if "[tool.ty]" in text or "[tool.pyrefly]" in text:
            typecheck = f"{prefix}ty check ."
        elif "mypy" in text:
            typecheck = f"{prefix}mypy ."
        # ruff is the dominant Python linter — assume it when ruff config is present
        if (
            "[tool.ruff]" in text
            or (root / ".ruff.toml").is_file()
            or (root / "ruff.toml").is_file()
        ):
            lint = f"{prefix}ruff check ."

        # pytest is the de-facto test runner
        if (
            (root / "pytest.ini").is_file()
            or "[tool.pytest" in text
            or any(root.glob("tests"))
        ):
            test = f"{prefix}pytest -q"

    elif cargo_toml.is_file():
        indicators.append("Cargo.toml")
        language = "rust"
        typecheck = "cargo check"
        lint = "cargo clippy --all-targets -- -D warnings"
        test = "cargo test"

    elif go_mod.is_file():
        indicators.append("go.mod")
        language = "go"
        typecheck = "go vet ./..."
        lint = "golangci-lint run" if _which("golangci-lint") else ""
        test = "go test ./..."

    elif pom_xml.is_file():
        indicators.append("pom.xml")
        language = "java"
        typecheck = "mvn -q compile"
        test = "mvn -q test"

    return ProjectProfile(
        language=language,
        typecheck=typecheck,
        lint=lint,
        test=test,
        detected_via=indicators,
    )


def _run_check(name: str, command: str, *, cwd: Path, timeout: int) -> CheckResult:
    """Execute a single verification command and capture its result.

    Args:
        name: Label for the check (used in the summary).
        command: Shell command to run.
        cwd: Working directory.
        timeout: Per-check timeout in seconds.

    Returns:
        A CheckResult describing what happened.
    """
    if not command:
        return CheckResult(
            name=name,
            command="",
            exit_code=0,
            duration_seconds=0.0,
            output="",
            skipped_reason="not configured",
        )

    start = time.monotonic()
    try:
        argv = shlex.split(command, posix=sys.platform != "win32")
        if not argv:
            return CheckResult(
                name=name,
                command=command,
                exit_code=0,
                duration_seconds=0.0,
                output="",
                skipped_reason="empty command",
            )
        # On Windows, commands like `npm`, `npx`, `yarn` are .cmd wrappers that
        # subprocess cannot find without shell=True. Resolve via shutil.which
        # so we keep shell=False (safer) while still finding .cmd/.bat wrappers.
        if sys.platform == "win32":
            resolved = shutil.which(argv[0])
            if resolved:
                argv[0] = resolved
        proc = subprocess.run(  # noqa: S603
            argv,
            cwd=str(cwd),
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        elapsed = time.monotonic() - start
        output = (proc.stdout or "") + (
            ("\n[stderr]\n" + proc.stderr) if proc.stderr else ""
        )
        # Cap each check's output to keep verification_summary.md tractable
        if len(output) > 8000:
            output = (
                output[:4000]
                + f"\n\n... [truncated {len(output) - 8000} chars] ...\n\n"
                + output[-4000:]
            )
        return CheckResult(
            name=name,
            command=command,
            exit_code=proc.returncode,
            duration_seconds=round(elapsed, 2),
            output=output,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        return CheckResult(
            name=name,
            command=command,
            exit_code=124,
            duration_seconds=round(elapsed, 2),
            output=f"[verify] command timed out after {timeout}s",
        )


def _override_script(root: Path) -> Path | None:
    """Return the path to a `.bog-agents/verify.sh|cmd` override, if present."""
    posix = root / ".bog-agents" / "verify.sh"
    win = root / ".bog-agents" / "verify.cmd"
    if sys.platform == "win32" and win.is_file():
        return win
    if posix.is_file():
        return posix
    return None


def _format_summary(
    profile: ProjectProfile, results: list[CheckResult], override_script: Path | None
) -> str:
    """Render the markdown verification summary.

    Args:
        profile: Detected project profile (or a synthetic one for override-script runs).
        results: Per-check results in execution order.
        override_script: If set, the override script that ran instead of the auto-detected chain.

    Returns:
        Markdown text suitable for `verification_summary.md`.
    """
    passing = [r for r in results if r.exit_code == 0 and not r.skipped_reason]
    failing = [r for r in results if r.exit_code != 0]
    skipped = [r for r in results if r.skipped_reason]

    lines: list[str] = ["# Verification summary", ""]
    if override_script is not None:
        lines.append(f"**Override script:** `{override_script}`")
    else:
        lines.append(f"**Detected language:** {profile.language}")
        if profile.detected_via:
            lines.append(f"**Detected via:** {', '.join(profile.detected_via)}")
    lines.extend(
        [
            "",
            f"**Result:** {len(passing)} passed, {len(failing)} failed, {len(skipped)} skipped.",
            "",
            "| Check | Command | Exit | Time | Status |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for r in results:
        status = (
            "✓ pass"
            if r.exit_code == 0 and not r.skipped_reason
            else (f"⊘ skipped ({r.skipped_reason})" if r.skipped_reason else "✗ fail")
        )
        cmd_display = r.command or "—"
        lines.append(
            f"| {r.name} | `{cmd_display}` | {r.exit_code} | {r.duration_seconds}s | {status} |"
        )
    lines.append("")

    for r in results:
        if r.skipped_reason:
            continue
        lines.extend(
            [
                f"## {r.name} (`{r.command}`)",
                "",
                "```",
                r.output.rstrip() or "(no output)",
                "```",
                "",
            ]
        )

    return "\n".join(lines)


def _emit_text_report(results: list[CheckResult], summary_path: Path) -> None:
    """Print a short text report to stdout for human consumption."""
    failed = [r for r in results if r.exit_code != 0]
    print(f"verification: {len(results)} check(s) ran, {len(failed)} failed")  # noqa: T201
    for r in results:
        marker = (
            "✓"
            if r.exit_code == 0 and not r.skipped_reason
            else ("⊘" if r.skipped_reason else "✗")
        )
        print(  # noqa: T201
            f"  {marker} {r.name:<10} ({r.duration_seconds}s)  {r.command or '(skipped)'}"
        )
    print(f"  → wrote {summary_path}")  # noqa: T201


def _emit_json_report(
    profile: ProjectProfile, results: list[CheckResult], summary_path: Path
) -> None:
    """Print a JSON envelope for machine consumers."""
    envelope = {
        "schema_version": 1,
        "command": "verify",
        "data": {
            "language": profile.language,
            "detected_via": profile.detected_via,
            "summary_path": str(summary_path),
            "checks": [
                {
                    "name": r.name,
                    "command": r.command,
                    "exit_code": r.exit_code,
                    "duration_seconds": r.duration_seconds,
                    "skipped_reason": r.skipped_reason,
                }
                for r in results
            ],
        },
    }
    sys.stdout.write(json.dumps(envelope) + "\n")
    sys.stdout.flush()


def cmd_verify(args: argparse.Namespace) -> int:
    """Run the project's verification chain.

    Args:
        args: Parsed argparse namespace with `cwd`, `output`, `timeout`,
            `output_format`, `skip_lint`, `skip_test`, and `skip_typecheck`.

    Returns:
        Process exit code.
    """
    root = Path(getattr(args, "cwd", "") or Path.cwd()).resolve()
    timeout = int(getattr(args, "timeout", 300) or 300)
    output_path = Path(
        getattr(args, "output", "") or root / "verification_summary.md"
    ).resolve()
    output_format = getattr(args, "output_format", "text") or "text"

    override = _override_script(root)
    if override is not None:
        # Pass the script path through shlex.quote so _run_check (which now
        # uses argv form, no shell=True) splits it back into a clean list.
        if override.suffix == ".sh":
            cmd = f"bash {shlex.quote(str(override))}"
        else:
            cmd = f"cmd /c {shlex.quote(str(override))}"
        result = _run_check("custom", cmd, cwd=root, timeout=timeout)
        results = [result]
        profile = ProjectProfile(
            language="custom", detected_via=[str(override.relative_to(root))]
        )
    else:
        profile = detect_project_profile(root)
        if profile.language == "unknown":
            sys.stderr.write(
                "verify: no project type detected. Add a .bog-agents/verify.sh "
                "or .bog-agents/verify.cmd to define your verification chain.\n"
            )
            return 2

        skip_typecheck = bool(getattr(args, "skip_typecheck", False))
        skip_lint = bool(getattr(args, "skip_lint", False))
        skip_test = bool(getattr(args, "skip_test", False))

        results = []
        if skip_typecheck:
            results.append(
                CheckResult(
                    name="typecheck",
                    command=profile.typecheck,
                    exit_code=0,
                    duration_seconds=0.0,
                    output="",
                    skipped_reason="--skip-typecheck",
                )
            )
        else:
            results.append(
                _run_check("typecheck", profile.typecheck, cwd=root, timeout=timeout)
            )

        if skip_lint:
            results.append(
                CheckResult(
                    name="lint",
                    command=profile.lint,
                    exit_code=0,
                    duration_seconds=0.0,
                    output="",
                    skipped_reason="--skip-lint",
                )
            )
        else:
            results.append(_run_check("lint", profile.lint, cwd=root, timeout=timeout))

        if skip_test:
            results.append(
                CheckResult(
                    name="tests",
                    command=profile.test,
                    exit_code=0,
                    duration_seconds=0.0,
                    output="",
                    skipped_reason="--skip-test",
                )
            )
        else:
            results.append(_run_check("tests", profile.test, cwd=root, timeout=timeout))

    no_output = bool(getattr(args, "no_output", False))
    summary = _format_summary(profile, results, override)
    if not no_output:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(summary, encoding="utf-8")

    if output_format == "json":
        _emit_json_report(profile, results, output_path)
    else:
        _emit_text_report(results, output_path)

    failed = [r for r in results if r.exit_code != 0]
    return 1 if failed else 0


def setup_verify_parser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: Iterable[argparse.ArgumentParser] = (),
) -> None:
    """Wire the `verify` subcommand into the top-level argparse tree.

    Args:
        subparsers: argparse subparsers action returned by `add_subparsers()`.
        parents: Parent parsers to inherit (for shared `-h`/`--help` handling).
    """
    p = subparsers.add_parser(
        "verify",
        help="Run the project's typecheck + lint + tests; write verification_summary.md",
        parents=list(parents),
        add_help=not list(parents),
    )
    p.add_argument(
        "--cwd", default="", help="Project root (default: current directory)"
    )
    p.add_argument(
        "--output",
        default="",
        metavar="PATH",
        help="Where to write verification_summary.md (default: <cwd>/verification_summary.md)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Per-check timeout in seconds (default: 300)",
    )
    p.add_argument(
        "--skip-typecheck",
        action="store_true",
        help="Don't run the auto-detected typecheck command",
    )
    p.add_argument(
        "--skip-lint",
        action="store_true",
        help="Don't run the auto-detected lint command",
    )
    p.add_argument(
        "--skip-test",
        action="store_true",
        help="Don't run the auto-detected test command",
    )
    p.add_argument(
        "--no-output",
        action="store_true",
        default=False,
        help="Skip writing verification_summary.md (results still printed to console)",
    )
    p.add_argument(
        "--json",
        action="store_const",
        const="json",
        dest="output_format",
        help="Emit a JSON envelope on stdout",
    )
