"""Test runner command module for the bog-agents CLI.

Provides auto-detection of test frameworks and Rich-formatted output
for the /test slash command.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess  # noqa: S404
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Framework detection priority order
_FRAMEWORK_ORDER = ["pytest", "jest", "vitest", "go", "cargo", "maven"]


def detect_test_framework(cwd: Path) -> str | None:
    """Detect the test framework in use for a given working directory.

    Checks for framework-specific config files and falls back to directory
    heuristics when nothing definitive is found.

    Args:
        cwd: Working directory to inspect.

    Returns:
        One of 'pytest', 'jest', 'vitest', 'go', 'cargo', 'maven', or None
        if no framework can be detected.
    """
    pyproject = cwd / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8")
            if "[tool.pytest" in text or "pytest" in text:
                return "pytest"
        except OSError:
            pass

    package_json = cwd / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            dev_deps: dict[str, str] = data.get("devDependencies", {})
            all_deps: dict[str, str] = {**dev_deps, **data.get("dependencies", {})}
            if "vitest" in all_deps:
                return "vitest"
            if "jest" in all_deps:
                return "jest"
        except (OSError, json.JSONDecodeError):
            pass

    if (cwd / "go.mod").exists():
        return "go"

    if (cwd / "Cargo.toml").exists():
        return "cargo"

    if (cwd / "pom.xml").exists():
        return "maven"

    # Fallback: test/tests directory present and pytest importable
    has_test_dir = (cwd / "tests").is_dir() or (cwd / "test").is_dir()
    if has_test_dir:
        result = subprocess.run(
            ["python", "-m", "pytest", "--version"],
            capture_output=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            return "pytest"

    return None


def find_test_file(source_file: Path, cwd: Path) -> Path | None:
    """Try to find the test file corresponding to a source file.

    Searches common test file naming conventions alongside the source,
    inside a sibling tests/ directory, and inside __tests__ directories.

    Args:
        source_file: Path to the source file (may be relative or absolute).
        cwd: Working directory used to resolve relative paths.

    Returns:
        Absolute path to the discovered test file, or None if not found.
    """
    if not source_file.is_absolute():
        source_file = cwd / source_file

    stem = source_file.stem
    suffix = source_file.suffix  # e.g. ".py" or ".ts"
    parent = source_file.parent

    candidates: list[Path] = []

    # Python conventions
    if suffix == ".py":
        candidates += [
            parent / f"test_{stem}.py",
            parent / f"{stem}_test.py",
            parent / "tests" / f"test_{stem}.py",
            parent / "tests" / f"{stem}_test.py",
            cwd / "tests" / f"test_{stem}.py",
            cwd / "tests" / f"{stem}_test.py",
            cwd / "test" / f"test_{stem}.py",
            cwd / "test" / f"{stem}_test.py",
        ]

    # JavaScript / TypeScript conventions
    elif suffix in {".js", ".ts", ".jsx", ".tsx", ".mjs"}:
        candidates += [
            parent / f"{stem}.test{suffix}",
            parent / f"{stem}.spec{suffix}",
            parent / "__tests__" / f"{stem}.test{suffix}",
            parent / "__tests__" / f"{stem}.spec{suffix}",
            cwd / "__tests__" / f"{stem}.test{suffix}",
            cwd / "__tests__" / f"{stem}.spec{suffix}",
        ]

    # Go convention: <stem>_test.go lives alongside the source
    elif suffix == ".go":
        candidates += [
            parent / f"{stem}_test.go",
        ]

    # Rust convention: tests live inline but also in tests/
    elif suffix == ".rs":
        candidates += [
            cwd / "tests" / f"{stem}.rs",
        ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return None


def _parse_pytest_output(output: str) -> dict[str, int]:
    """Extract pass/fail/error counts from pytest -q output.

    Args:
        output: Combined stdout+stderr from pytest.

    Returns:
        Dict with 'passed', 'failed', 'error', 'skipped' counts.
    """
    counts: dict[str, int] = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    # Matches lines like: "3 passed, 1 failed, 2 errors in 0.45s"
    pattern = re.compile(
        r"(?:(\d+) passed)?[,\s]*(?:(\d+) failed)?[,\s]*(?:(\d+) error(?:s)?)?[,\s]*(?:(\d+) skipped)?"
    )
    for line in output.splitlines():
        m = pattern.search(line)
        if m and any(m.groups()):
            if m.group(1):
                counts["passed"] = int(m.group(1))
            if m.group(2):
                counts["failed"] = int(m.group(2))
            if m.group(3):
                counts["error"] = int(m.group(3))
            if m.group(4):
                counts["skipped"] = int(m.group(4))
    return counts


def _parse_jest_output(output: str) -> dict[str, int]:
    """Extract pass/fail counts from jest/vitest output.

    Args:
        output: Combined stdout+stderr from jest or vitest.

    Returns:
        Dict with 'passed', 'failed', 'skipped' counts.
    """
    counts: dict[str, int] = {"passed": 0, "failed": 0, "skipped": 0}
    for line in output.splitlines():
        passed_m = re.search(r"(\d+)\s+passed", line)
        failed_m = re.search(r"(\d+)\s+(?:failed|\xd7)", line)
        skipped_m = re.search(r"(\d+)\s+(?:skipped|pending|todo)", line)
        if passed_m:
            counts["passed"] = int(passed_m.group(1))
        if failed_m:
            counts["failed"] = int(failed_m.group(1))
        if skipped_m:
            counts["skipped"] = int(skipped_m.group(1))
    return counts


def _parse_go_output(output: str) -> dict[str, int]:
    """Extract pass/fail counts from go test output.

    Args:
        output: Combined stdout+stderr from go test.

    Returns:
        Dict with 'passed' and 'failed' counts.
    """
    counts: dict[str, int] = {"passed": 0, "failed": 0}
    for line in output.splitlines():
        if line.startswith("ok") or "PASS" in line:
            counts["passed"] += 1
        if line.startswith("FAIL") or "--- FAIL" in line:
            counts["failed"] += 1
    return counts


def run_tests(
    *,
    cwd: Path,
    test_file: str | None = None,
    framework: str | None = None,
    timeout: int = 60,
) -> str:
    """Run tests and return Rich-formatted results.

    Auto-detects the test framework when `framework` is not specified. Runs
    the appropriate test command and parses pass/fail counts from the output.

    Args:
        cwd: Working directory.
        test_file: Optional specific test file or pattern to run.
        framework: Override framework detection. Must be one of the values
            returned by `detect_test_framework`.
        timeout: Seconds to wait before killing the test process.

    Returns:
        Rich-formatted string with pass/fail counts, duration, and any
        failure details extracted from the test runner output.
    """
    framework = framework or detect_test_framework(cwd)
    if framework is None:
        return (
            "[yellow]No test framework detected.[/yellow] "
            "Make sure your project has a pyproject.toml, package.json, go.mod, Cargo.toml, or pom.xml."
        )

    cmd: list[str]
    if framework == "pytest":
        cmd = ["uv", "run", "pytest", "--tb=short", "-q"]
        if test_file:
            cmd.append(test_file)
    elif framework == "jest":
        cmd = ["npx", "jest", "--no-coverage"]
        if test_file:
            cmd.append(test_file)
    elif framework == "vitest":
        cmd = ["npx", "vitest", "run"]
        if test_file:
            cmd.append(test_file)
    elif framework == "go":
        cmd = ["go", "test", "./..."]
    elif framework == "cargo":
        cmd = ["cargo", "test"]
    elif framework == "maven":
        cmd = ["mvn", "test", "-q"]
    else:
        return f"[red]Unknown framework:[/red] {framework!r}"

    start = time.monotonic()
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
            timeout=timeout,
        )
    except FileNotFoundError:
        return f"[red]Command not found:[/red] {cmd[0]!r}. Is it installed?"
    except subprocess.TimeoutExpired:
        return f"[red]Test run timed out[/red] after {timeout}s."
    elapsed = time.monotonic() - start

    combined = result.stdout + result.stderr

    # Parse counts per framework family
    if framework == "pytest":
        counts = _parse_pytest_output(combined)
    elif framework in {"jest", "vitest"}:
        counts = _parse_jest_output(combined)
    elif framework == "go":
        counts = _parse_go_output(combined)
    else:
        # For cargo/maven just use return-code heuristic
        counts = {"passed": 0, "failed": 0}
        if result.returncode == 0:
            counts["passed"] = 1
        else:
            counts["failed"] = 1

    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0)
    errors = counts.get("error", 0)
    skipped = counts.get("skipped", 0)
    total_bad = failed + errors

    status_tag = "[green]PASSED[/green]" if (result.returncode == 0) else "[red]FAILED[/red]"

    lines: list[str] = [
        f"{status_tag}  [{framework}]  {elapsed:.2f}s",
        f"  [green]{passed} passed[/green]"
        + (f"  [red]{failed} failed[/red]" if failed else "")
        + (f"  [red]{errors} errors[/red]" if errors else "")
        + (f"  [dim]{skipped} skipped[/dim]" if skipped else ""),
    ]

    if total_bad > 0 and combined.strip():
        # Surface the tail of the output where failures typically appear
        tail_lines = combined.strip().splitlines()[-40:]
        lines.append("")
        lines.append("[dim]--- output ---[/dim]")
        lines.extend(tail_lines)

    return "\n".join(lines)


def format_test_help() -> str:
    """Return usage help for /test command.

    Returns:
        Multi-line help string describing /test subcommands and options.
    """
    return (
        "[bold]/test[/bold] — Run project tests\n\n"
        "Usage:\n"
        "  /test                    Auto-detect framework and run all tests\n"
        "  /test <file>             Run a specific test file or pattern\n"
        "  /test --framework=<fw>   Force a specific framework\n\n"
        "Supported frameworks: pytest, jest, vitest, go, cargo, maven\n\n"
        "Examples:\n"
        "  /test\n"
        "  /test tests/unit_tests/test_agent.py\n"
        "  /test --framework=pytest"
    )
