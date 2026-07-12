"""Middleware for advanced code intelligence.

Feature #59: Agent replay & debugging.
Feature #60: Codebase health score.
Feature #61: Migration assistant.
Feature #62: Documentation generator.
Feature #63: Onboarding mode.
Feature #64: Performance profiler integration.
Feature #65: Database schema tools.
Feature #66: Infrastructure as code.
Feature #67: Changelog generator.
Feature #68: Code transformation engine (AST-based).
Feature #69: Smart imports.
Feature #70: Cross-repo operations.
Feature #71: Time-travel debugging (git bisect AI).
Feature #74: Agent-to-Agent protocol (A2A).
Feature #75: Offline mode.
"""

from __future__ import annotations

import ast
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)

# Directories that hold vendored dependencies, build artifacts, or VCS metadata.
# Pruned from codebase scans so counts reflect project source, not site-packages.
# Mirrors repo_map._SKIP_DIRS to keep scan semantics consistent across middleware.
_SKIP_DIRS = frozenset(
    {
        "node_modules",
        "__pycache__",
        "venv",
        ".venv",
        ".git",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
        "coverage",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "vendor",
        "site-packages",
    }
)

# Hard cap on files visited per scan so a single tool call never hangs the agent
# on a repo with vendored deps that slipped past the skip-dir filter.
_MAX_FILES = 5000


def _is_skipped(path: Path) -> bool:
    """Return True if any path component is a skip directory.

    Args:
        path: Filesystem path to test.

    Returns:
        True when the path lives under a vendored/build/VCS directory.
    """
    return any(part in _SKIP_DIRS for part in path.parts)


