"""
Vertical Slice: trust boundary enforcement in _call_grok.

These tests lock the pin_identity/authorize contract already established
in Hueco B: a volunteer can never spoof another volunteer's ID for a
self-service call, and admin-only tools stay blocked for volunteers.

Since Hueco B / B.4, _call_grok no longer reaches for module globals
(_client, _run_tool, ...) — it depends only on an injected
call_grok_core.Deps(call_model, perform). These tests construct fake deps
directly instead of patching bot internals, exercising the real effects
boundary rather than monkeypatching around it.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, AsyncMock

import pytest

# conftest.py already stubs out .env / openai / telegram before this import.
import bot


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_tool_call(tc_id: str, name: str, args: dict):
    """Build a fake ToolCall object matching the OpenAI SDK shape."""
    fc = SimpleNamespace(name=name, arguments=json.dumps(args))
    return SimpleNamespace(id=tc_id, function=fc)


def _llm_response(tool_calls=None, content=None):
    """Return a fake chat completion response."""
    msg = SimpleNamespace(tool_calls=tool_calls, content=content)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


def _make_deps(*responses, perform_result='{"ok": true}'):
    """
    Build a fake call_grok_core.Deps for a test: call_model returns each
    fake LLM response in order (one per agentic-loop turn), perform is a
    single AsyncMock returning perform_result for every tool call. No
    network, no bot.* patching.
    """
    return bot.Deps(
        call_model=Mock(side_effect=list(responses)),
        perform=AsyncMock(return_value=perform_result),
    )


# ─── Test 1: volunteer cannot set preferences for a different volunteer ────────

@pytest.mark.asyncio
async def test_volunteer_cannot_spoof_volunteer_id_in_save_preferences():
    """
    A malicious user (internal_id=42) sends a message that tricks the LLM into
    calling save_preferences(volunteer_id=999, ...). pin_identity() must force
    the call to dispatch with volunteer_id=42, not 999.
    """
    ATTACKER_INTERNAL_ID = 42
    VICTIM_INTERNAL_ID   = 999

    # LLM first returns a tool call with the spoofed volunteer_id.
    # Second call returns plain text (conversation ends).
    malicious_tool_call = _make_tool_call(
        tc_id="call_abc",
        name="save_preferences",
        args={"volunteer_id": VICTIM_INTERNAL_ID, "data": '{"day_rec": "avoid"}'},
    )
    first_response  = _llm_response(tool_calls=[malicious_tool_call])
    second_response = _llm_response(tool_calls=None, content="Preferences saved!")

    fake_context = MagicMock()  # Telegram context — not used in this path
    deps = _make_deps(first_response, second_response, perform_result=json.dumps({"ok": True}))

    # Pre-seed history so _call_grok doesn't blow up on missing key
    bot._store.append_history(11111, "user", "Set my preferences to avoid day reception")

    await bot._call_grok(
        user_id=11111,
        user_type="volunteer",
        internal_id=ATTACKER_INTERNAL_ID,
        context=fake_context,
        deps=deps,
    )

    # deps.perform must have been called exactly once, with the REAL volunteer id.
    deps.perform.assert_called_once()
    actual_name, actual_args, actual_ctx, actual_acting_user = deps.perform.call_args.args

    assert actual_name == "save_preferences"
    assert actual_args["volunteer_id"] == ATTACKER_INTERNAL_ID, (
        f"SECURITY: perform() received volunteer_id={actual_args['volunteer_id']} "
        f"but the authenticated volunteer is {ATTACKER_INTERNAL_ID}. "
        f"The LLM-supplied ID ({VICTIM_INTERNAL_ID}) was not sanitized."
    )


# ─── Test 2: admin-only tools remain blocked for volunteers ───────────────────

@pytest.mark.asyncio
async def test_volunteer_cannot_call_admin_only_tools():
    """
    Regression guard — authorize() must keep admin-only tools blocked for
    volunteers. This test is GREEN baseline documentation; it must stay
    GREEN after any further refactor.
    """
    admin_tool_call = _make_tool_call(
        tc_id="call_xyz",
        name="approve_draft",
        args={"week": "2026-07-07", "telegram_id": "8632082731"},
    )
    # LLM tries to use approve_draft, then gives up and replies in text.
    first_response  = _llm_response(tool_calls=[admin_tool_call])
    second_response = _llm_response(tool_calls=None, content="Sorry, you can't do that.")

    fake_context = MagicMock()
    deps = _make_deps(first_response, second_response)
    bot._store.append_history(22222, "user", "Approve the schedule")

    await bot._call_grok(
        user_id=22222,
        user_type="volunteer",
        internal_id=55,
        context=fake_context,
        deps=deps,
    )

    # deps.perform must NEVER be called when the tool is admin-only.
    deps.perform.assert_not_called()


# ─── Test 3: legitimate volunteer preference save passes through unchanged ─────

@pytest.mark.asyncio
async def test_volunteer_save_preferences_with_correct_id_passes():
    """
    When the LLM correctly uses the authenticated volunteer_id, the call must
    reach deps.perform unmodified.
    """
    VOLUNTEER_ID = 42

    correct_tool_call = _make_tool_call(
        tc_id="call_def",
        name="save_preferences",
        args={"volunteer_id": VOLUNTEER_ID, "data": '{"day_rec": "like"}'},
    )
    first_response  = _llm_response(tool_calls=[correct_tool_call])
    second_response = _llm_response(tool_calls=None, content="Done!")

    fake_context = MagicMock()
    deps = _make_deps(first_response, second_response, perform_result=json.dumps({"ok": True}))
    bot._store.append_history(33333, "user", "I like day reception")

    await bot._call_grok(
        user_id=33333,
        user_type="volunteer",
        internal_id=VOLUNTEER_ID,
        context=fake_context,
        deps=deps,
    )

    deps.perform.assert_called_once()
    _, actual_args, _, _ = deps.perform.call_args.args
    assert actual_args["volunteer_id"] == VOLUNTEER_ID
