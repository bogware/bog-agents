"""Unit tests for bog_agents_cli.vars."""

from __future__ import annotations

import pytest

from bog_agents_cli.vars import (
    VarBundle,
    VarError,
    VarSpec,
    auto_variabilize,
)
from bog_agents_cli.vault import SecretStr, SessionVault


class TestVarSpec:
    def test_default_string(self):
        s = VarSpec(name="x")
        assert s.type == "string"
        assert s.required is True

    def test_invalid_name(self):
        with pytest.raises(VarError, match="invalid var name"):
            VarSpec(name="bad-name")

    def test_unknown_type(self):
        with pytest.raises(VarError, match="unknown var type"):
            VarSpec(name="x", type="weird")

    def test_enum_requires_choices(self):
        with pytest.raises(VarError, match="non-empty 'choices'"):
            VarSpec(name="x", type="enum")

    def test_secret_rejects_default(self):
        with pytest.raises(VarError, match="must not declare a default"):
            VarSpec(name="x", type="secret", default="leaked")

    def test_coerce_int(self):
        assert VarSpec(name="x", type="int").coerce("42") == 42

    def test_coerce_int_invalid(self):
        with pytest.raises(VarError, match="integer"):
            VarSpec(name="x", type="int").coerce("not-a-number")

    def test_coerce_bool(self):
        s = VarSpec(name="x", type="bool")
        assert s.coerce("true") is True
        assert s.coerce("FALSE") is False
        assert s.coerce("yes") is True
        assert s.coerce("0") is False

    def test_coerce_bool_invalid(self):
        with pytest.raises(VarError, match="boolean"):
            VarSpec(name="x", type="bool").coerce("maybe")

    def test_coerce_enum(self):
        s = VarSpec(name="env", type="enum", choices=["dev", "prod"])
        assert s.coerce("dev") == "dev"
        with pytest.raises(VarError, match="must be one of"):
            s.coerce("staging")

    def test_to_dict_round_trip(self):
        s = VarSpec(name="x", type="enum", choices=["a", "b"], description="d", default="a", required=False)
        d = s.to_dict()
        s2 = VarSpec.from_dict("x", d)
        assert s2.choices == ["a", "b"]
        assert s2.default == "a"
        assert s2.required is False


