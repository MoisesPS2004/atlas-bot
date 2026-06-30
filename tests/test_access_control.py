"""
Vertical Slice: trust boundary enforcement in _call_grok.

RED tests — these document the MISSING behavior. They will fail until
_call_grok intercepts tool args and pins volunteer_id to the authenticated
caller's internal_id before dispatching to _run_tool.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, AsyncMock

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


# ─── Test 1: volunteer cannot set preferences for a different volunteer ────────

@pytest.mark.asyncio
async def test_volunteer_cannot_spoof_volunteer_id_in_save_preferences():
    """
    RED — _call_grok currently passes the LLM-generated volunteer_id straight
    to _run_tool without verifying it matches the authenticated volunteer.

    A malicious user (internal_id=42) sends a message that tricks the LLM into
    calling save_preferences(volunteer_id=999, ...). The tool MUST be dispatched
    with volunteer_id=42, not 999.
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

    fake_tool_result = json.dumps({"ok": True})
    fake_context     = MagicMock()  # Telegram context — not used in this path

    # Pre-seed history so _call_grok doesn't blow up on missing key
    bot._history[11111] = [{"role": "user", "content": "Set my preferences to avoid day reception"}]

    with patch.object(bot._client.chat.completions, "create",
                      side_effect=[first_response, second_response]) as mock_llm, \
         patch("bot._run_tool", return_value=fake_tool_result) as mock_run_tool:

        await bot._call_grok(
            user_id=11111,
            user_type="volunteer",
            internal_id=ATTACKER_INTERNAL_ID,
            context=fake_context,
        )

    # The tool must have been called exactly once with the REAL volunteer id.
    mock_run_tool.assert_called_once()
    actual_name, actual_args = mock_run_tool.call_args[0]

    assert actual_name == "save_preferences"
    assert actual_args["volunteer_id"] == ATTACKER_INTERNAL_ID, (
        f"SECURITY: _run_tool received volunteer_id={actual_args['volunteer_id']} "
        f"but the authenticated volunteer is {ATTACKER_INTERNAL_ID}. "
        f"The LLM-supplied ID ({VICTIM_INTERNAL_ID}) was not sanitized."
    )


# ─── Test 2: admin-only tools remain blocked for volunteers ───────────────────

@pytest.mark.asyncio
async def test_volunteer_cannot_call_admin_only_tools():
    """
    Regression guard — the existing _ADMIN_ONLY_TOOLS block must still work
    after we add the new volunteer_id enforcement. This test is GREEN baseline
    documentation; it must stay GREEN after the fix.
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
    bot._history[22222] = [{"role": "user", "content": "Approve the schedule"}]

    with patch.object(bot._client.chat.completions, "create",
                      side_effect=[first_response, second_response]), \
         patch("bot._run_tool", return_value='{"ok": true}') as mock_run_tool:

        await bot._call_grok(
            user_id=22222,
            user_type="volunteer",
            internal_id=55,
            context=fake_context,
        )

    # _run_tool must NEVER be called when the tool is admin-only.
    mock_run_tool.assert_not_called()


# ─── Test 3: legitimate volunteer preference save passes through unchanged ─────

@pytest.mark.asyncio
async def test_volunteer_save_preferences_with_correct_id_passes():
    """
    RED — documents the expected GREEN behavior after the fix.
    When the LLM correctly uses the authenticated volunteer_id, the call must
    reach _run_tool unmodified.
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
    bot._history[33333] = [{"role": "user", "content": "I like day reception"}]

    with patch.object(bot._client.chat.completions, "create",
                      side_effect=[first_response, second_response]), \
         patch("bot._run_tool", return_value=json.dumps({"ok": True})) as mock_run_tool:

        await bot._call_grok(
            user_id=33333,
            user_type="volunteer",
            internal_id=VOLUNTEER_ID,
            context=fake_context,
        )

    mock_run_tool.assert_called_once()
    _, actual_args = mock_run_tool.call_args[0]
    assert actual_args["volunteer_id"] == VOLUNTEER_ID
