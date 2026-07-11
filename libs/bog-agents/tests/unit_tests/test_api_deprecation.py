"""Tests for `bog_agents._api.deprecation`."""

import inspect
import warnings

import pytest

from bog_agents._api.deprecation import (
    LangChainDeprecationWarning,
    deprecated,
    reset_deprecation_dedupe,
    suppress_langchain_deprecation_warning,
    warn_deprecated,
)


@deprecated(since="0.9.0", removal="1.0.0", alternative="new_thing")
def old_thing(a: int, b: str = "x", *, c: bool = False) -> str:
    """Do the old thing.

    Args:
        a: First.
        b: Second.
        c: Third.

    Returns:
        A string.
    """
    return f"{a}{b}{c}"


class _Holder:
    @property
    @deprecated(since="0.9.0")
    def old_prop(self) -> int:
        """An old property."""
        return 7


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_deprecation_dedupe(old_thing, _Holder.__dict__["old_prop"])


def test_warns_once_by_default() -> None:
    """The decorator dedupes: the second call emits nothing."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        old_thing(1)
        old_thing(2)

    deprecations = [w for w in caught if issubclass(w.category, LangChainDeprecationWarning)]
    assert len(deprecations) == 1
    assert "old_thing" in str(deprecations[0].message)


def test_reset_deprecation_dedupe_re_arms_the_warning() -> None:
    """After a reset the decorated callable warns again."""
    with warnings.catch_warnings(record=True) as first:
        warnings.simplefilter("always")
        old_thing(1)
        old_thing(2)
    assert len([w for w in first if issubclass(w.category, LangChainDeprecationWarning)]) == 1

    reset_deprecation_dedupe(old_thing)

    with warnings.catch_warnings(record=True) as second:
        warnings.simplefilter("always")
        old_thing(3)
    assert len([w for w in second if issubclass(w.category, LangChainDeprecationWarning)]) == 1


def test_reset_deprecation_dedupe_handles_properties() -> None:
    """A `property` target is unwrapped to its `fget` closure."""
    holder = _Holder()

    with warnings.catch_warnings(record=True) as first:
        warnings.simplefilter("always")
        assert holder.old_prop == 7
        assert holder.old_prop == 7
    assert len([w for w in first if issubclass(w.category, LangChainDeprecationWarning)]) == 1

    reset_deprecation_dedupe(_Holder.__dict__["old_prop"])

    with warnings.catch_warnings(record=True) as second:
        warnings.simplefilter("always")
        assert holder.old_prop == 7
    assert len([w for w in second if issubclass(w.category, LangChainDeprecationWarning)]) == 1


def test_reset_deprecation_dedupe_ignores_undecorated_targets() -> None:
    """Non-decorated callables and odd objects are skipped, not raised on."""

    def plain() -> None:
        return None

    def closure_without_warned() -> int:
        return value

    value = 3

    # Must not raise.
    reset_deprecation_dedupe(plain, closure_without_warned, object(), 42, None)


def test_decorator_preserves_signature_and_docstring() -> None:
    """The wrapped function keeps its name, signature, and docstring body."""
    assert old_thing.__name__ == "old_thing"

    sig = inspect.signature(old_thing)
    assert list(sig.parameters) == ["a", "b", "c"]
    assert sig.parameters["b"].default == "x"
    assert sig.parameters["c"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["c"].default is False
    assert sig.return_annotation is str

    doc = old_thing.__doc__ or ""
    assert "Do the old thing." in doc
    # langchain_core prepends a deprecation admonition to the original docstring.
    assert "deprecated" in doc.lower()

    # The wrapper still forwards args through to the real body.
    assert old_thing(1, "y", c=True) == "1yTrue"


def test_warn_deprecated_attributes_to_caller_stacklevel() -> None:
    """`stacklevel` is honoured: the warning points at the user's call site."""

    def deprecated_method_body() -> None:
        warn_deprecated("0.9.0", name="thing", removal="1.0.0", stacklevel=3)

    def user_call_site() -> None:
        deprecated_method_body()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        user_call_site()
        expected_line = inspect.currentframe().f_lineno - 1

    assert len(caught) == 1
    assert caught[0].filename == __file__
    assert caught[0].lineno == expected_line
    assert "thing" in str(caught[0].message)


def test_suppress_langchain_deprecation_warning_silences_emissions() -> None:
    """The context manager silences warnings from both helpers."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with suppress_langchain_deprecation_warning():
            old_thing(1)
            warn_deprecated("0.9.0", name="thing")

    assert [w for w in caught if issubclass(w.category, LangChainDeprecationWarning)] == []
