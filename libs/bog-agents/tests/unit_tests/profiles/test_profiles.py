"""Unit tests for `bog_agents.profiles` (harness + provider registries)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bog_agents.profiles._keys import validate_profile_key
from bog_agents.profiles.harness import harness_profiles as hp
from bog_agents.profiles.harness.harness_profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    HarnessProfileConfig,
    _get_harness_profile,
    _merge_profiles,
    register_harness_profile,
)
from bog_agents.profiles.provider import provider_profiles as pp
from bog_agents.profiles.provider.provider_profiles import (
    ProviderProfile,
    apply_provider_profile,
    get_provider_profile,
    register_provider_profile,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _isolate_registries() -> Iterator[None]:
    """Snapshot and restore both module-global registries around each test."""
    saved_harness = dict(hp._HARNESS_PROFILES)
    saved_provider = dict(pp._PROVIDER_PROFILES)
    hp._HARNESS_PROFILES.clear()
    pp._PROVIDER_PROFILES.clear()
    try:
        yield
    finally:
        hp._HARNESS_PROFILES.clear()
        hp._HARNESS_PROFILES.update(saved_harness)
        pp._PROVIDER_PROFILES.clear()
        pp._PROVIDER_PROFILES.update(saved_provider)


# ---------------------------------------------------------------------------
# validate_profile_key
# ---------------------------------------------------------------------------


def test_validate_key_valid_provider() -> None:
    validate_profile_key("openai")  # no raise


def test_validate_key_valid_provider_model() -> None:
    validate_profile_key("openai:gpt-5.4")  # no raise


def test_validate_key_empty_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        validate_profile_key("")


def test_validate_key_double_colon_raises() -> None:
    with pytest.raises(ValueError, match="more than one"):
        validate_profile_key("a:b:c")


def test_validate_key_empty_halves_raise() -> None:
    with pytest.raises(ValueError, match="empty provider or model"):
        validate_profile_key("openai:")
    with pytest.raises(ValueError, match="empty provider or model"):
        validate_profile_key(":model")


def test_validate_key_leading_trailing_whitespace_raises() -> None:
    with pytest.raises(ValueError, match="leading or trailing whitespace"):
        validate_profile_key(" openai")


def test_validate_key_whitespace_adjacent_colon_raises() -> None:
    with pytest.raises(ValueError, match="whitespace adjacent"):
        validate_profile_key("openai: gpt")


# ---------------------------------------------------------------------------
# HarnessProfile construction / immutability / scaffolding rejection
# ---------------------------------------------------------------------------


def test_harness_profile_tool_overrides_frozen() -> None:
    profile = HarnessProfile(tool_description_overrides={"ls": "list"})
    with pytest.raises(TypeError):
        profile.tool_description_overrides["ls"] = "mutated"  # type: ignore[index]


def test_harness_profile_defensive_copy() -> None:
    """Mutating the source dict after construction does not leak in."""
    src = {"ls": "orig"}
    profile = HarnessProfile(tool_description_overrides=src)
    src["ls"] = "mutated"
    assert profile.tool_description_overrides["ls"] == "orig"


def test_harness_profile_excluded_extra_middleware_tuple_copy() -> None:
    profile = HarnessProfile()
    assert profile.extra_middleware == ()


@pytest.mark.parametrize("name", ["FilesystemMiddleware", "SubAgentMiddleware"])
def test_harness_profile_scaffolding_string_exclusion_raises(name: str) -> None:
    with pytest.raises(ValueError, match="required scaffolding"):
        HarnessProfile(excluded_middleware=frozenset({name}))


def test_harness_profile_config_scaffolding_exclusion_raises() -> None:
    with pytest.raises(ValueError, match="required scaffolding"):
        HarnessProfileConfig(excluded_middleware=frozenset({"FilesystemMiddleware"}))


def test_harness_profile_class_form_scaffolding_exclusion_raises() -> None:
    class FilesystemMiddleware:  # name matches required scaffolding
        pass

    with pytest.raises(ValueError, match="required scaffolding"):
        HarnessProfile(excluded_middleware=frozenset({FilesystemMiddleware}))


# ---------------------------------------------------------------------------
# register_harness_profile + _get_harness_profile resolution
# ---------------------------------------------------------------------------


def test_register_and_get_exact() -> None:
    register_harness_profile("openai:gpt-x", HarnessProfile(system_prompt_suffix="suf"))
    got = _get_harness_profile("openai:gpt-x")
    assert got is not None
    assert got.system_prompt_suffix == "suf"


def test_get_provider_fallback() -> None:
    register_harness_profile("openai", HarnessProfile(system_prompt_suffix="prov"))
    got = _get_harness_profile("openai:unregistered-model")
    assert got is not None
    assert got.system_prompt_suffix == "prov"


def test_get_merges_exact_over_provider() -> None:
    register_harness_profile("openai", HarnessProfile(system_prompt_suffix="prov", excluded_tools=frozenset({"a"})))
    register_harness_profile("openai:gpt-x", HarnessProfile(excluded_tools=frozenset({"b"})))
    got = _get_harness_profile("openai:gpt-x")
    assert got is not None
    # Suffix inherited from provider; exclusions unioned.
    assert got.system_prompt_suffix == "prov"
    assert got.excluded_tools == frozenset({"a", "b"})


def test_get_malformed_spec_returns_none() -> None:
    register_harness_profile("openai", HarnessProfile())
    assert _get_harness_profile("openai:") is None
    assert _get_harness_profile("a:b:c") is None
    assert _get_harness_profile("") is None


# ---------------------------------------------------------------------------
# _merge_profiles field semantics
# ---------------------------------------------------------------------------


def test_merge_suffix_override_fallback() -> None:
    base = HarnessProfile(system_prompt_suffix="base")
    override = HarnessProfile()  # suffix None -> falls back
    merged = _merge_profiles(base, override)
    assert merged.system_prompt_suffix == "base"


def test_merge_suffix_override_wins() -> None:
    base = HarnessProfile(system_prompt_suffix="base")
    override = HarnessProfile(system_prompt_suffix="over")
    assert _merge_profiles(base, override).system_prompt_suffix == "over"


def test_merge_excluded_tools_union() -> None:
    base = HarnessProfile(excluded_tools=frozenset({"x"}))
    override = HarnessProfile(excluded_tools=frozenset({"y"}))
    assert _merge_profiles(base, override).excluded_tools == frozenset({"x", "y"})


def test_merge_tool_description_per_key_override() -> None:
    base = HarnessProfile(tool_description_overrides={"task": "base-task", "ls": "base-ls"})
    override = HarnessProfile(tool_description_overrides={"task": "over-task"})
    merged = _merge_profiles(base, override)
    assert merged.tool_description_overrides["task"] == "over-task"
    assert merged.tool_description_overrides["ls"] == "base-ls"


def test_merge_general_purpose_subagent_field_merge() -> None:
    base = HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False, description="base-desc"))
    override = HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=True))
    merged = _merge_profiles(base, override)
    gp = merged.general_purpose_subagent
    assert gp is not None
    assert gp.enabled is True  # override wins
    assert gp.description == "base-desc"  # inherited from base


# ---------------------------------------------------------------------------
# HarnessProfileConfig.from_dict / to_dict round-trip
# ---------------------------------------------------------------------------


def test_config_round_trip() -> None:
    cfg = HarnessProfileConfig(
        base_system_prompt="base",
        system_prompt_suffix="suf",
        tool_description_overrides={"ls": "list"},
        excluded_tools=frozenset({"grep"}),
        excluded_middleware=frozenset({"SummarizationMiddleware"}),
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    )
    restored = HarnessProfileConfig.from_dict(cfg.to_dict())
    assert restored == cfg


def test_config_from_dict_unknown_key_raises() -> None:
    with pytest.raises(TypeError, match="Unknown keys"):
        HarnessProfileConfig.from_dict({"bogus": 1})


def test_config_to_harness_profile() -> None:
    cfg = HarnessProfileConfig(system_prompt_suffix="suf", excluded_middleware=frozenset({"SummarizationMiddleware"}))
    profile = cfg.to_harness_profile()
    assert isinstance(profile, HarnessProfile)
    assert profile.system_prompt_suffix == "suf"
    assert "SummarizationMiddleware" in profile.excluded_middleware


def test_register_accepts_config() -> None:
    register_harness_profile("openai:cfg", HarnessProfileConfig(system_prompt_suffix="from-cfg"))
    got = _get_harness_profile("openai:cfg")
    assert got is not None
    assert got.system_prompt_suffix == "from-cfg"


# ---------------------------------------------------------------------------
# GeneralPurposeSubagentProfile from_dict / to_dict
# ---------------------------------------------------------------------------


def test_gp_subagent_round_trip() -> None:
    gp = GeneralPurposeSubagentProfile(enabled=True, description="d", system_prompt="p")
    assert GeneralPurposeSubagentProfile.from_dict(gp.to_dict()) == gp


def test_gp_subagent_to_dict_omits_none() -> None:
    assert GeneralPurposeSubagentProfile().to_dict() == {}


def test_gp_subagent_from_dict_unknown_key_raises() -> None:
    with pytest.raises(TypeError, match="Unknown keys"):
        GeneralPurposeSubagentProfile.from_dict({"nope": 1})


def test_gp_subagent_from_dict_wrong_type_raises() -> None:
    with pytest.raises(TypeError, match="must be bool"):
        GeneralPurposeSubagentProfile.from_dict({"enabled": "yes"})


# ---------------------------------------------------------------------------
# ProviderProfile init_kwargs frozen
# ---------------------------------------------------------------------------


def test_provider_profile_init_kwargs_frozen() -> None:
    profile = ProviderProfile(init_kwargs={"temperature": 0})
    with pytest.raises(TypeError):
        profile.init_kwargs["temperature"] = 1  # type: ignore[index]


def test_provider_profile_defensive_copy() -> None:
    src = {"temperature": 0}
    profile = ProviderProfile(init_kwargs=src)
    src["temperature"] = 99
    assert profile.init_kwargs["temperature"] == 0


# ---------------------------------------------------------------------------
# register_provider_profile + get_provider_profile + apply_provider_profile
# ---------------------------------------------------------------------------


def test_register_and_get_provider() -> None:
    register_provider_profile("myprov", ProviderProfile(init_kwargs={"temperature": 0.5}))
    got = get_provider_profile("myprov")
    assert got is not None
    assert got.init_kwargs["temperature"] == 0.5


def test_apply_caller_kwargs_win() -> None:
    register_provider_profile("myprov2", ProviderProfile(init_kwargs={"temperature": 0}))
    merged = apply_provider_profile("myprov2", {"temperature": 0.9})
    assert merged["temperature"] == 0.9


def test_apply_factory_overrides_init_kwargs() -> None:
    register_provider_profile(
        "myprov3",
        ProviderProfile(init_kwargs={"temperature": 0, "timeout": 30}, init_kwargs_factory=lambda: {"temperature": 0.7}),
    )
    merged = apply_provider_profile("myprov3")
    assert merged["temperature"] == 0.7  # factory wins over static
    assert merged["timeout"] == 30


def test_apply_pre_init_fires() -> None:
    calls: list[str] = []
    register_provider_profile("myprov4", ProviderProfile(pre_init=calls.append))
    apply_provider_profile("myprov4")
    assert calls == ["myprov4"]


def test_apply_pre_init_suppressed() -> None:
    calls: list[str] = []
    register_provider_profile("myprov5", ProviderProfile(pre_init=calls.append))
    apply_provider_profile("myprov5", run_pre_init=False)
    assert calls == []


def test_apply_no_profile_returns_kwargs_copy() -> None:
    out = apply_provider_profile("unregistered-prov", {"a": 1})
    assert out == {"a": 1}


def test_provider_merge_chains_pre_init_and_factory() -> None:
    order: list[str] = []
    register_provider_profile(
        "chainprov",
        ProviderProfile(
            init_kwargs={"a": 1},
            pre_init=lambda spec: order.append("base"),
            init_kwargs_factory=lambda: {"shared": "base", "only_base": 1},
        ),
    )
    register_provider_profile(
        "chainprov",
        ProviderProfile(
            init_kwargs={"b": 2},
            pre_init=lambda spec: order.append("over"),
            init_kwargs_factory=lambda: {"shared": "over", "only_over": 2},
        ),
    )
    merged = apply_provider_profile("chainprov")
    assert order == ["base", "over"]  # chained, base first
    assert merged["a"] == 1
    assert merged["b"] == 2
    assert merged["shared"] == "over"  # override factory wins
    assert merged["only_base"] == 1
    assert merged["only_over"] == 2


def test_provider_get_merges_exact_over_provider() -> None:
    register_provider_profile("xprov", ProviderProfile(init_kwargs={"shared": "prov", "p": 1}))
    register_provider_profile("xprov:model", ProviderProfile(init_kwargs={"shared": "model", "m": 2}))
    got = get_provider_profile("xprov:model")
    assert got is not None
    assert got.init_kwargs["shared"] == "model"
    assert got.init_kwargs["p"] == 1
    assert got.init_kwargs["m"] == 2
