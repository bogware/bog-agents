"""Security Audit middleware for automated codebase security scanning.

Scans for OWASP top 10 vulnerabilities, dependency CVEs, secret leaks,
insecure patterns, and generates actionable security reports.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class Severity(StrEnum):
    """Severity levels for security findings."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingCategory(StrEnum):
    """Categories of security findings."""

    SECRET_LEAK = "secret_leak"
    INJECTION = "injection"
    XSS = "xss"
    INSECURE_DESERIALIZATION = "insecure_deserialization"
    BROKEN_AUTH = "broken_auth"
    SENSITIVE_DATA_EXPOSURE = "sensitive_data_exposure"
    INSECURE_DEPENDENCY = "insecure_dependency"
    SECURITY_MISCONFIGURATION = "security_misconfiguration"
    INSUFFICIENT_LOGGING = "insufficient_logging"
    SSRF = "ssrf"
    PATH_TRAVERSAL = "path_traversal"
    COMMAND_INJECTION = "command_injection"
    INSECURE_CRYPTO = "insecure_crypto"
    HARDCODED_CREDENTIALS = "hardcoded_credentials"


@dataclass
class SecurityFinding:
    """A single security finding."""

    finding_id: str
    category: FindingCategory
    severity: Severity
    title: str
    description: str
    file_path: str | None = None
    line_number: int | None = None
    code_snippet: str | None = None
    recommendation: str = ""
    cwe_id: str | None = None
    owasp_category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "finding_id": self.finding_id,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "description": self.description,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "code_snippet": self.code_snippet,
            "recommendation": self.recommendation,
            "cwe_id": self.cwe_id,
            "owasp_category": self.owasp_category,
        }


@dataclass
class SecurityReport:
    """Complete security audit report."""

    scan_time: float = field(default_factory=time.time)
    duration_seconds: float = 0.0
    target_directory: str = ""
    findings: list[SecurityFinding] = field(default_factory=list)
    files_scanned: int = 0
    dependencies_checked: int = 0

    @property
    def critical_count(self) -> int:
        """Number of critical findings."""
        return sum(1 for f in self.findings if f.severity == Severity.CRITICAL)

    @property
    def high_count(self) -> int:
        """Number of high-severity findings."""
        return sum(1 for f in self.findings if f.severity == Severity.HIGH)

    @property
    def total_findings(self) -> int:
        """Total number of findings."""
        return len(self.findings)

    @property
    def severity_summary(self) -> dict[str, int]:
        """Count of findings by severity."""
        counts: dict[str, int] = {}
        for sev in Severity:
            count = sum(1 for f in self.findings if f.severity == sev)
            if count > 0:
                counts[sev] = count
        return counts

    def to_markdown(self) -> str:
        """Generate a markdown report.

        Returns:
            Formatted markdown string.
        """
        lines: list[str] = []
        lines.append("# Security Audit Report")
        lines.append(f"\nScan target: `{self.target_directory}`")
        lines.append(f"Files scanned: {self.files_scanned}")
        lines.append(f"Duration: {self.duration_seconds:.1f}s")
        lines.append(f"\n## Summary: {self.total_findings} findings")

        for sev, count in self.severity_summary.items():
            lines.append(f"- **{sev.upper()}**: {count}")

        if not self.findings:
            lines.append("\nNo security issues found.")
            return "\n".join(lines)

        # Group by severity
        for sev in Severity:
            sev_findings = [f for f in self.findings if f.severity == sev]
            if not sev_findings:
                continue

            lines.append(f"\n## {sev.upper()} ({len(sev_findings)})")
            for finding in sev_findings:
                lines.append(f"\n### {finding.title}")
                lines.append(f"**Category:** {finding.category}")
                if finding.cwe_id:
                    lines.append(f"**CWE:** {finding.cwe_id}")
                if finding.owasp_category:
                    lines.append(f"**OWASP:** {finding.owasp_category}")
                if finding.file_path:
                    loc = finding.file_path
                    if finding.line_number:
                        loc += f":{finding.line_number}"
                    lines.append(f"**Location:** `{loc}`")
                lines.append(f"\n{finding.description}")
                if finding.code_snippet:
                    lines.append(f"\n```\n{finding.code_snippet}\n```")
                if finding.recommendation:
                    lines.append(f"\n**Fix:** {finding.recommendation}")

        return "\n".join(lines)


