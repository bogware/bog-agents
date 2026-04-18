"""Unit tests for bog_agents_cli.cmd_build."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bog_agents_cli.cmd_build import (
    build_pipeline_yaml,
    build_prompt_entry,
    build_skill_template,
    extract_variables,
    format_build_help,
    preview_skill,
    save_pipeline,
    save_skill,
)


class TestExtractVariables:
    def test_finds_single_var(self):
        assert extract_variables("Hello {{MY_VAR}}") == ["MY_VAR"]

    def test_finds_multiple_vars(self):
        result = extract_variables("{{A}} and {{B}}")
        assert result == ["A", "B"]

    def test_deduplicates(self):
        result = extract_variables("{{A}} {{A}} {{B}}")
        assert result == ["A", "B"]

    def test_empty_string(self):
        assert extract_variables("") == []

    def test_no_vars(self):
        assert extract_variables("plain text no vars") == []

    def test_returns_sorted(self):
        result = extract_variables("{{ZEBRA}} {{ALPHA}}")
        assert result == ["ALPHA", "ZEBRA"]

    def test_lowercase_not_matched(self):
        # Only uppercase names are matched
        result = extract_variables("{{lower_case}}")
        assert result == []

    def test_mixed_case_not_matched(self):
        result = extract_variables("{{MyVar}}")
        assert result == []

    def test_underscore_in_name(self):
        result = extract_variables("{{MY_LONG_VAR}}")
        assert result == ["MY_LONG_VAR"]

    def test_digits_in_name(self):
        result = extract_variables("{{VAR1}} {{VAR2}}")
        assert result == ["VAR1", "VAR2"]


class TestBuildSkillTemplate:
    def test_basic_template(self):
        result = build_skill_template("my-skill", "Does something", "body text", variables=[])
        assert "# my-skill" in result
        assert "Does something" in result
        assert "body text" in result

    def test_includes_variables_section(self):
        result = build_skill_template("my-skill", "desc", "body", variables=["FOO", "BAR"])
        assert "## Variables" in result
        assert "{{FOO}}" in result
        assert "{{BAR}}" in result

    def test_no_variables_section_when_empty(self):
        result = build_skill_template("my-skill", "desc", "body", variables=[])
        assert "## Variables" not in result

    def test_invalid_name_raises(self):
        with pytest.raises(ValueError, match="Invalid skill name"):
            build_skill_template("InvalidName", "desc", "body", variables=[])

    def test_name_with_uppercase_raises(self):
        with pytest.raises(ValueError):
            build_skill_template("My-Skill", "desc", "body", variables=[])

    def test_name_with_spaces_raises(self):
        with pytest.raises(ValueError):
            build_skill_template("my skill", "desc", "body", variables=[])

    def test_result_ends_with_newline(self):
        result = build_skill_template("valid-name", "desc", "body", variables=[])
        assert result.endswith("\n")

    def test_includes_usage_section(self):
        result = build_skill_template("my-skill", "desc", "body text", variables=[])
        assert "## Usage" in result


class TestBuildPromptEntry:
    def test_basic_entry(self):
        entry = build_prompt_entry("my-prompt", "Does something", "Hello {{NAME}}")
        assert entry["name"] == "my-prompt"
        assert entry["description"] == "Does something"
        assert entry["body"] == "Hello {{NAME}}"
        assert entry["variables"] == ["NAME"]

    def test_empty_variables_when_no_placeholders(self):
        entry = build_prompt_entry("test", "desc", "no vars here")
        assert entry["variables"] == []

    def test_invalid_name_empty_raises(self):
        with pytest.raises(ValueError, match="Invalid prompt name"):
            build_prompt_entry("", "desc", "body")

    def test_invalid_name_with_spaces_raises(self):
        with pytest.raises(ValueError):
            build_prompt_entry("my prompt", "desc", "body")

    def test_valid_name_with_underscores(self):
        entry = build_prompt_entry("my_prompt", "desc", "body")
        assert entry["name"] == "my_prompt"

    def test_valid_name_with_hyphens(self):
        entry = build_prompt_entry("my-prompt", "desc", "body")
        assert entry["name"] == "my-prompt"

    def test_variables_extracted_from_body(self):
        entry = build_prompt_entry("p", "d", "{{A}} and {{B}}")
        assert entry["variables"] == ["A", "B"]


class TestBuildPipelineYaml:
    def test_basic_pipeline(self):
        steps = [{"label": "Step 1", "type": "prompt", "content": "do something"}]
        result = build_pipeline_yaml("my-pipeline", "A pipeline", steps)
        assert "name: my-pipeline" in result
        assert "description: A pipeline" in result
        assert "label: Step 1" in result
        assert "type: prompt" in result

    def test_includes_schedule(self):
        result = build_pipeline_yaml("p", "d", [], schedule="0 9 * * *")
        assert "schedule: 0 9 * * *" in result

    def test_no_schedule_omitted(self):
        result = build_pipeline_yaml("p", "d", [])
        assert "schedule:" not in result

    def test_multiple_steps(self):
        steps = [
            {"label": "A", "type": "prompt", "content": "first"},
            {"label": "B", "type": "skill", "content": "second"},
        ]
        result = build_pipeline_yaml("p", "d", steps)
        assert "label: A" in result
        assert "label: B" in result
        assert "type: skill" in result

    def test_result_ends_with_newline(self):
        result = build_pipeline_yaml("p", "d", [])
        assert result.endswith("\n")

    def test_content_indented_for_block_scalar(self):
        steps = [{"label": "S", "type": "prompt", "content": "line1\nline2"}]
        result = build_pipeline_yaml("p", "d", steps)
        # content uses block scalar (|)
        assert "content: |" in result


class TestSaveSkill:
    def test_creates_skill_file(self, tmp_path):
        result = save_skill("my-skill", "content here", user_skills_dir=tmp_path)
        assert result.exists()
        assert result.read_text() == "content here"

    def test_returns_skill_md_path(self, tmp_path):
        result = save_skill("my-skill", "content", user_skills_dir=tmp_path)
        assert result.name == "SKILL.md"
        assert result.parent.name == "my-skill"

    def test_raises_if_already_exists(self, tmp_path):
        save_skill("my-skill", "content", user_skills_dir=tmp_path)
        with pytest.raises(FileExistsError):
            save_skill("my-skill", "other content", user_skills_dir=tmp_path)

    def test_creates_parent_dirs(self, tmp_path):
        nested = tmp_path / "nested" / "skills"
        result = save_skill("my-skill", "body", user_skills_dir=nested)
        assert result.exists()

    def test_different_names_dont_conflict(self, tmp_path):
        r1 = save_skill("skill-a", "a", user_skills_dir=tmp_path)
        r2 = save_skill("skill-b", "b", user_skills_dir=tmp_path)
        assert r1.exists()
        assert r2.exists()
        assert r1 != r2

    def test_empty_existing_does_not_raise(self, tmp_path):
        # If SKILL.md exists but is empty, allow overwrite
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("")
        # Should not raise because size is 0
        result = save_skill("my-skill", "new content", user_skills_dir=tmp_path)
        assert result.read_text() == "new content"


class TestSavePipeline:
    def test_creates_pipeline_file(self, tmp_path):
        result = save_pipeline("my-pipeline", "yaml: content", pipelines_dir=tmp_path)
        assert result.exists()
        assert result.read_text() == "yaml: content"

    def test_returns_yaml_path(self, tmp_path):
        result = save_pipeline("my-pipeline", "yaml", pipelines_dir=tmp_path)
        assert result.name == "my-pipeline.yaml"

    def test_raises_if_already_exists(self, tmp_path):
        save_pipeline("my-pipeline", "yaml", pipelines_dir=tmp_path)
        with pytest.raises(FileExistsError):
            save_pipeline("my-pipeline", "other", pipelines_dir=tmp_path)

    def test_creates_pipelines_dir(self, tmp_path):
        new_dir = tmp_path / "pipelines"
        result = save_pipeline("p", "yaml", pipelines_dir=new_dir)
        assert result.exists()


class TestFormatBuildHelp:
    def test_returns_string(self):
        result = format_build_help()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_mentions_build_command(self):
        result = format_build_help()
        assert "/build" in result

    def test_mentions_skill_and_prompt(self):
        result = format_build_help()
        assert "skill" in result
        assert "prompt" in result


class TestPreviewSkill:
    def test_includes_name(self):
        result = preview_skill("my-skill", "desc", "body", [])
        assert "my-skill" in result

    def test_includes_description(self):
        result = preview_skill("my-skill", "My description", "body", [])
        assert "My description" in result

    def test_includes_variables(self):
        result = preview_skill("my-skill", "desc", "body", ["VAR_A", "VAR_B"])
        assert "VAR_A" in result
        assert "VAR_B" in result

    def test_no_variables_section_when_empty(self):
        result = preview_skill("my-skill", "desc", "body", [])
        assert "Variables:" not in result

    def test_has_separator_lines(self):
        result = preview_skill("my-skill", "desc", "body", [])
        assert "─" in result
