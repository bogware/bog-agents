"""Unit tests for bog_agents_cli.vault."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bog_agents_cli.vault import (
    SecretStr,
    SessionVault,
    get_default_vault,
    reset_default_vault,
)


class TestSecretStr:
    def test_repr_redacts(self):
        s = SecretStr("hunter2")
        assert "hunter2" not in repr(s)
        assert "***" in repr(s)

    def test_str_redacts(self):
        s = SecretStr("hunter2")
        assert str(s) == "***"
        assert "hunter2" not in str(s)

    def test_fstring_redacts(self):
        s = SecretStr("hunter2")
        assert "hunter2" not in f"value={s}"

    def test_get_secret_value_returns_cleartext(self):
        assert SecretStr("hunter2").get_secret_value() == "hunter2"

    def test_eq_with_secret(self):
        assert SecretStr("hunter2") == SecretStr("hunter2")
        assert SecretStr("hunter2") != SecretStr("other")

    def test_eq_with_str(self):
        assert SecretStr("hunter2") == "hunter2"
        assert SecretStr("hunter2") != "other"

    def test_constant_time_eq_handles_length_mismatch(self):
        # Just verify behaviour, not actual timing.
        assert SecretStr("a") != SecretStr("aa")

    def test_rejects_non_str(self):
        with pytest.raises(TypeError):
            SecretStr(123)  # type: ignore[arg-type]

    def test_bool(self):
        assert bool(SecretStr("x"))
        assert not bool(SecretStr(""))

    def test_len(self):
        assert len(SecretStr("hello")) == 5


class TestSessionVault:
    def test_put_and_get(self):
        v = SessionVault()
        v.put("token", "abc123")
        assert v.get("token").get_secret_value() == "abc123"

    def test_put_secretstr(self):
        v = SessionVault()
        v.put("token", SecretStr("abc123"))
        assert v.get("token").get_secret_value() == "abc123"

    def test_get_missing(self):
        v = SessionVault()
        assert v.get("nope") is None

    def test_has(self):
        v = SessionVault()
        assert not v.has("k")
        v.put("k", "v")
        assert v.has("k")

    def test_keys_snapshot_does_not_leak_values(self):
        v = SessionVault()
        v.put("a", "secret_a")
        v.put("b", "secret_b")
        assert set(v.keys()) == {"a", "b"}

    def test_clear(self):
        v = SessionVault()
        v.put("k", "v")
        v.clear()
        assert v.get("k") is None
        assert v.keys() == []

    def test_overwrite(self):
        v = SessionVault()
        v.put("k", "v1")
        v.put("k", "v2")
        assert v.get("k").get_secret_value() == "v2"

    def test_empty_key_rejected(self):
        v = SessionVault()
        with pytest.raises(ValueError, match="non-empty"):
            v.put("", "x")

    def test_render_unwraps_secretstr(self):
        v = SessionVault()
        out = v.render({"token": SecretStr("abc"), "user": "alice"})
        assert out == {"token": "abc", "user": "alice"}

    def test_render_nested(self):
        v = SessionVault()
        out = v.render([1, {"k": SecretStr("secret")}, (SecretStr("a"), "b")])
        assert out == [1, {"k": "secret"}, ("a", "b")]

    def test_render_passthrough_for_plain_values(self):
        v = SessionVault()
        assert v.render(42) == 42
        assert v.render("plain") == "plain"

    def test_repr_does_not_leak_values(self):
        v = SessionVault()
        v.put("token", "hunter2")
        assert "hunter2" not in repr(v)


class TestKeyringBridge:
    def test_keyring_disabled_by_default(self):
        v = SessionVault()
        with patch.dict("sys.modules", {}, clear=False):
            assert v.get("any") is None

    def test_keyring_lookup_when_enabled(self):
        v = SessionVault(allow_keyring=True)
        with patch("keyring.get_password", return_value="from-keychain"):
            result = v.get("k")
        assert result is not None
        assert result.get_secret_value() == "from-keychain"

    def test_keyring_caches_after_lookup(self):
        v = SessionVault(allow_keyring=True)
        with patch("keyring.get_password", return_value="from-keychain") as mock_kr:
            v.get("k")
            v.get("k")
        # Second call should hit cache, not keyring.
        assert mock_kr.call_count == 1

    def test_keyring_failure_returns_none(self):
        v = SessionVault(allow_keyring=True)
        with patch("keyring.get_password", side_effect=RuntimeError("locked")):
            assert v.get("k") is None

    def test_keyring_missing_key_returns_none(self):
        v = SessionVault(allow_keyring=True)
        with patch("keyring.get_password", return_value=None):
            assert v.get("k") is None


class TestDefaultVault:
    def test_singleton(self):
        reset_default_vault()
        a = get_default_vault()
        b = get_default_vault()
        assert a is b

    def test_reset_drops_values(self):
        reset_default_vault()
        v = get_default_vault()
        v.put("k", "v")
        reset_default_vault()
        assert get_default_vault().get("k") is None
