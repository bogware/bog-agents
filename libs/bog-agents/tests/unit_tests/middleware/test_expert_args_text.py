"""Regression test for REVIEW.md v2 P1-20 — secret-in-args detection.

The shipped `redact_token_args` starter rule matched the bare `args` field
(a dict) with a string regex, so it never fired — and its `modify` action
didn't redact anything anyway. The fix: the tool_call fact now exposes an
`args_text` string view of all args, and the rule blocks (deny) on a match.
"""

from __future__ import annotations

import json

from bog_agents.middleware.expert_engine.engine import ExpertEngine
from bog_agents.middleware.expert_engine.loader import load_rules_from_string
from bog_agents.middleware.expert_engine.working_memory import Fact

_RULE = """
- name: block_secret_in_tool_args
  salience: 50
  when:
    - tool_call:
        args_text:
          matches: 'sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16}'
  then:
    - deny: "secret-shaped token detected"
"""


def _tool_call_fact(args: dict) -> Fact:
    # Mirror the shape ExpertRulesMiddleware._run_for_request builds.
    return Fact(
        fact_type="tool_call",
        data={
            "name": "shell_execute",
            "args": dict(args),
            "id": "1",
            "command": args.get("command", ""),
            "args_text": json.dumps(args, default=str, ensure_ascii=False),
        },
    )


def test_secret_in_args_text_is_denied() -> None:
    rules = load_rules_from_string(_RULE)
    engine = ExpertEngine(rules=rules)
    engine.assert_fact(_tool_call_fact({"command": "deploy", "token": "sk-ABCDEFGHIJKLMNOPQRST012345"}))
    result = engine.run()
    assert result.denied is True


def test_secret_outside_command_field_still_caught() -> None:
    # The whole point of args_text: a secret in a NON-command arg is caught.
    rules = load_rules_from_string(_RULE)
    engine = ExpertEngine(rules=rules)
    engine.assert_fact(_tool_call_fact({"path": "x", "header": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"}))
    result = engine.run()
    assert result.denied is True


def test_clean_args_not_denied() -> None:
    rules = load_rules_from_string(_RULE)
    engine = ExpertEngine(rules=rules)
    engine.assert_fact(_tool_call_fact({"command": "ls -la", "path": "/tmp"}))
    result = engine.run()
    assert result.denied is False
