# ruff: noqa: S404,S603  -- subprocess required for docker CLI; args are list-form (no shell injection)
"""Docker sandbox backend for bog-agents-cli.

Runs agent commands inside a Docker container. The host working directory
is mounted read-write so file edits made by the agent are reflected on disk.

Requirements:
- Docker Engine installed and the ``docker`` CLI on PATH.
- The specified image pulled (or pullable at start).

Usage::

    bog-agents --sandbox docker
    bog-agents --sandbox docker --sandbox-id <container-name-or-id>

Environment variables:
    BOG_DOCKER_IMAGE   Base image to use (default: ``python:3.11-slim``).
    BOG_DOCKER_MEMORY  Memory limit (default: ``2g``).
    BOG_DOCKER_CPUS    CPU quota (default: ``2.0``).
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from bog_agents.backends.protocol import ExecuteResponse
from bog_agents.backends.sandbox import BaseSandbox

from bog_agents_cli.integrations.sandbox_provider import SandboxError, SandboxProvider

_DEFAULT_IMAGE = "python:3.11-slim"
_DEFAULT_MEMORY = "2g"
_DEFAULT_CPUS = "2.0"
_CONTAINER_WORKDIR = "/workspace"


class DockerBackend(BaseSandbox):
    """Sandbox backend that executes commands inside a Docker container.

    The host's current working directory is mounted at /workspace inside the
    container, giving the agent read-write access to the project files.

    Attributes:
        _container_id: Docker container ID (short hash).
        _image: Docker image used for this container.
    """

    def __init__(self, container_id: str, *, image: str = _DEFAULT_IMAGE) -> None:
        """Initialise with a running container.

        Args:
            container_id: Running container ID or name.
            image: Image the container was started from.
        """
        self._container_id = container_id
        self._image = image

    @property
    def id(self) -> str:
        """Unique identifier for this sandbox (container ID)."""
        return self._container_id

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Execute a shell command inside the container.

        Args:
            command: Shell command string to execute.
            timeout: Max seconds to wait (default: 300).

        Returns:
            ExecuteResponse with stdout+stderr merged, exit_code, and container id.
        """
        timeout = timeout or 300
        try:
            result = subprocess.run(
                ["docker", "exec", self._container_id, "bash", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            combined = result.stdout + (("\n" + result.stderr) if result.stderr else "")
            return ExecuteResponse(
                output=combined.strip(),
                exit_code=result.returncode,
                sandbox_id=self._container_id,
            )
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output=f"Command timed out after {timeout}s",
                exit_code=124,
                sandbox_id=self._container_id,
            )
        except Exception as exc:
            return ExecuteResponse(
                output=f"docker exec failed: {exc}",
                exit_code=1,
                sandbox_id=self._container_id,
            )


class DockerProvider(SandboxProvider):
    """Lifecycle manager for Docker sandbox containers.

    Creates containers with the host working directory mounted, installs
    common Python tooling, and stops/removes them on cleanup.

    Attributes:
        _image: Docker image to use.
        _memory: Container memory limit.
        _cpus: Container CPU quota.
        _cwd: Host directory to mount at /workspace.
    """

    def __init__(
        self,
        *,
        image: str | None = None,
        memory: str | None = None,
        cpus: str | None = None,
        cwd: str | None = None,
    ) -> None:
        """Initialise the provider.

        Args:
            image: Docker image name (defaults to BOG_DOCKER_IMAGE env or python:3.11-slim).
            memory: Memory limit string e.g. '2g' (defaults to BOG_DOCKER_MEMORY env).
            cpus: CPU quota string e.g. '2.0' (defaults to BOG_DOCKER_CPUS env).
            cwd: Host directory to mount (defaults to current working directory).
        """
        self._image = image or os.environ.get("BOG_DOCKER_IMAGE", _DEFAULT_IMAGE)
        self._memory = memory or os.environ.get("BOG_DOCKER_MEMORY", _DEFAULT_MEMORY)
        self._cpus = cpus or os.environ.get("BOG_DOCKER_CPUS", _DEFAULT_CPUS)
        self._cwd = cwd or str(Path.cwd())

    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        **kwargs: Any,  # noqa: ARG002
    ) -> DockerBackend:
        """Return an existing container or start a new one.

        Args:
            sandbox_id: Existing container name or ID to reuse.
            **kwargs: Ignored (for interface compatibility).

        Returns:
            DockerBackend wrapping the container.
        """
        if sandbox_id:
            return self._attach(sandbox_id)
        return self._start_new()

    def delete(self, *, sandbox_id: str, **kwargs: Any) -> None:  # noqa: ARG002,PLR6301
        """Stop and remove a container.

        Args:
            sandbox_id: Container name or ID to remove.
            **kwargs: Ignored.

        Raises:
            SandboxError: If Docker remove command fails.
        """
        try:
            subprocess.run(
                ["docker", "rm", "-f", sandbox_id],
                capture_output=True,
                check=False,
                timeout=30,
            )
        except Exception as exc:
            msg = f"Failed to remove Docker container {sandbox_id}"
            raise SandboxError(msg) from exc

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_docker() -> None:
        """Raise SandboxError if docker CLI is not available.

        Raises:
            SandboxError: When docker is not installed or its daemon is not running.
        """
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                msg = "Docker daemon is not running. Start Docker and try again."
                raise SandboxError(msg)
        except FileNotFoundError as exc:
            msg = "Docker CLI not found. Install Docker: https://docs.docker.com/get-docker/"
            raise SandboxError(msg) from exc

    def _start_new(self) -> DockerBackend:
        """Start a fresh container and return a DockerBackend for it.

        Returns:
            DockerBackend wrapping the new container.

        Raises:
            SandboxError: If the container fails to start.
        """
        self._check_docker()

        name = f"bog-agents-{uuid.uuid4().hex[:8]}"
        host_dir = str(Path(self._cwd).resolve())

        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--memory",
            self._memory,
            "--cpus",
            self._cpus,
            "-v",
            f"{host_dir}:{_CONTAINER_WORKDIR}",
            "-w",
            _CONTAINER_WORKDIR,
            "--rm",
            self._image,
            "tail",
            "-f",
            "/dev/null",  # keep alive
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, check=False
            )
        except Exception as exc:
            msg = f"Failed to start Docker container: {exc}"
            raise SandboxError(msg) from exc

        if result.returncode != 0:
            msg = f"Docker container failed to start:\n{result.stderr.strip()}"
            raise SandboxError(msg)

        container_id = result.stdout.strip()[:12]
        backend = DockerBackend(container_id, image=self._image)

        # Bootstrap: install pip if missing (slim images often omit it)
        backend.execute(
            "which pip3 || (apt-get update -qq && apt-get install -y -qq python3-pip)"
        )

        return backend

    def _attach(self, sandbox_id: str) -> DockerBackend:
        """Attach to an existing running container.

        Args:
            sandbox_id: Container name or ID.

        Returns:
            DockerBackend wrapping the existing container.

        Raises:
            SandboxError: If the container is not running.
        """
        self._check_docker()
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", sandbox_id],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception as exc:
            msg = f"Cannot inspect Docker container {sandbox_id}"
            raise SandboxError(msg) from exc

        if result.stdout.strip() != "true":
            msg = f"Container '{sandbox_id}' is not running. Start it first or omit --sandbox-id."
            raise SandboxError(msg)
        return DockerBackend(sandbox_id, image=self._image)

    @staticmethod
    def is_available() -> bool:
        """Return True if the docker CLI is on PATH and the daemon responds.

        Returns:
            True when Docker is usable, False otherwise.
        """
        try:
            result = subprocess.run(
                ["docker", "info"], capture_output=True, timeout=5, check=False
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def detect_devcontainer(repo_path: str = ".") -> dict[str, str] | None:
        """Detect a devcontainer configuration in *repo_path*.

        Looks for `.devcontainer/devcontainer.json` or `.devcontainer.json`.
        Returns a dict with ``image`` and ``name`` keys if found.

        Args:
            repo_path: Directory to search.

        Returns:
            Dict with detected config, or None if no devcontainer found.
        """
        import json

        candidates = [
            Path(repo_path) / ".devcontainer" / "devcontainer.json",
            Path(repo_path) / ".devcontainer.json",
        ]
        for candidate in candidates:
            if candidate.exists():
                try:
                    data = json.loads(candidate.read_text(encoding="utf-8"))
                    image = data.get("image", _DEFAULT_IMAGE)
                    name = data.get("name", "devcontainer")
                    return {"image": image, "name": name}
                except Exception:
                    return None
        return None


def get_docker_provider(*, cwd: str | None = None) -> DockerProvider:
    """Convenience factory respecting devcontainer.json image selection.

    Args:
        cwd: Project directory (defaults to cwd).

    Returns:
        DockerProvider configured with the devcontainer image (if found) or
        the default image.
    """
    cwd = cwd or str(Path.cwd())
    devcontainer = DockerProvider.detect_devcontainer(cwd)
    image = devcontainer["image"] if devcontainer else None
    return DockerProvider(image=image, cwd=cwd)