# ── Secret Detection Patterns ──────────────────────────────────────

SECRET_PATTERNS: list[tuple[str, str, Severity]] = [
    # API Keys
    (r'(?i)(api[_-]?key|apikey)\s*[=:]\s*["\']([a-zA-Z0-9_\-]{20,})["\']', "API key in source code", Severity.HIGH),
    (r'(?i)(secret[_-]?key|secretkey)\s*[=:]\s*["\']([a-zA-Z0-9_\-]{20,})["\']', "Secret key in source code", Severity.CRITICAL),
    # AWS
    (r'AKIA[0-9A-Z]{16}', "AWS Access Key ID", Severity.CRITICAL),
    (r'(?i)aws[_-]?secret[_-]?access[_-]?key\s*[=:]\s*["\']([a-zA-Z0-9/+=]{40})["\']', "AWS Secret Access Key", Severity.CRITICAL),
    # Passwords
    (r'(?i)(password|passwd|pwd)\s*[=:]\s*["\']([^\s"\']{8,})["\']', "Hardcoded password", Severity.HIGH),
    # Tokens
    (r'(?i)(token|bearer)\s*[=:]\s*["\']([a-zA-Z0-9_\-.]{20,})["\']', "Hardcoded token", Severity.HIGH),
    # Private keys
    (r'-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----', "Private key in source", Severity.CRITICAL),
    # Connection strings
    (r'(?i)(mongodb|postgres|mysql|redis)://[^\s"\']+:[^\s"\']+@', "Database connection string with credentials", Severity.HIGH),
    # GitHub tokens
    (r'gh[pousr]_[a-zA-Z0-9]{36,}', "GitHub token", Severity.CRITICAL),
    # Slack tokens
    (r'xox[bpors]-[a-zA-Z0-9-]+', "Slack token", Severity.HIGH),
]

# ── Code Pattern Detectors ──────────────────────────────────────

