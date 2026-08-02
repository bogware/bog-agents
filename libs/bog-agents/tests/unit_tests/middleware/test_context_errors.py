"""Unit tests for `is_context_length_error` classification."""

from langchain_core.exceptions import ContextOverflowError

from bog_agents.middleware._context_errors import is_context_length_error


class _StatusError(RuntimeError):
    """Helper exception carrying an HTTP status code."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_context_overflow_error_is_detected() -> None:
    assert is_context_length_error(ContextOverflowError("too long"))


def test_strong_marker_matches_without_status() -> None:
    assert is_context_length_error(RuntimeError("This model's maximum context length is 200000 tokens."))
    assert is_context_length_error(RuntimeError("prompt is too long: 200000 max"))


def test_weak_marker_requires_client_error_status() -> None:
    assert is_context_length_error(_StatusError("context window exceeded", 400))
    assert is_context_length_error(_StatusError("token limit reached", 429))
    # Same message but a 500 status (or none) must NOT match: transient/server errors
    # are never treated as context-length failures.
    assert not is_context_length_error(_StatusError("context window exceeded", 500))
    assert not is_context_length_error(RuntimeError("context window exceeded"))


def test_class_name_substring_match() -> None:
    class ContextWindowExceededError(RuntimeError):
        pass

    assert is_context_length_error(ContextWindowExceededError("any message"))
    # Class-name only matches when the name identifies the condition.
    assert not is_context_length_error(RuntimeError(""))


def test_unrelated_errors_are_rejected() -> None:
    assert not is_context_length_error(RuntimeError("Connection reset by peer"))
    assert not is_context_length_error(ValueError("invalid response format"))
    assert not is_context_length_error(_StatusError("rate limited", 429))