class TestVarBundle:
    def _bundle(self, **kwargs) -> VarBundle:
        return VarBundle(vault=SessionVault(), **kwargs)

    def test_from_dict(self):
        b = VarBundle.from_dict(
            {"a": {"type": "string", "default": "x"}, "tok": {"type": "secret"}},
            vault=SessionVault(),
        )
        assert "a" in b.specs
        assert b.specs["tok"].type == "secret"

    def test_from_dict_rejects_non_dict_spec(self):
        with pytest.raises(VarError, match="must be a dict"):
            VarBundle.from_dict({"a": "wrong"}, vault=SessionVault())

    def test_set_and_get_string(self):
        b = self._bundle()
        b.declare(VarSpec(name="x", default="y"))
        b.set("x", "hello")
        assert b.get("x") == "hello"

    def test_set_unknown_raises(self):
        b = self._bundle()
        with pytest.raises(VarError, match="unknown var"):
            b.set("nope", "v")

    def test_secret_routed_to_vault(self):
        v = SessionVault()
        b = VarBundle(vault=v)
        b.declare(VarSpec(name="tok", type="secret"))
        b.set("tok", "shhh")
        # Plain values dict should be empty.
        assert b.values == {}
        # Vault has it.
        assert v.get("tok").get_secret_value() == "shhh"

    def test_secret_get_returns_secretstr(self):
        b = self._bundle()
        b.declare(VarSpec(name="tok", type="secret"))
        b.set("tok", "shhh")
        result = b.get("tok")
        assert isinstance(result, SecretStr)

    def test_default_returned_when_unset(self):
        b = self._bundle()
        b.declare(VarSpec(name="x", default="fallback"))
        assert b.get("x") == "fallback"

    def test_is_resolved(self):
        b = self._bundle()
        b.declare(VarSpec(name="a"))
        b.declare(VarSpec(name="b", default="ok"))
        assert not b.is_resolved("a")
        assert b.is_resolved("b")  # has default

    def test_missing_only_returns_required(self):
        b = self._bundle()
        b.declare(VarSpec(name="a"))
        b.declare(VarSpec(name="b", required=False))
        b.declare(VarSpec(name="c", default="x"))
        names = [s.name for s in b.missing()]
        assert names == ["a"]

    async def test_resolve_uses_cli_overrides(self):
        b = self._bundle()
        b.declare(VarSpec(name="x"))

        async def fake_prompt(spec):
            pytest.fail("should not prompt — override provided")

        await b.resolve(prompt=fake_prompt, cli_overrides={"x": "from-cli"})
        assert b.get("x") == "from-cli"

    async def test_resolve_prompts_for_missing(self):
        b = self._bundle()
        b.declare(VarSpec(name="x"))

        async def fake_prompt(spec):
            return "from-prompt"

        await b.resolve(prompt=fake_prompt)
        assert b.get("x") == "from-prompt"

    async def test_resolve_secret_uses_secret_callback(self):
        v = SessionVault()
        b = VarBundle(vault=v)
        b.declare(VarSpec(name="tok", type="secret"))

        async def normal_prompt(spec):
            pytest.fail("non-secret prompt should not be called for secret")

        async def secret_prompt(spec):
            assert spec.name == "tok"
            return "hunter2"

        await b.resolve(prompt=normal_prompt, prompt_secret=secret_prompt)
        assert v.get("tok").get_secret_value() == "hunter2"

    async def test_resolve_accepts_default_on_empty_answer(self):
        b = self._bundle()
        b.declare(VarSpec(name="x", default="d"))

        async def fake_prompt(spec):
            return ""

        # default of "d" means is_resolved=True; prompt is never called.
        await b.resolve(prompt=fake_prompt)
        assert b.get("x") == "d"

    async def test_resolve_prompts_when_no_default_and_required(self):
        b = self._bundle()
        b.declare(VarSpec(name="x"))
        called = []

        async def fake_prompt(spec):
            called.append(spec.name)
            return "answered"

        await b.resolve(prompt=fake_prompt)
        assert called == ["x"]

    def test_substitute_simple(self):
        b = self._bundle()
        b.declare(VarSpec(name="ticket"))
        b.set("ticket", "JIRA-200")
        assert b.substitute("Open ${ticket}") == "Open JIRA-200"

    def test_substitute_missing_var(self):
        b = self._bundle()
        b.declare(VarSpec(name="x"))
        with pytest.raises(VarError, match="unresolved"):
            b.substitute("Hello ${x}")

    def test_substitute_undeclared_var(self):
        b = self._bundle()
        with pytest.raises(VarError, match="undeclared var"):
            b.substitute("Hello ${unknown}")

    def test_substitute_nested(self):
        b = self._bundle()
        b.declare(VarSpec(name="x"))
        b.set("x", "world")
        out = b.substitute({"greet": "hello ${x}", "list": ["${x}", 42]})
        assert out == {"greet": "hello world", "list": ["world", 42]}

    def test_substitute_inserts_secret_cleartext(self):
        v = SessionVault()
        b = VarBundle(vault=v)
        b.declare(VarSpec(name="tok", type="secret"))
        b.set("tok", "secret-token")
        assert b.substitute("Bearer ${tok}") == "Bearer secret-token"

    def test_to_dict_does_not_include_values(self):
        b = self._bundle()
        b.declare(VarSpec(name="x", default="d"))
        b.set("x", "actual_value_runtime")
        d = b.to_dict()
        # Spec defaults are kept; resolved runtime values are NOT.
        assert d == {"x": {"type": "string", "default": "d"}}


class TestAutoVariabilize:
    def test_extracts_jira_ticket(self):
        text, vars_map = auto_variabilize("Get details for JIRA-134 please")
        assert "${jira_ticket}" in text
        assert vars_map["jira_ticket"] == "JIRA-134"

    def test_extracts_url(self):
        text, vars_map = auto_variabilize("Fetch from https://api.example.com/v1/x")
        assert "${url}" in text
        assert vars_map["url"].startswith("https://")

    def test_extracts_github_repo_from_url(self):
        text, vars_map = auto_variabilize("clone github.com/myorg/myrepo please")
        assert "${github_repo}" in text
        assert vars_map["github_repo"] == "myorg/myrepo"

    def test_does_not_match_plain_path(self):
        # File-style path "a/b" should not be misread as a github repo —
        # too many file paths look like that.
        text, _ = auto_variabilize("read /a/b/c.txt")
        assert "${github_repo}" not in text

    def test_same_literal_reuses_name(self):
        text, vars_map = auto_variabilize("JIRA-134 then JIRA-134")
        assert text.count("${jira_ticket}") == 2
        assert vars_map == {"jira_ticket": "JIRA-134"}

    def test_distinct_literals_get_distinct_names(self):
        text, vars_map = auto_variabilize("JIRA-134 plus JIRA-200")
        assert "${jira_ticket}" in text
        # Second match should get a suffixed name.
        assert any(k.startswith("jira_ticket") and k != "jira_ticket" for k in vars_map)
        assert "JIRA-200" in vars_map.values()

    def test_no_matches_returns_empty(self):
        text, vars_map = auto_variabilize("just plain text")
        assert text == "just plain text"
        assert vars_map == {}

    def test_extracted_vars_round_trip_through_bundle(self):
        text, vars_map = auto_variabilize("Get details for JIRA-134 please")
        b = VarBundle.from_dict(
            {name: {"type": "string", "default": val} for name, val in vars_map.items()},
            vault=SessionVault(),
        )
        assert b.substitute(text) == "Get details for JIRA-134 please"