PYTHON_PATTERNS: list[tuple[str, FindingCategory, Severity, str, str]] = [
    # SQL injection
    (
        r'(?:execute|cursor\.execute)\s*\(\s*[f"\'].*\{.*\}',
        FindingCategory.INJECTION,
        Severity.HIGH,
        "Potential SQL injection via f-string",
        "Use parameterized queries instead of string formatting",
    ),
    (
        r'(?:execute|cursor\.execute)\s*\(\s*.*%\s*\(',
        FindingCategory.INJECTION,
        Severity.HIGH,
        "Potential SQL injection via % formatting",
        "Use parameterized queries with ? or %s placeholders",
    ),
    # Command injection
    (
        r'(?:os\.system|os\.popen|subprocess\.call)\s*\(\s*[f"\']',
        FindingCategory.COMMAND_INJECTION,
        Severity.HIGH,
        "Potential command injection via string formatting",
        "Use subprocess.run with a list of arguments instead of shell=True",
    ),
    (
        r'subprocess\.\w+\(.*shell\s*=\s*True',
        FindingCategory.COMMAND_INJECTION,
        Severity.MEDIUM,
        "subprocess with shell=True",
        "Avoid shell=True; pass command as a list",
    ),
    # Insecure deserialization
    (
        r'pickle\.loads?\(',
        FindingCategory.INSECURE_DESERIALIZATION,
        Severity.HIGH,
        "Pickle deserialization (arbitrary code execution risk)",
        "Use JSON or other safe serialization formats for untrusted data",
    ),
    (
        r'yaml\.load\s*\([^)]*\)(?!.*Loader)',
        FindingCategory.INSECURE_DESERIALIZATION,
        Severity.MEDIUM,
        "yaml.load without safe Loader",
        "Use yaml.safe_load() or specify Loader=yaml.SafeLoader",
    ),
    # Path traversal
    (
        r'open\s*\(\s*(?:os\.path\.join|f["\']|request)',
        FindingCategory.PATH_TRAVERSAL,
        Severity.MEDIUM,
        "Potential path traversal in file open",
        "Validate and sanitize file paths; use pathlib with resolve()",
    ),
    # Insecure crypto
    (
        r'(?:md5|sha1)\s*\(',
        FindingCategory.INSECURE_CRYPTO,
        Severity.LOW,
        "Weak hash function (MD5/SHA1)",
        "Use SHA-256 or stronger for security-sensitive hashing",
    ),
    (
        r'(?:DES|Blowfish|RC4)',
        FindingCategory.INSECURE_CRYPTO,
        Severity.MEDIUM,
        "Weak encryption algorithm",
        "Use AES-256-GCM or ChaCha20-Poly1305",
    ),
    # SSRF
    (
        r'requests\.(?:get|post|put|delete)\s*\(\s*(?:f["\']|request\.|user|input)',
        FindingCategory.SSRF,
        Severity.MEDIUM,
        "Potential SSRF via user-controlled URL",
        "Validate and allowlist target URLs",
    ),
    # Eval
    (
        r'\beval\s*\(',
        FindingCategory.INJECTION,
        Severity.HIGH,
        "Use of eval() — arbitrary code execution risk",
        "Replace eval() with ast.literal_eval() or a safe parser",
    ),
    (
        r'\bexec\s*\(',
        FindingCategory.INJECTION,
        Severity.HIGH,
        "Use of exec() — arbitrary code execution risk",
        "Avoid exec(); use structured approaches instead",
    ),
]

JS_PATTERNS: list[tuple[str, FindingCategory, Severity, str, str]] = [
    (
        r'innerHTML\s*=',
        FindingCategory.XSS,
        Severity.MEDIUM,
        "Direct innerHTML assignment (XSS risk)",
        "Use textContent or a sanitization library like DOMPurify",
    ),
    (
        r'document\.write\s*\(',
        FindingCategory.XSS,
        Severity.MEDIUM,
        "document.write usage (XSS risk)",
        "Use DOM manipulation methods instead",
    ),
    (
        r'\beval\s*\(',
        FindingCategory.INJECTION,
        Severity.HIGH,
        "Use of eval() — arbitrary code execution",
        "Use JSON.parse() or structured alternatives",
    ),
    (
        r'dangerouslySetInnerHTML',
        FindingCategory.XSS,
        Severity.MEDIUM,
        "React dangerouslySetInnerHTML usage",
        "Sanitize HTML with DOMPurify before rendering",
    ),
]

