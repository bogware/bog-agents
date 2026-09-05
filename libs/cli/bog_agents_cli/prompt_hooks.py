"""`prompt` hooks: a small model judges an event against a rule, fail-closed (ROADMAP #64).

A command hook is a script; a prompt hook is a sentence. `hooks.json` entries
with `"type": "prompt"` carry the rule (`"prompt": "Deny any shell command
that touches the production database"`) and the events / matcher they apply
to; `evaluate_prompt_hooks` renders the event as JSON, asks the injected model
for `{"decision": "allow" | "deny", "reason": "..."}` and — unlike command
hooks, which are fail-open — denies when the model cannot be reached or does
not answer in that shape. That is the Expert-Mode posture: a policy you wrote
in prose must not silently evaporate because the judge timed out.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from bog_agents_cli.hook_decisions import HookDecision, _matcher_matches

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

PROMPT_HOOK_SYSTEM = (
    "You are a policy check for an autonomous coding agent. You will get a RULE the "
    "operator wrote and an EVENT (JSON) the agent is about to perform. Decide whether "
    "the event violates the rule. Answer with JSON only: "
    '{"decision": "allow" | "deny", "reason": "<one sentence>"}.'
)
_PAYLOAD_CHARS = 6_000


def is_prompt_hook(hook: dict[str, Any]) -> bool:
    """Whether a hook dict is a prompt hook (`type: prompt` with a non-empty `prompt`)."""
    return str(hook.get("type", "")).lower() == "prompt" and bool(
        str(hook.get("prompt", "")).strip()
    )


def _matches(hook: dict[str, Any], event: str, tool_name: str) -> bool:
    events = hook.get("events")
    if events and event not in events:
        return False
    return _matcher_matches(str(hook.get("matcher") or ""), tool_name)


def _parse_verdict(reply: str) -> HookDecision | None:
    """`allow` / `deny` from the judge's reply, or `None` when it did not answer in shape."""
    from bog_agents_cli.feature_helpers import extract_json_object

    data = extract_json_object(reply or "")
    if not isinstance(data, dict):
        return None
    decision = str(data.get("decision", "")).strip().lower()
    reason = str(data.get("reason", "")).strip()
    if decision == "deny":
        return HookDecision(
            action="deny", reason=reason or "A prompt hook denied this action."
        )
    if decision in ("allow", "approve"):
        return HookDecision(action="allow", reason=reason)
    return None


def evaluate_prompt_hooks(
    event: str,
    payload: dict[str, Any],
    hooks: list[dict[str, Any]],
    *,
    invoke: Callable[[str, str], str] | None,
    tool_name: str = "",
) -> HookDecision:
    """Run the matching prompt hooks through `invoke`; the first deny wins; failures deny.

    Args:
        event: The lifecycle event (e.g. `PreToolUse`).
        payload: JSON-serialisable event payload the judge sees.
        hooks: Hook dicts; non-prompt entries are ignored.
        invoke: `(system_prompt, user_prompt) -> str` — the judge. `None` means no
            judge is available, which denies every matching prompt hook.
        tool_name: The tool being called (for matcher filtering).

    Returns:
        The first denying `HookDecision`, else an allow.
    """
    matching = [h for h in hooks if is_prompt_hook(h) and _matches(h, event, tool_name)]
    if not matching:
        return HookDecision()
    rendered = json.dumps({"event": event, **payload}, default=str)[:_PAYLOAD_CHARS]
    for hook in matching:
        rule = str(hook.get("prompt", "")).strip()
        if invoke is None:
            return HookDecision(
                action="deny",
                reason=f"prompt hook could not be evaluated (no judge model): {rule[:80]}",
            )
        user = f"RULE:\n{rule}\n\nEVENT:\n{rendered}"
        try:
            reply = invoke(PROMPT_HOOK_SYSTEM, user)
        except Exception as exc:
            logger.warning("prompt hook judge failed (fail-closed): %s", exc)
            return HookDecision(
                action="deny",
                reason=f"prompt hook could not be evaluated ({exc.__class__.__name__}): {rule[:80]}",
            )
        verdict = _parse_verdict(reply)
        if verdict is None:
            return HookDecision(
                action="deny",
                reason=f"prompt hook judge answered out of shape: {rule[:80]}",
            )
        if verdict.blocks:
            logger.info("Prompt hook %s deny: %s", event, verdict.reason)
            return verdict
    return HookDecision()


def build_prompt_invoke(
    model: BaseChatModel, *, timeout_seconds: float = 30.0
) -> Callable[[str, str], str]:
    """A sync `(system, user) -> str` over `feature_helpers.invoke_model`, safe from sync and async callers.

    The judge call runs in its own event loop on a worker thread, so the tool
    middleware can call it from `wrap_tool_call` (sync) and from
    `awrap_tool_call` (already off the loop via `asyncio.to_thread`).
    """
    import asyncio
    import concurrent.futures

    from bog_agents_cli.feature_helpers import invoke_model

    def _invoke(system: str, user: str) -> str:
        def _run() -> str:
            return asyncio.run(
                invoke_model(model, system, user, timeout_seconds=timeout_seconds)
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_run).result(timeout=timeout_seconds + 5)

    return _invoke


__all__ = [
    "PROMPT_HOOK_SYSTEM",
    "build_prompt_invoke",
    "evaluate_prompt_hooks",
    "is_prompt_hook",
]
