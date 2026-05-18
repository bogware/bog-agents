"""Tests for ``bog_agents_cli.api_keys`` — vault key bridge + registry consistency.

Guards P0-G in REVIEW.md: ``WELL_KNOWN_API_KEYS`` and
``model_config.PROVIDER_API_KEY_ENV`` must agree, and the
``PERPLEXITY_API_KEY`` user-facing alias must resolve to the canonical
``PPLX_API_KEY`` that ``langchain-perplexity`` reads.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

# ---------------------------------------------------------------------------
# Registry consistency
# ---------------------------------------------------------------------------


class TestRegistryConsistency:
    def test_every_provider_env_var_is_in_well_known_keys(self) -> None:
        from bog_agents_cli.api_keys import WELL_KNOWN_API_KEYS
        from bog_agents_cli.model_config import PROVIDER_API_KEY_ENV

        missing = sorted(set(PROVIDER_API_KEY_ENV.values()) - set(WELL_KNOWN_API_KEYS))
        assert not missing, (
            f"PROVIDER_API_KEY_ENV references env vars not in WELL_KNOWN_API_KEYS: {missing}. "
            "Add metadata to _PROVIDER_KEY_METADATA in api_keys.py."
        )

    def test_well_known_keys_includes_critical_providers(self) -> None:
        from bog_agents_cli.api_keys import WELL_KNOWN_API_KEYS

        # These were the providers explicitly missing before P0-G.
        for env_var in (
            "PPLX_API_KEY",
            "BASETEN_API_KEY",
            "HUGGINGFACEHUB_API_TOKEN",
            "WATSONX_APIKEY",
            "LITELLM_API_KEY",
            "TOGETHER_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "AZURE_OPENAI_API_KEY",
            "GOOGLE_CLOUD_PROJECT",
        ):
            assert env_var in WELL_KNOWN_API_KEYS, (
                f"{env_var} missing from WELL_KNOWN_API_KEYS — vault auto-inject is broken for it."
            )

    def test_each_entry_has_description_and_url(self) -> None:
        from bog_agents_cli.api_keys import WELL_KNOWN_API_KEYS

        for env_var, meta in WELL_KNOWN_API_KEYS.items():
            assert isinstance(meta, tuple)
            assert len(meta) == 2
            desc, url = meta
            assert desc, f"{env_var}: empty description"
            assert url.startswith("http"), f"{env_var}: docs URL must be http(s)"


# ---------------------------------------------------------------------------
# Vault injection
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear every WELL_KNOWN_API_KEYS env var before the test."""
    from bog_agents_cli.api_keys import WELL_KNOWN_API_KEYS

    for env_var in WELL_KNOWN_API_KEYS:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    return


class TestVaultInjection:
    def test_injects_when_env_missing(
        self, _isolated_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A vault key without a matching env var should be injected."""
        from bog_agents_cli import api_keys

        monkeypatch.setattr(
            "bog_agents_cli.vars_store.get_var",
            lambda name: "from-vault" if name == "OPENAI_API_KEY" else None,
        )
        injected = api_keys.inject_vault_keys_into_env()
        assert "OPENAI_API_KEY" in injected

        import os

        assert os.environ.get("OPENAI_API_KEY") == "from-vault"

    def test_does_not_overwrite_existing_env(
        self, _isolated_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        os.environ["OPENAI_API_KEY"] = "from-shell"
        try:
            from bog_agents_cli import api_keys

            monkeypatch.setattr(
                "bog_agents_cli.vars_store.get_var",
                lambda name: "from-vault" if name == "OPENAI_API_KEY" else None,
            )
            injected = api_keys.inject_vault_keys_into_env()
            assert "OPENAI_API_KEY" not in injected
            assert os.environ["OPENAI_API_KEY"] == "from-shell"
        finally:
            os.environ.pop("OPENAI_API_KEY", None)


# ---------------------------------------------------------------------------
# Perplexity alias
# ---------------------------------------------------------------------------


class TestPerplexityAlias:
    def test_alias_in_vault_resolves_to_canonical(
        self, _isolated_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A user storing PERPLEXITY_API_KEY in vault should land at PPLX_API_KEY."""
        from bog_agents_cli import api_keys

        monkeypatch.setattr(
            "bog_agents_cli.vars_store.get_var",
            lambda name: "alias-key" if name == "PERPLEXITY_API_KEY" else None,
        )
        injected = api_keys.inject_vault_keys_into_env()
        assert "PPLX_API_KEY" in injected

        import os

        assert os.environ.get("PPLX_API_KEY") == "alias-key"

    def test_alias_env_var_resolves_to_canonical(
        self, _isolated_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exported ``PERPLEXITY_API_KEY`` should also resolve to ``PPLX_API_KEY``."""
        import os

        os.environ["PERPLEXITY_API_KEY"] = "from-shell-alias"
        try:
            from bog_agents_cli import api_keys

            monkeypatch.setattr(
                "bog_agents_cli.vars_store.get_var",
                lambda _name: None,
            )
            api_keys.inject_vault_keys_into_env()
            assert os.environ.get("PPLX_API_KEY") == "from-shell-alias"
        finally:
            os.environ.pop("PERPLEXITY_API_KEY", None)
            os.environ.pop("PPLX_API_KEY", None)

    def test_canonical_takes_precedence_over_alias(
        self, _isolated_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If both PPLX_API_KEY and PERPLEXITY_API_KEY are set, the canonical wins."""
        import os

        os.environ["PPLX_API_KEY"] = "canonical"
        os.environ["PERPLEXITY_API_KEY"] = "alias"
        try:
            from bog_agents_cli import api_keys

            monkeypatch.setattr(
                "bog_agents_cli.vars_store.get_var",
                lambda _name: None,
            )
            api_keys.inject_vault_keys_into_env()
            assert os.environ["PPLX_API_KEY"] == "canonical"
        finally:
            os.environ.pop("PPLX_API_KEY", None)
            os.environ.pop("PERPLEXITY_API_KEY", None)
