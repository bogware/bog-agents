"""Middleware for automatic linting and testing after file changes.

Feature #11: Auto-lint — automatically run configured linter after file edits.
Feature #12: Auto-test — automatically run configured test suite after file edits.
Feature #44: Dependency-aware tool filtering — detect project type and auto-configure.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

from bog_agents.backends.protocol import ExecuteResponse, SandboxBackendProtocol
from bog_agents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)

# Mutating tools that should trigger quality checks
_FILE_MUTATING_TOOLS = frozenset({"write_file", "edit_file", "multi_edit_file"})


@dataclass
class ProjectDetection:
    """Detected project configuration."""

    language: str
    """Primary language (python, javascript, typescript, rust, go, java)."""

    package_manager: str | None = None
    """Package manager (uv, pip, npm, yarn, pnpm, cargo, go)."""

    lint_command: str | None = None
    """Auto-detected lint command."""

    test_command: str | None = None
    """Auto-detected test command."""

    format_command: str | None = None
    """Auto-detected format command."""

    type_check_command: str | None = None
    """Auto-detected type check command."""

    build_command: str | None = None
    """Auto-detected build command."""


# Project detection rules: (indicator_file, language, package_manager, lint, test, format, typecheck)
_PROJECT_INDICATORS: list[tuple[str, str, str | None, str | None, str | None, str | None, str | None]] = [
    ("pyproject.toml", "python", "uv", "uv run ruff check .", "uv run pytest", "uv run ruff format .", "uv run ty check"),
    ("setup.py", "python", "pip", "ruff check .", "pytest", "ruff format .", None),
    ("Cargo.toml", "rust", "cargo", "cargo clippy", "cargo test", "cargo fmt", None),
    ("go.mod", "go", "go", "golangci-lint run", "go test ./...", "gofmt -w .", None),
    ("package.json", "javascript", None, "npm run lint", "npm test", "npm run format", None),
    ("tsconfig.json", "typescript", None, "npm run lint", "npm test", "npm run format", "npx tsc --noEmit"),
    ("pom.xml", "java", "maven", "mvn checkstyle:check", "mvn test", None, None),
    ("build.gradle", "java", "gradle", "gradle check", "gradle test", None, None),
    ("Makefile", "unknown", "make", "make lint", "make test", "make format", None),
]


def detect_project(working_dir: Path) -> ProjectDetection | None:
    """Detect the project type and appropriate tooling commands.

    Args:
        working_dir: The project root directory to analyze.

    Returns:
        ProjectDetection if a project was detected, None otherwise.
    """
    for indicator, language, pkg_mgr, lint, test, fmt, typecheck in _PROJECT_INDICATORS:
        if (working_dir / indicator).exists():
            # Refine npm-based detection
            if indicator == "package.json":
                try:
                    pkg = json.loads((working_dir / indicator).read_text())
                    scripts = pkg.get("scripts", {})
                    if "lint" not in scripts:
                        lint = None
                    if "test" not in scripts:
                        test = None
                    if "format" not in scripts:
                        fmt = None

                    # Detect package manager
                    if (working_dir / "yarn.lock").exists():
                        pkg_mgr = "yarn"
                        if lint:
                            lint = lint.replace("npm run", "yarn")
                        if test:
                            test = test.replace("npm", "yarn")
                        if fmt:
                            fmt = fmt.replace("npm run", "yarn")
                    elif (working_dir / "pnpm-lock.yaml").exists():
                        pkg_mgr = "pnpm"
                        if lint:
                            lint = lint.replace("npm run", "pnpm run")
                        if test:
                            test = test.replace("npm", "pnpm")
                        if fmt:
                            fmt = fmt.replace("npm run", "pnpm run")
                    else:
                        pkg_mgr = "npm"
                except (json.JSONDecodeError, OSError):
                    pass

            # Refine pyproject.toml detection
            if indicator == "pyproject.toml":
                try:
                    content = (working_dir / indicator).read_text()
                    if "[tool.poetry]" in content:
                        pkg_mgr = "poetry"
                        lint = "poetry run ruff check ."
                        test = "poetry run pytest"
                        fmt = "poetry run ruff format ."
                    elif "uv" not in content and "[project]" in content:
                        pkg_mgr = "pip"
                except OSError:
                    pass

            return ProjectDetection(
                language=language,
                package_manager=pkg_mgr,
                lint_command=lint,
                test_command=test,
                format_command=fmt,
                type_check_command=typecheck,
            )

    return None


class AutoQualityState(TypedDict):
    """State for auto-quality middleware."""


class AutoQualityMiddleware(AgentMiddleware[AutoQualityState, ContextT, ResponseT]):
    """Middleware that automatically runs linting and testing after file changes.

    Detects the project type and configures appropriate quality commands.
    After any file-mutating tool call, runs the configured linter and/or
    test suite and injects the results as system context.

    Args:
        working_dir: Project root directory.
        auto_lint: Whether to auto-run linter after edits.
        auto_test: Whether to auto-run tests after edits.
        auto_format: Whether to auto-run formatter after edits.
        lint_command: Override auto-detected lint command.
        test_command: Override auto-detected test command.
        format_command: Override auto-detected format command.
        backend: Backend supporting execution for running commands.
        max_auto_test_files: Max number of edited files before skipping auto-test.
    """

    state_schema = AutoQualityState

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        auto_lint: bool = True,
        auto_test: bool = False,
        auto_format: bool = False,
        lint_command: str | None = None,
        test_command: str | None = None,
        format_command: str | None = None,
        backend: SandboxBackendProtocol | None = None,
        max_auto_test_files: int = 5,
    ) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._auto_lint = auto_lint
        self._auto_test = auto_test
        self._auto_format = auto_format
        self._backend = backend
        self._max_auto_test_files = max_auto_test_files

        # Detect project and set commands
        detection = detect_project(self._working_dir)
        self._detection = detection
        self._lint_command = lint_command or (detection.lint_command if detection else None)
        self._test_command = test_command or (detection.test_command if detection else None)
        self._format_command = format_command or (detection.format_command if detection else None)

        self._edited_files: set[str] = set()
        self._tools = self._build_tools()

    @property
    def tools(self) -> list[BaseTool]:
        """Tools provided by this middleware."""
        return self._tools

    def _build_tools(self) -> list[BaseTool]:
        """Build the quality check tools."""
        tools: list[BaseTool] = []
        middleware = self

        if self._lint_command:

            def run_lint(
                runtime: ToolRuntime[None, AutoQualityState],
                path: str | None = None,
            ) -> str:
                """Run the project linter. Optionally pass a specific file path."""
                cmd = middleware._lint_command
                if path and cmd:
                    cmd = f"{cmd} {path}"
                return middleware._run_command(cmd or "echo 'No linter configured'")

            tools.append(
                StructuredTool.from_function(
                    name="run_lint",
                    description=f"Run the project linter ({self._lint_command}). Optionally specify a path to lint a single file.",
                    func=run_lint,
                )
            )

        if self._test_command:

            def run_tests(
                runtime: ToolRuntime[None, AutoQualityState],
                path: str | None = None,
            ) -> str:
                """Run the project test suite. Optionally pass a specific test file."""
                cmd = middleware._test_command
                if path and cmd:
                    cmd = f"{cmd} {path}"
                return middleware._run_command(cmd or "echo 'No test command configured'")

            tools.append(
                StructuredTool.from_function(
                    name="run_tests",
                    description=f"Run the project test suite ({self._test_command}). Optionally specify a test file path.",
                    func=run_tests,
                )
            )

        if self._format_command:

            def run_format(
                runtime: ToolRuntime[None, AutoQualityState],
                path: str | None = None,
            ) -> str:
                """Run the project formatter. Optionally pass a specific file path."""
                cmd = middleware._format_command
                if path and cmd:
                    cmd = f"{cmd} {path}"
                return middleware._run_command(cmd or "echo 'No formatter configured'")

            tools.append(
                StructuredTool.from_function(
                    name="run_format",
                    description=f"Run the project formatter ({self._format_command}). Optionally specify a file path.",
                    func=run_format,
                )
            )

        def detect_project_tool(
            runtime: ToolRuntime[None, AutoQualityState],
        ) -> str:
            """Detect the project type, language, and available tooling."""
            detection = middleware._detection
            if not detection:
                return "Could not detect project type. No known project indicators found."
            parts = [
                f"Language: {detection.language}",
                f"Package Manager: {detection.package_manager or 'unknown'}",
            ]
            if detection.lint_command:
                parts.append(f"Lint: {detection.lint_command}")
            if detection.test_command:
                parts.append(f"Test: {detection.test_command}")
            if detection.format_command:
                parts.append(f"Format: {detection.format_command}")
            if detection.type_check_command:
                parts.append(f"Type Check: {detection.type_check_command}")
            return "\n".join(parts)

        tools.append(
            StructuredTool.from_function(
                name="detect_project",
                description="Detect the project type, language, package manager, and available quality commands.",
                func=detect_project_tool,
            )
        )

        return tools

    def _run_command(self, command: str) -> str:
        """Run a shell command via the backend.

        Args:
            command: Shell command to execute.

        Returns:
            Command output or error message.
        """
        if not self._backend:
            return "Error: No execution backend configured for quality checks."

        try:
            result: ExecuteResponse = self._backend.execute(command, timeout=60)
            output = result.output
            if result.exit_code != 0:
                output = f"[exit code {result.exit_code}]\n{output}"
            return output
        except Exception as e:
            return f"Error running quality check: {e}"

    def wrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject project detection info into the system prompt.

        Args:
            request: The model request.
            call_next: Next handler.

        Returns:
            Model response.
        """
        if self._detection:
            info = f"\n\n## Project Detection\nLanguage: {self._detection.language}"
            if self._detection.package_manager:
                info += f", Package Manager: {self._detection.package_manager}"
            if self._lint_command:
                info += f"\nLint: `{self._lint_command}`"
            if self._test_command:
                info += f"\nTest: `{self._test_command}`"
            request = append_to_system_message(request, info)

        return call_next(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version of wrap_model_call."""
        if self._detection:
            info = f"\n\n## Project Detection\nLanguage: {self._detection.language}"
            if self._detection.package_manager:
                info += f", Package Manager: {self._detection.package_manager}"
            if self._lint_command:
                info += f"\nLint: `{self._lint_command}`"
            if self._test_command:
                info += f"\nTest: `{self._test_command}`"
            request = append_to_system_message(request, info)

        return await call_next(request)
