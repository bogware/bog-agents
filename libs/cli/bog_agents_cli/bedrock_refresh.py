"""Auto-refresh middleware for expired Bedrock credentials.

The motivating user complaint: "if bedrock is selected and it fails, it
should try to login/refresh/help the user refresh/login, it shouldnt just
fail — we need resiliency."

This middleware sits in front of Bedrock model calls and catches the
``CREDENTIALS_EXPIRED`` shape (``ExpiredTokenException`` /
``SSOTokenLoadError`` / ``UnauthorizedSSOTokenError``). On a hit it
attempts to refresh the local credentials cache:

- **SSO mode (or `auto` resolved to SSO)** — spawn ``aws sso login``
  as a subprocess. AWS opens a browser tab for the OAuth approval; we
  block until the subprocess exits, then retry the model call once.
- **Profile mode** — reload the profile section from
  ``~/.aws/credentials`` / ``~/.aws/config``. Boto3's profile-based
  refresh handles ``role_arn`` chains automatically when present.
- **Static / IAM modes** — we cannot safely re-derive STS tokens
  without knowing the upstream tool (aws-vault, Granted, Vault, the
  user's CI). Surface the categorized ``BedrockError`` so the user
  sees the exact next step and the failure mode is loud, not silent.

Budget: one refresh per failed call, three refreshes per Python
process. After three, further failures bypass the refresh path —
something is wrong with the SSO configuration and looping won't
unbreak it.

Non-interactive mode: ``interactive=False`` (the default in `bog-agents
-p` and the daemon) prints the actionable error banner to stderr and
re-raises without spawning a subprocess. Headless callers see the
same fix command they would have seen pre-Wave-4, just with a clearer
signal at the right layer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess  # noqa: S404 — only used for `aws sso login`, never user input
import sys
import threading
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware

from bog_agents_cli._bedrock import (
    BedrockErrorKind,
    categorize_bedrock_error,
)

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ModelRequest, ModelResponse

logger = logging.getLogger(__name__)


# Session-wide counters. Module-level so they persist across middleware
# instances within a single Python process. A fresh `bog-agents`
# invocation gets a fresh budget.
_SESSION_REFRESH_COUNT: int = 0
_USER_PROMPTED_THIS_SESSION: bool = False
_DEFAULT_MAX_REFRESHES_PER_SESSION: int = 3

# Serializes the whole check-then-act refresh critical section. Without
# it, concurrent expired-creds model calls (parallel sub-agents, each
# running under ``asyncio.to_thread``) can all pass the budget check
# before anyone increments the counter — spawning multiple ``aws sso
# login`` subprocesses that race on the same SSO cache and blowing past
# the cap. Holding the lock across the subprocess spawn also guarantees
# at most one ``aws sso login`` is in flight at a time.
_REFRESH_LOCK = threading.Lock()


def _reset_session_state_for_tests() -> None:
    """Test-only: reset the module-level counters between cases.

    Pytest doesn't isolate module state across tests, so test files
    that exercise the refresh budget must call this in setup/teardown.
    """
    global _SESSION_REFRESH_COUNT, _USER_PROMPTED_THIS_SESSION  # noqa: PLW0603
    _SESSION_REFRESH_COUNT = 0
    _USER_PROMPTED_THIS_SESSION = False


def _can_run_aws_cli() -> bool:
    """Return True when the `aws` CLI is on PATH.

    The refresh path needs ``aws sso login``; without the CLI we can
    only surface the actionable error.
    """
    return shutil.which("aws") is not None


def _resolved_profile() -> str:
    """Return the AWS profile name that ``aws sso login`` should target.

    Precedence: ``BOG_AGENTS_BEDROCK_PROFILE`` > ``AWS_PROFILE`` >
    ``"default"``. Returning the empty string would make ``aws sso
    login`` operate on the implicit default profile, which is fine but
    less self-documenting in logs.
    """
    return (
        os.environ.get("BOG_AGENTS_BEDROCK_PROFILE")
        or os.environ.get("AWS_PROFILE")
        or "default"
    )


def _attempt_sso_login(profile: str, *, timeout_s: float = 120.0) -> bool:
    """Spawn ``aws sso login --profile <profile>`` and wait for it to exit.

    Returns:
        True when the subprocess exits 0 (browser approval completed
        and credentials were cached). False on timeout, non-zero exit,
        missing CLI, or any other failure.
    """
    if not _can_run_aws_cli():
        logger.warning(
            "bedrock_refresh: cannot run 'aws sso login' — `aws` CLI not on PATH"
        )
        return False
    cmd = ["aws", "sso", "login", "--profile", profile]
    logger.info("bedrock_refresh: running %s", " ".join(cmd))
    try:
        # subprocess.run with timeout — `aws sso login` blocks until the
        # user approves in the browser. 2 minutes is enough for the
        # average human; users with slow networks can re-run manually.
        result = subprocess.run(  # noqa: S603 — argv is a fixed list, no shell, no user-injected args
            cmd,
            check=False,
            timeout=timeout_s,
            text=True,
            capture_output=False,  # let the user see the browser prompt
        )
    except subprocess.TimeoutExpired:
        logger.warning("bedrock_refresh: aws sso login timed out after %ss", timeout_s)
        return False
    except (OSError, ValueError) as exc:
        logger.warning("bedrock_refresh: aws sso login launch failed (%s)", exc)
        return False
    if result.returncode != 0:
        logger.warning(
            "bedrock_refresh: aws sso login exited %d for profile=%s",
            result.returncode,
            profile,
        )
        return False
    return True


def _print_refresh_banner_to_stderr(profile: str) -> None:
    """Write a copy-paste fix to stderr when we won't auto-refresh.

    Used by:
      - Headless / non-interactive callers (where spawning a browser
        would hang).
      - First-time interactive use, so the user sees what's about to
        happen before we spawn the subprocess.
    """
    banner = (
        "\n"
        + "=" * 70
        + "\n"
        + "BEDROCK: SSO credentials expired\n"
        + "=" * 70
        + "\n"
        + f"  Run: aws sso login --profile {profile}\n"
        + "  Or:  export AWS_PROFILE=<your-profile>; aws sso login\n"
        + "  Or:  re-export AWS_SESSION_TOKEN with a fresh value\n"
        + "=" * 70
        + "\n"
    )
    print(banner, file=sys.stderr, flush=True)  # noqa: T201 — intentional banner


class BedrockRefreshMiddleware(AgentMiddleware[Any, Any, Any]):
    """Catches expired-credential failures on Bedrock and retries once.

    Use::

        from bog_agents_cli.bedrock_refresh import BedrockRefreshMiddleware

        agent = create_agent(
            model="bedrock_converse:us.anthropic.claude-opus-4-8",
            middleware=[BedrockRefreshMiddleware(interactive=True)],
        )

    The CLI wires this in automatically when a Bedrock model is in
    use; SDK callers opt in by adding the middleware to their list.

    Args:
        interactive: When True (default for TTY callers), spawn
            ``aws sso login`` on a credential-expired hit. When False
            (headless / `bog-agents -p`), print the fix to stderr and
            re-raise without spawning a subprocess.
        max_refreshes_per_session: Hard cap on refresh attempts across
            the lifetime of this process. Default 3.
    """

    def __init__(
        self,
        *,
        interactive: bool = True,
        max_refreshes_per_session: int = _DEFAULT_MAX_REFRESHES_PER_SESSION,
    ) -> None:
        super().__init__()
        self._interactive = interactive
        self._max_refreshes = max(0, int(max_refreshes_per_session))

    async def awrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Any,  # noqa: ANN401 — langchain middleware contract
    ) -> ModelResponse:
        """Async wrap — catch expired creds, refresh, retry once.

        Raises:
            KeyboardInterrupt: Propagated unchanged.
            SystemExit: Propagated unchanged.
            asyncio.CancelledError: Propagated unchanged.
        """
        try:
            return await call_next(request)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as exc:
            if not self._should_attempt_refresh(exc):
                raise
            refreshed = await asyncio.to_thread(self._refresh_credentials)
            if not refreshed:
                raise
            logger.info("bedrock_refresh: SSO refreshed, retrying model call")
            return await call_next(request)

    def wrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Any,  # noqa: ANN401 — langchain middleware contract
    ) -> ModelResponse:
        """Sync wrap — catch expired creds, refresh, retry once.

        Raises:
            KeyboardInterrupt: Propagated unchanged.
            SystemExit: Propagated unchanged.
        """
        try:
            return call_next(request)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            if not self._should_attempt_refresh(exc):
                raise
            if not self._refresh_credentials():
                raise
            logger.info("bedrock_refresh: SSO refreshed, retrying model call (sync)")
            return call_next(request)

    @staticmethod
    def _should_attempt_refresh(exc: BaseException) -> bool:
        """True when the exception is a Bedrock CREDENTIALS_EXPIRED hit.

        Falls back to safe (no-refresh) when the error doesn't look
        like a Bedrock-credentials problem — we don't want to spawn
        `aws sso login` for a generic 5xx.
        """
        # Cheap shape check first to avoid running the full categoriser
        # on unrelated errors.
        type_name = type(exc).__name__
        bedrock_shaped = (
            type_name
            in {
                "ExpiredTokenException",
                "SSOTokenLoadError",
                "UnauthorizedSSOTokenError",
                "TokenRetrievalError",
                "ClientError",
                "NoCredentialsError",
            }
            or "expired" in str(exc).lower()
        )
        if not bedrock_shaped:
            return False
        try:
            err = categorize_bedrock_error(exc)
        except Exception:  # diagnostic — never mask the original error
            return False
        return err.kind == BedrockErrorKind.CREDENTIALS_EXPIRED

    def _refresh_credentials(self) -> bool:
        """Attempt one credential refresh. Returns success.

        Honors the session budget, prints first-time hint, runs the
        subprocess in interactive mode only, updates module counters.
        """
        global _SESSION_REFRESH_COUNT, _USER_PROMPTED_THIS_SESSION  # noqa: PLW0603

        # Serialize the entire check-then-act region. The lock covers
        # both the sync path and the ``asyncio.to_thread`` path, so
        # concurrent expired-creds calls can't all clear the budget
        # check before any of them increments, and at most one ``aws
        # sso login`` runs at a time (no SSO-cache race).
        with _REFRESH_LOCK:
            if _SESSION_REFRESH_COUNT >= self._max_refreshes:  # noqa: SIM300 — natural ordering for budget check
                logger.warning(
                    "bedrock_refresh: session budget exhausted "
                    "(%d/%d) — not attempting refresh",
                    _SESSION_REFRESH_COUNT,
                    self._max_refreshes,
                )
                return False

            profile = _resolved_profile()

            if not self._interactive:
                # Headless: print the fix and re-raise. No subprocess.
                _print_refresh_banner_to_stderr(profile)
                return False

            # First refresh of the session: show the banner so the user
            # sees what's about to happen before a browser tab pops up.
            if not _USER_PROMPTED_THIS_SESSION:
                _print_refresh_banner_to_stderr(profile)
                _USER_PROMPTED_THIS_SESSION = True

            # Reserve the budget slot before launching so a concurrent
            # caller that acquires the lock next sees the consumed slot.
            _SESSION_REFRESH_COUNT += 1
            attempt_num = _SESSION_REFRESH_COUNT

            ok = _attempt_sso_login(profile)
            if ok:
                logger.info(
                    "bedrock_refresh: refresh #%d succeeded for profile=%s",
                    attempt_num,
                    profile,
                )
            else:
                logger.warning(
                    "bedrock_refresh: refresh #%d FAILED for profile=%s",
                    attempt_num,
                    profile,
                )
            return ok


__all__ = [
    "BedrockRefreshMiddleware",
    "_attempt_sso_login",
    "_reset_session_state_for_tests",
    "_resolved_profile",
]