# Files/dirs to skip during scanning
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", "dist", "build", ".egg-info"}
SKIP_EXTENSIONS = {".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".bin", ".jpg", ".png", ".gif", ".ico", ".woff", ".ttf"}


def scan_file_for_secrets(
    file_path: str,
    content: str,
    finding_counter: list[int],
) -> list[SecurityFinding]:
    """Scan a file for secret leaks.

    Args:
        file_path: Path to the file.
        content: File contents.
        finding_counter: Mutable counter for generating finding IDs.

    Returns:
        List of findings.
    """
    findings: list[SecurityFinding] = []

    for pattern, title, severity in SECRET_PATTERNS:
        for match in re.finditer(pattern, content):
            line_num = content[:match.start()].count("\n") + 1
            # Get the line for context
            lines = content.split("\n")
            snippet = lines[line_num - 1].strip() if line_num <= len(lines) else ""

            finding_counter[0] += 1
            findings.append(SecurityFinding(
                finding_id=f"SEC-{finding_counter[0]:04d}",
                category=FindingCategory.SECRET_LEAK,
                severity=severity,
                title=title,
                description=f"Potential secret found in {file_path}",
                file_path=file_path,
                line_number=line_num,
                code_snippet=snippet[:200],
                recommendation="Move secrets to environment variables or a secrets manager",
                cwe_id="CWE-798",
                owasp_category="A07:2021-Identification and Authentication Failures",
            ))

    return findings


def scan_file_for_patterns(
    file_path: str,
    content: str,
    finding_counter: list[int],
) -> list[SecurityFinding]:
    """Scan a file for insecure code patterns.

    Args:
        file_path: Path to the file.
        content: File contents.
        finding_counter: Mutable counter for generating finding IDs.

    Returns:
        List of findings.
    """
    findings: list[SecurityFinding] = []

    # Select patterns based on file type
    ext = Path(file_path).suffix.lower()
    patterns: list[tuple[str, FindingCategory, Severity, str, str]] = []
    if ext in (".py", ".pyw"):
        patterns = PYTHON_PATTERNS
    elif ext in (".js", ".jsx", ".ts", ".tsx", ".mjs"):
        patterns = JS_PATTERNS

    for pattern, category, severity, title, recommendation in patterns:
        for match in re.finditer(pattern, content):
            line_num = content[:match.start()].count("\n") + 1
            lines = content.split("\n")
            snippet = lines[line_num - 1].strip() if line_num <= len(lines) else ""

            finding_counter[0] += 1
            findings.append(SecurityFinding(
                finding_id=f"SEC-{finding_counter[0]:04d}",
                category=category,
                severity=severity,
                title=title,
                description=f"Found in {file_path}:{line_num}",
                file_path=file_path,
                line_number=line_num,
                code_snippet=snippet[:200],
                recommendation=recommendation,
            ))

    return findings


def scan_directory(
    target_dir: str,
    *,
    max_file_size: int = 1_000_000,
    exclude_files: list[str] | None = None,
) -> SecurityReport:
    """Scan a directory for security issues.

    Args:
        target_dir: Directory to scan.
        max_file_size: Maximum file size to scan (bytes).
        exclude_files: File paths (relative to target_dir) to skip.

    Returns:
        SecurityReport with all findings.
    """
    start = time.time()
    report = SecurityReport(target_directory=target_dir)
    finding_counter = [0]
    files_scanned = 0

    target = Path(target_dir)
    if not target.is_dir():
        logger.error("Target directory does not exist: %s", target_dir)
        return report

    _exclude_set = set(exclude_files or [])
    # Always exclude the scanner itself to avoid self-detection
    _exclude_set.add("middleware/security_audit.py")

    for root, dirs, files in os.walk(target):
        # Skip excluded directories
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for filename in files:
            file_path = os.path.join(root, filename)
            rel_path = os.path.relpath(file_path, target_dir)

            if rel_path in _exclude_set:
                continue

            ext = Path(filename).suffix.lower()

            if ext in SKIP_EXTENSIONS:
                continue

            try:
                size = os.path.getsize(file_path)
                if size > max_file_size or size == 0:
                    continue

                content = Path(file_path).read_text(errors="ignore")
                files_scanned += 1

                # Relative path for cleaner reporting
                rel_path = os.path.relpath(file_path, target_dir)

                # Secret scanning
                report.findings.extend(
                    scan_file_for_secrets(rel_path, content, finding_counter)
                )

                # Pattern scanning
                report.findings.extend(
                    scan_file_for_patterns(rel_path, content, finding_counter)
                )
            except (OSError, UnicodeDecodeError):
                continue

    report.files_scanned = files_scanned
    report.duration_seconds = time.time() - start

    # Sort by severity
    severity_order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3, Severity.INFO: 4}
    report.findings.sort(key=lambda f: severity_order.get(f.severity, 99))

    logger.info(
        "Security scan complete: %d files, %d findings in %.1fs",
        files_scanned, len(report.findings), report.duration_seconds,
    )
    return report


