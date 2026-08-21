"""Live conformance tests for `DaytonaSandbox` against a real Daytona sandbox.

Requires `DAYTONA_API_KEY` in the environment (the daytona SDK also honors
`DAYTONA_API_URL` / `DAYTONA_TARGET`); the suite is skipped without it.

Historical note: this suite previously subclassed
`langchain_tests.integration_tests.SandboxIntegrationTests`, which was removed
from langchain-tests and made the module fail at collection. The SDK's own
`bog_agents.testing.SandboxConformanceSuite` is the supported replacement and
pins the same structured file surface (`awrite` / `aread_file` / `als` /
`agrep` / `aglob` / `aedit` / `adelete`).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import daytona
import pytest
from bog_agents.testing import SandboxConformanceSuite

from langchain_daytona import DaytonaSandbox

if TYPE_CHECKING:
    from collections.abc import Iterator

    from bog_agents.backends.protocol import SandboxBackendProtocol

pytestmark = pytest.mark.skipif(
    not os.environ.get("DAYTONA_API_KEY"),
    reason="DAYTONA_API_KEY not set; live Daytona sandbox tests skipped",
)


class TestDaytonaSandboxConformance(SandboxConformanceSuite):
    """Run the SDK sandbox-conformance contract against a live Daytona sandbox."""

    # The conformance suite defaults to `/`, which is not writable for the
    # sandbox user; use a dedicated directory instead. The whole sandbox is
    # disposable, so a fixed path under /tmp is fine here.
    root = "/tmp/bog_conformance"  # noqa: S108

    @pytest.fixture(scope="class")
    def sandbox(self) -> Iterator[SandboxBackendProtocol]:
        """Provide a `DaytonaSandbox` wrapping a freshly created live sandbox."""
        sdk = daytona.Daytona()
        live_sandbox = sdk.create()
        backend = DaytonaSandbox(sandbox=live_sandbox)
        backend.execute(f"mkdir -p {self.root}")
        try:
            yield backend
        finally:
            live_sandbox.delete()