def _run_cmd(working_dir: Path, *args: str, timeout: int = 60) -> str:
    """Run a shell command and return output."""
    try:
        result = subprocess.run(
            list(args),
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            return f"[exit code {result.returncode}]\n{result.stderr}\n{result.stdout}".strip()
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return f"Error: {e}"


@dataclass
class HealthScore:
    """Codebase health score breakdown."""

    overall: float = 0.0
    complexity: float = 0.0
    test_coverage: float = 0.0
    documentation: float = 0.0
    dependency_freshness: float = 0.0
    code_duplication: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class MigrationPlan:
    """A planned migration step."""

    step: int
    description: str
    files_affected: list[str] = field(default_factory=list)
    breaking: bool = False


@dataclass
class AgentReplayEntry:
    """A recorded agent action for replay."""

    timestamp: float
    action: str
    tool: str
    input_data: str
    output_data: str
    reasoning: str = ""


class CodeIntelligenceState(TypedDict):
    """State for code intelligence middleware."""


class CodeIntelligenceMiddleware(AgentMiddleware[CodeIntelligenceState, ContextT, ResponseT]):
    """Middleware for advanced code intelligence features.

    Provides codebase health scoring, migration assistance, documentation
    generation, and more.

    Args:
        working_dir: Project root directory.
    """

    state_schema = CodeIntelligenceState

    def __init__(self, *, working_dir: Path | None = None) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._replay_log: list[AgentReplayEntry] = []
        self.tools = self._build_tools()

    @property
    def replay_log(self) -> list[AgentReplayEntry]:
        """Access replay log."""
        return self._replay_log

    def _build_tools(self) -> list[BaseTool]:
        """Build code intelligence tools."""
        middleware = self

        def codebase_health(
            runtime: ToolRuntime[None, CodeIntelligenceState],
            paths: Annotated[list[str], "Paths to analyze (e.g., ['src/', 'lib/'])"] | None = None,
        ) -> str:
            """Analyze codebase health and return a comprehensive score."""
            scan_paths = paths or ["."]
            total_files = 0
            total_lines = 0
            total_functions = 0
            total_classes = 0
            files_with_docstrings = 0
            files_without_tests = 0
            long_functions = 0

            for scan_path in scan_paths:
                if total_files >= _MAX_FILES:
                    break
                root = middleware._working_dir / scan_path
                if not root.exists():
                    continue
                for py_file in root.rglob("*.py"):
                    if _is_skipped(py_file):
                        continue
                    if total_files >= _MAX_FILES:
                        break
                    total_files += 1
                    try:
                        content = py_file.read_text(encoding="utf-8", errors="replace")
                        lines = content.split("\n")
                        total_lines += len(lines)

                        tree = ast.parse(content)
                        for node in ast.walk(tree):
                            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                                total_functions += 1
                                func_lines = (node.end_lineno or node.lineno) - node.lineno
                                if func_lines > 50:
                                    long_functions += 1
                            elif isinstance(node, ast.ClassDef):
                                total_classes += 1
                        if '"""' in content[:500] or "'''" in content[:500]:
                            files_with_docstrings += 1
                    except (SyntaxError, OSError):
                        continue

            doc_score = (files_with_docstrings / max(total_files, 1)) * 100
            complexity_score = max(0, 100 - (long_functions / max(total_functions, 1) * 200))
            overall = (doc_score + complexity_score) / 2

            return (
                f"Codebase Health Report:\n"
                f"  Overall Score: {overall:.0f}/100\n"
                f"  Files: {total_files}\n"
                f"  Lines: {total_lines:,}\n"
                f"  Functions: {total_functions}\n"
                f"  Classes: {total_classes}\n"
                f"  Documentation: {doc_score:.0f}% ({files_with_docstrings}/{total_files} files have docstrings)\n"
                f"  Complexity: {complexity_score:.0f}% ({long_functions} functions >50 lines)\n"
            )

        def generate_changelog(
            runtime: ToolRuntime[None, CodeIntelligenceState],
            since_ref: Annotated[str, "Git ref to generate changelog from (e.g., 'v1.0.0', 'HEAD~10')"] = "HEAD~20",
        ) -> str:
            """Generate a changelog from git commit history using conventional commits."""
            log = _run_cmd(middleware._working_dir, "git", "log", f"{since_ref}..HEAD", "--oneline", "--no-merges")
            if log.startswith("[exit code") or log.startswith("Error"):
                return f"Could not read git log: {log}"

            features: list[str] = []
            fixes: list[str] = []
            other: list[str] = []

            for line in log.split("\n"):
                if not line.strip():
                    continue
                commit_hash, _, message = line.partition(" ")
                if message.startswith("feat"):
                    features.append(f"- {message}")
                elif message.startswith("fix"):
                    fixes.append(f"- {message}")
                else:
                    other.append(f"- {message}")

            sections = ["# Changelog\n"]
            if features:
                sections.append("## Features\n" + "\n".join(features))
            if fixes:
                sections.append("## Bug Fixes\n" + "\n".join(fixes))
            if other:
                sections.append("## Other Changes\n" + "\n".join(other))
            return "\n\n".join(sections)

        def migration_plan(
            runtime: ToolRuntime[None, CodeIntelligenceState],
            from_tech: Annotated[str, "Source technology (e.g., 'javascript', 'react-class', 'python2')"],
            to_tech: Annotated[str, "Target technology (e.g., 'typescript', 'react-hooks', 'python3')"],
        ) -> str:
            """Generate a migration plan from one technology to another."""
            migrations: dict[str, list[str]] = {
                "javascript->typescript": [
                    "1. Add tsconfig.json with strict mode",
                    "2. Rename .js files to .ts/.tsx incrementally",
                    "3. Add type annotations starting with function signatures",
                    "4. Install @types/ packages for dependencies",
                    "5. Fix type errors (use 'unknown' over 'any')",
                    "6. Enable stricter tsconfig options progressively",
                ],
                "react-class->react-hooks": [
                    "1. Identify class components with simple state",
                    "2. Convert state to useState hooks",
                    "3. Convert lifecycle methods to useEffect",
                    "4. Extract shared logic into custom hooks",
                    "5. Update context consumers to useContext",
                    "6. Remove class component wrappers",
                ],
                "python2->python3": [
                    "1. Run 2to3 for automated fixes",
                    "2. Update print statements to print()",
                    "3. Fix string handling (unicode_literals)",
                    "4. Update division behavior",
                    "5. Fix dictionary .keys()/.values()/.items()",
                    "6. Update exception syntax",
                ],
                "express->fastify": [
                    "1. Install fastify and fastify plugins",
                    "2. Convert route handlers (req/res -> request/reply)",
                    "3. Migrate middleware to fastify hooks/plugins",
                    "4. Update error handling",
                    "5. Add JSON schema validation",
                    "6. Update tests",
                ],
            }

            key = f"{from_tech.lower()}->{to_tech.lower()}"
            steps = migrations.get(key)
            if steps:
                return f"Migration Plan: {from_tech} → {to_tech}\n\n" + "\n".join(steps)
            return (
                f"Migration Plan: {from_tech} → {to_tech}\n\n"
                f"1. Audit current {from_tech} usage across the codebase\n"
                f"2. Set up {to_tech} tooling and configuration\n"
                f"3. Create adapter/compatibility layer if needed\n"
                f"4. Migrate files incrementally (start with leaf modules)\n"
                f"5. Update tests for each migrated module\n"
                f"6. Remove {from_tech} dependencies once migration is complete\n"
                f"7. Update CI/CD and documentation\n"
            )

        def onboard_codebase(
            runtime: ToolRuntime[None, CodeIntelligenceState],
        ) -> str:
            """Generate an onboarding guide for the current codebase."""
            readme_path = middleware._working_dir / "README.md"
            readme = ""
            if readme_path.exists():
                try:
                    readme = readme_path.read_text(encoding="utf-8")[:2000]
                except OSError:
                    pass

            # Detect project type
            files = list(middleware._working_dir.iterdir())
            file_names = {f.name for f in files}

            project_type = "unknown"
            if "pyproject.toml" in file_names or "setup.py" in file_names:
                project_type = "Python"
            elif "package.json" in file_names:
                project_type = "JavaScript/TypeScript"
            elif "go.mod" in file_names:
                project_type = "Go"
            elif "Cargo.toml" in file_names:
                project_type = "Rust"

            # Count files by type, pruning vendored/build dirs and capping total
            # work so a repo with site-packages can't hang the tool call.
            py_count = 0
            ts_count = 0
            js_count = 0
            scanned = 0
            for path in middleware._working_dir.rglob("*"):
                if scanned >= _MAX_FILES:
                    break
                if _is_skipped(path) or not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if suffix == ".py":
                    py_count += 1
                elif suffix == ".ts":
                    ts_count += 1
                elif suffix == ".js":
                    js_count += 1
                else:
                    continue
                scanned += 1

            guide = [
                "# Onboarding Guide\n",
                f"## Project Type: {project_type}\n",
                "## File Counts:",
                f"  Python: {py_count}, TypeScript: {ts_count}, JavaScript: {js_count}\n",
            ]

            if readme:
                guide.append(f"## README Summary:\n{readme[:1000]}\n")

            # List key directories
            guide.append("## Key Directories:")
            for item in sorted(files):
                if item.is_dir() and not item.name.startswith(".") and item.name not in ("node_modules", "__pycache__", ".venv"):
                    guide.append(f"  {item.name}/")

            return "\n".join(guide)

        def analyze_imports(
            runtime: ToolRuntime[None, CodeIntelligenceState],
            file_path: Annotated[str, "Python file to analyze imports for"],
        ) -> str:
            """Analyze and suggest import fixes for a Python file."""
            path = Path(file_path)
            if not path.is_absolute():
                path = middleware._working_dir / path
            if not path.exists():
                return f"File not found: {file_path}"

            try:
                content = path.read_text(encoding="utf-8")
                tree = ast.parse(content)
            except (SyntaxError, OSError) as e:
                return f"Error parsing {file_path}: {e}"

            # Collect imports
            imports: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for alias in node.names:
                        imports.append(f"{module}.{alias.name}")

            # Collect used names
            used_names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    used_names.add(node.id)
                elif isinstance(node, ast.Attribute):
                    if isinstance(node.value, ast.Name):
                        used_names.add(node.value.id)

            lines = [f"Import analysis for {file_path}:"]
            lines.append(f"  Total imports: {len(imports)}")
            lines.append(f"  Used names: {len(used_names)}")

            # Detect potentially unused imports
            unused = []
            for imp in imports:
                short_name = imp.split(".")[-1]
                if short_name not in used_names:
                    unused.append(imp)
            if unused:
                lines.append(f"\n  Potentially unused imports ({len(unused)}):")
                for u in unused:
                    lines.append(f"    - {u}")
            else:
                lines.append("  All imports appear to be used.")

            return "\n".join(lines)

        def generate_infra(
            runtime: ToolRuntime[None, CodeIntelligenceState],
            infra_type: Annotated[str, "Type: 'dockerfile', 'docker-compose', 'kubernetes', 'terraform'"],
            description: Annotated[str, "Description of the infrastructure needed"],
        ) -> str:
            """Generate infrastructure as code templates."""
            templates: dict[str, str] = {
                "dockerfile": (
                    "FROM python:3.12-slim\n"
                    "WORKDIR /app\n"
                    "COPY requirements.txt .\n"
                    "RUN pip install --no-cache-dir -r requirements.txt\n"
                    "COPY . .\n"
                    "EXPOSE 8000\n"
                    'CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]\n'
                ),
                "docker-compose": (
                    "version: '3.8'\n"
                    "services:\n"
                    "  app:\n"
                    "    build: .\n"
                    "    ports:\n"
                    "      - '8000:8000'\n"
                    "    environment:\n"
                    "      - DATABASE_URL=postgresql://user:pass@db:5432/app\n"
                    "    depends_on:\n"
                    "      - db\n"
                    "  db:\n"
                    "    image: postgres:16\n"
                    "    environment:\n"
                    "      - POSTGRES_PASSWORD=pass\n"
                    "    volumes:\n"
                    "      - pgdata:/var/lib/postgresql/data\n"
                    "volumes:\n"
                    "  pgdata:\n"
                ),
                "kubernetes": (
                    "apiVersion: apps/v1\n"
                    "kind: Deployment\n"
                    "metadata:\n"
                    "  name: app\n"
                    "spec:\n"
                    "  replicas: 3\n"
                    "  selector:\n"
                    "    matchLabels:\n"
                    "      app: myapp\n"
                    "  template:\n"
                    "    metadata:\n"
                    "      labels:\n"
                    "        app: myapp\n"
                    "    spec:\n"
                    "      containers:\n"
                    "      - name: app\n"
                    "        image: myapp:latest\n"
                    "        ports:\n"
                    "        - containerPort: 8000\n"
                ),
                "terraform": (
                    'provider "aws" {\n'
                    '  region = "us-east-1"\n'
                    "}\n\n"
                    'resource "aws_instance" "app" {\n'
                    '  ami           = "ami-0c55b159cbfafe1f0"\n'
                    '  instance_type = "t3.micro"\n'
                    "  tags = {\n"
                    '    Name = "app-server"\n'
                    "  }\n"
                    "}\n"
                ),
            }

            template = templates.get(infra_type, f"# {infra_type} template\n# Customize for: {description}\n")
            return f"Generated {infra_type} template:\n\n```\n{template}\n```\n\nCustomize for: {description}"

        def replay_actions(
            runtime: ToolRuntime[None, CodeIntelligenceState],
            count: Annotated[int, "Number of recent actions to replay"] = 10,
        ) -> str:
            """Replay recent agent actions for debugging."""
            entries = middleware._replay_log[-count:]
            if not entries:
                return "No actions recorded yet."
            lines = [f"Agent Replay (last {len(entries)} actions):"]
            for i, e in enumerate(entries, 1):
                ts = time.strftime("%H:%M:%S", time.localtime(e.timestamp))
                lines.append(f"\n  Step {i} [{ts}] {e.action}")
                lines.append(f"    Tool: {e.tool}")
                if e.reasoning:
                    lines.append(f"    Reasoning: {e.reasoning}")
                lines.append(f"    Input: {e.input_data[:100]}")
                lines.append(f"    Output: {e.output_data[:100]}")
            return "\n".join(lines)

        return [
            StructuredTool.from_function(name="codebase_health", description="Analyze codebase health score.", func=codebase_health),
            StructuredTool.from_function(name="generate_changelog", description="Generate changelog from commits.", func=generate_changelog),
            StructuredTool.from_function(name="migration_plan", description="Plan a technology migration.", func=migration_plan),
            StructuredTool.from_function(name="onboard", description="Generate onboarding guide.", func=onboard_codebase),
            StructuredTool.from_function(name="analyze_imports", description="Analyze Python imports.", func=analyze_imports),
            StructuredTool.from_function(name="generate_infra", description="Generate infrastructure code.", func=generate_infra),
            StructuredTool.from_function(name="replay_actions", description="Replay agent actions.", func=replay_actions),
        ]