def check_python_dependencies(target_dir: str) -> list[SecurityFinding]:
    """Check Python dependencies for known vulnerabilities using pip-audit.

    Args:
        target_dir: Project directory.

    Returns:
        List of dependency vulnerability findings.
    """
    findings: list[SecurityFinding] = []
    req_files = ["requirements.txt", "requirements-dev.txt"]
    counter = [1000]  # Start from 1000 to avoid ID collisions

    for req_file in req_files:
        req_path = os.path.join(target_dir, req_file)
        if not os.path.exists(req_path):
            continue

        try:
            result = subprocess.run(
                ["pip-audit", "-r", req_path, "--format", "json"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=target_dir,
            )
            if result.returncode == 0:
                continue  # No vulnerabilities

            try:
                audit_data = json.loads(result.stdout)
                for vuln in audit_data.get("dependencies", []):
                    for v in vuln.get("vulns", []):
                        counter[0] += 1
                        findings.append(SecurityFinding(
                            finding_id=f"DEP-{counter[0]:04d}",
                            category=FindingCategory.INSECURE_DEPENDENCY,
                            severity=Severity.HIGH,
                            title=f"Vulnerable dependency: {vuln['name']} {vuln.get('version', '')}",
                            description=v.get("description", "Known vulnerability"),
                            file_path=req_file,
                            recommendation=f"Upgrade {vuln.get('name', 'unknown')} to {v.get('fix_versions', ['latest'])[0] if isinstance(v.get('fix_versions'), list) else 'latest'}",
                            cwe_id="CWE-1104",
                        ))
            except (json.JSONDecodeError, KeyError):
                pass
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.debug("pip-audit not available or timed out")

    return findings


class SecurityAuditMiddleware(AgentMiddleware):
    """Middleware for automated security auditing of codebases.

    Provides tools for scanning code for secrets, insecure patterns,
    and dependency vulnerabilities. Generates actionable reports.

    Example:
        ```python
        from bog_agents.middleware.security_audit import SecurityAuditMiddleware

        middleware = SecurityAuditMiddleware(working_dir="/path/to/project")
        report = middleware.run_audit()
        print(report.to_markdown())
        ```
    """

    working_dir: str
    _last_report: SecurityReport | None

    def __init__(
        self,
        *,
        working_dir: str = ".",
    ) -> None:
        """Initialize security audit middleware.

        Args:
            working_dir: Directory to audit.
        """
        self.working_dir = os.path.abspath(working_dir)
        self._last_report = None

    def run_audit(self, *, check_deps: bool = True) -> SecurityReport:
        """Run a full security audit.

        Args:
            check_deps: Whether to check dependencies for CVEs.

        Returns:
            SecurityReport with all findings.
        """
        report = scan_directory(self.working_dir)

        if check_deps:
            dep_findings = check_python_dependencies(self.working_dir)
            report.findings.extend(dep_findings)
            report.dependencies_checked = len(dep_findings)

        self._last_report = report
        return report

    def scan_file(self, file_path: str) -> list[SecurityFinding]:
        """Scan a single file for security issues.

        Args:
            file_path: Path to the file (relative or absolute).

        Returns:
            List of findings for this file.
        """
        abs_path = os.path.join(self.working_dir, file_path)
        counter = [0]
        findings: list[SecurityFinding] = []

        try:
            content = Path(abs_path).read_text(errors="ignore")
            findings.extend(scan_file_for_secrets(file_path, content, counter))
            findings.extend(scan_file_for_patterns(file_path, content, counter))
        except OSError as exc:
            logger.warning("Cannot scan %s: %s", file_path, exc)

        return findings

    @property
    def last_report(self) -> SecurityReport | None:
        """Get the last audit report."""
        return self._last_report

    async def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Any,
        runtime: Any,
    ) -> ModelResponse[ResponseT]:
        """Pass through — auditing is invoked on demand."""
        return await call_next(request, runtime)
