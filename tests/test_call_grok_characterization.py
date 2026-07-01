"""
Vertical Slice: characterization suite for _call_grok (bot.py:522-618) — Hueco B / B.0.

These tests pin the behavior of _call_grok EXACTLY AS IT EXISTS TODAY, before any
extraction. They are the strangler-fig safety net: B.1-B.4 must keep this suite
GREEN except where a test is explicitly marked as documenting a gap that B.1 is
going to close on purpose (see inline "⚠️ TODAY'S BEHAVIOR" notes). A change to
one of those marked assertions in B.1 is an intentional behavior change, agreed
in the B.1 Grill Me — not a regression.

This file intentionally does NOT duplicate the contract already locked by
tests/test_access_control.py (pin_identity for save_preferences, admin-only
block for _ADMIN_ONLY_TOOLS members). It covers the paths that file leaves
uncharacterized: the default for tool_names outside any explicit allow/deny
list, the two notify_* tools, the auto-notify-after-approve_draft branch and
its swallowed exceptions, and the max-iteration fallback.

Same harness as test_access_control.py: patch bot._client.chat.completions.create
with a scripted side_effect (one fake response per agentic-loop turn) and patch
whatever bot.* effect function the case cares about. No network, no subprocess.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# conftest.py already stubs out .env / openai / telegram before this import.
import bot


# ─── Helpers (mirrors tests/test_access_control.py on purpose) ────────────────

def _make_tool_call(tc_id: str, name: str, args: dict):
    """Build a fake ToolCall object matching the OpenAI SDK shape."""
    fc = SimpleNamespace(name=name, arguments=json.dumps(args))
    return SimpleNamespace(id=tc_id, function=fc)


def _llm_response(tool_calls=None, content=None):
    """Return a fake chat completion response."""
    msg = SimpleNamespace(tool_calls=tool_calls, content=content)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


# ─── Case 1: unknown tool_name — fail-closed since B.1 ────────────────────────

@pytest.mark.asyncio
async def test_unknown_tool_name_is_denied_fail_closed():
    """
    FIXED IN B.1 — before this phase, the volunteer gate at bot.py:553 only
    blocked names present in _ADMIN_ONLY_TOOLS, and the notify_volunteer
    check only matched that one literal name; a tool_name in neither set
    fell through to _run_tool and only failed there as an execution error,
    not a permission error (see B.0's original version of this test).

    Since B.1, call_grok_core.authorize() denies any tool_name outside
    KNOWN_TOOL_NAMES up front, before _run_tool is ever reached — for any
    user_type, including admin (see tests/test_call_grok_core.py invariant
    2 for the admin case).
    """
    tool_call = _make_tool_call(
        tc_id="call_unknown", name="totally_unknown_tool", args={"x": "y"},
    )
    first_response  = _llm_response(tool_calls=[tool_call])
    second_response = _llm_response(tool_calls=None, content="done")

    fake_context = MagicMock()
    bot._history[44444] = [{"role": "user", "content": "do the thing"}]

    with patch.object(bot._client.chat.completions, "create",
                      side_effect=[first_response, second_response]) as mock_llm, \
         patch("bot._run_tool") as mock_run_tool:

        result = await bot._call_grok(
            user_id=44444, user_type="volunteer", internal_id=1, context=fake_context,
        )

    mock_run_tool.assert_not_called()
    assert result == "done"

    second_call_messages = mock_llm.call_args_list[1].kwargs["messages"]
    tool_msg = next(m for m in second_call_messages if m.get("tool_call_id") == "call_unknown")
    assert json.loads(tool_msg["content"]) == {
        "ok": False, "error": "permission denied: unknown tool",
    }


# ─── Case 2: notify_admins by a volunteer — admin-only since B.1 ──────────────

@pytest.mark.asyncio
async def test_notify_admins_is_blocked_for_a_volunteer_since_b1():
    """
    FIXED IN B.1 — before this phase, bot.py:571-574 dispatched notify_admins
    for ANY user_type; there was no admin-only guard on that branch, unlike
    notify_volunteer. That was the live version of the prompt-injection risk
    flagged in the Q2 alignment (a volunteer could reach notify_admins via a
    Grok tool call with zero restriction — see B.0's original version of
    this test, which documented exactly that).

    Since B.1, call_grok_core.authorize() puts notify_admins in NOTIFY_TOOLS,
    admin-only with no exception — the same rule notify_volunteer already had.
    """
    tool_call = _make_tool_call(
        tc_id="call_notify_admins", name="notify_admins",
        args={"message": "urgent, come look"},
    )
    first_response  = _llm_response(tool_calls=[tool_call])
    second_response = _llm_response(tool_calls=None, content="I can't do that.")

    fake_context = MagicMock()
    bot._history[55555] = [{"role": "user", "content": "tell the admins"}]

    with patch.object(bot._client.chat.completions, "create",
                      side_effect=[first_response, second_response]) as mock_llm, \
         patch("bot._notify_admins_tool", new_callable=AsyncMock) as mock_notify_admins:

        result = await bot._call_grok(
            user_id=55555, user_type="volunteer", internal_id=7, context=fake_context,
        )

    mock_notify_admins.assert_not_called()
    assert result == "I can't do that."

    second_call_messages = mock_llm.call_args_list[1].kwargs["messages"]
    tool_msg = next(m for m in second_call_messages if m.get("tool_call_id") == "call_notify_admins")
    assert json.loads(tool_msg["content"]) == {"ok": False, "error": "permission denied"}


# ─── Case 3: notify_volunteer by a volunteer — blocked explicitly ─────────────

@pytest.mark.asyncio
async def test_notify_volunteer_is_blocked_for_a_volunteer():
    """
    Regression guard for the explicit block at bot.py:563-570. Unlike
    notify_admins (case 2), this branch already denies volunteers today —
    B.1 should preserve this outcome (now via authorize()), not change it.
    """
    tool_call = _make_tool_call(
        tc_id="call_notify_vol", name="notify_volunteer",
        args={"telegram_id": "123", "message": "hey"},
    )
    first_response  = _llm_response(tool_calls=[tool_call])
    second_response = _llm_response(tool_calls=None, content="I can't do that.")

    fake_context = MagicMock()
    bot._history[66666] = [{"role": "user", "content": "message this volunteer"}]

    with patch.object(bot._client.chat.completions, "create",
                      side_effect=[first_response, second_response]) as mock_llm, \
         patch("bot._notify_volunteer_tool", new_callable=AsyncMock) as mock_notify_vol:

        result = await bot._call_grok(
            user_id=66666, user_type="volunteer", internal_id=9, context=fake_context,
        )

    mock_notify_vol.assert_not_called()
    assert result == "I can't do that."

    # The denial reaches the model verbatim, as today's literal at bot.py:568.
    second_call_messages = mock_llm.call_args_list[1].kwargs["messages"]
    tool_msg = next(m for m in second_call_messages if m.get("tool_call_id") == "call_notify_vol")
    assert json.loads(tool_msg["content"]) == {"ok": False, "error": "permission denied"}


# ─── Case 4a: auto-notify after approve_draft — happy path ────────────────────

@pytest.mark.asyncio
async def test_auto_notify_after_approve_draft_notifies_each_volunteer():
    """
    Characterizes bot.py:588-606: on a successful approve_draft with
    volunteers_to_notify, each entry with a telegram_id and shifts gets a
    built shift message pushed via _notify_volunteer_tool — without the
    model having to call notify_volunteer itself.
    """
    approve_call = _make_tool_call(
        tc_id="call_approve", name="approve_draft",
        args={"week": "2026-07-06", "telegram_id": "8632082731"},
    )
    first_response  = _llm_response(tool_calls=[approve_call])
    second_response = _llm_response(tool_calls=None, content="Approved and notified.")

    fake_context = MagicMock()
    bot._history[77777] = [{"role": "user", "content": "approve the draft"}]

    run_tool_result = json.dumps({
        "ok": True,
        "week": "2026-07-06",
        "volunteers_to_notify": [{
            "telegram_id": "111",
            "name": "Ana Garcia",
            "language": "es",
            "shifts": [{"date": "2026-07-06", "type": "breakfast"}],
        }],
    })

    with patch.object(bot._client.chat.completions, "create",
                      side_effect=[first_response, second_response]), \
         patch("bot._run_tool", return_value=run_tool_result), \
         patch("bot._notify_volunteer_tool", new_callable=AsyncMock,
               return_value=json.dumps({"ok": True, "sent_to": "111"})) as mock_notify_vol:

        result = await bot._call_grok(
            user_id=77777, user_type="admin", internal_id="marielle", context=fake_context,
        )

    mock_notify_vol.assert_called_once()
    tg_id, message, ctx = mock_notify_vol.call_args[0]
    assert tg_id == "111"
    assert "Ana" in message
    assert ctx is fake_context
    assert result == "Approved and notified."


# ─── Case 4b: auto-notify after approve_draft — incomplete entry is skipped ────

@pytest.mark.asyncio
async def test_auto_notify_after_approve_draft_silently_skips_incomplete_entries():
    """
    ⚠️ TODAY'S BEHAVIOR: bot.py:601-602 (`if not tg_id or not shifts: continue`)
    drops a volunteer entry with no error surfaced anywhere — not to the admin,
    not to the model, only a line that never gets logged because the loop just
    continues. This is part of what B.3's plan_post_approve_notifications is
    meant to make visible instead of silent.
    """
    approve_call = _make_tool_call(
        tc_id="call_approve_incomplete", name="approve_draft",
        args={"week": "2026-07-06", "telegram_id": "8632082731"},
    )
    first_response  = _llm_response(tool_calls=[approve_call])
    second_response = _llm_response(tool_calls=None, content="Approved.")

    fake_context = MagicMock()
    bot._history[78888] = [{"role": "user", "content": "approve the draft"}]

    run_tool_result = json.dumps({
        "ok": True,
        "week": "2026-07-06",
        "volunteers_to_notify": [
            {"telegram_id": "", "name": "No Phone", "language": "en",
             "shifts": [{"date": "2026-07-06", "type": "breakfast"}]},
            {"telegram_id": "222", "name": "No Shifts", "language": "en", "shifts": []},
        ],
    })

    with patch.object(bot._client.chat.completions, "create",
                      side_effect=[first_response, second_response]), \
         patch("bot._run_tool", return_value=run_tool_result), \
         patch("bot._notify_volunteer_tool", new_callable=AsyncMock) as mock_notify_vol:

        result = await bot._call_grok(
            user_id=78888, user_type="admin", internal_id="marielle", context=fake_context,
        )

    mock_notify_vol.assert_not_called()
    assert result == "Approved."


# ─── Case 5: exception in post-approve notification is swallowed ──────────────

@pytest.mark.asyncio
async def test_auto_notify_exception_is_swallowed_and_loop_continues():
    """
    ⚠️ TODAY'S BEHAVIOR: bot.py:589/607-608 wraps the whole post-approve
    notification block in `except Exception as e: logger.error(...)` with no
    re-raise and no error surfaced in the tool_result already appended to the
    loop. A volunteer entry missing "name" makes
    `vol.get("name", "").split()[0]` raise IndexError — and _call_grok simply
    keeps going as if approve_draft had fully succeeded. This is the exact
    swallow B.3 (plan_post_approve_notifications) is scoped to eliminate.
    """
    approve_call = _make_tool_call(
        tc_id="call_approve_boom", name="approve_draft",
        args={"week": "2026-07-06", "telegram_id": "8632082731"},
    )
    first_response  = _llm_response(tool_calls=[approve_call])
    second_response = _llm_response(tool_calls=None, content="Approved.")

    fake_context = MagicMock()
    bot._history[88888] = [{"role": "user", "content": "approve the draft"}]

    run_tool_result = json.dumps({
        "ok": True,
        "week": "2026-07-06",
        "volunteers_to_notify": [{
            # no "name" key at all -> "".split()[0] raises IndexError inside
            # the try/except at bot.py:589-608.
            "telegram_id": "333",
            "shifts": [{"date": "2026-07-06", "type": "breakfast"}],
        }],
    })

    with patch.object(bot._client.chat.completions, "create",
                      side_effect=[first_response, second_response]), \
         patch("bot._run_tool", return_value=run_tool_result), \
         patch("bot._notify_volunteer_tool", new_callable=AsyncMock) as mock_notify_vol:

        # Must NOT raise — the IndexError is caught inside _call_grok today.
        result = await bot._call_grok(
            user_id=88888, user_type="admin", internal_id="marielle", context=fake_context,
        )

    # The crash happens before _notify_volunteer_tool is reached for this
    # entry, so the volunteer silently never gets notified.
    mock_notify_vol.assert_not_called()
    assert result == "Approved."


# ─── Case 6: max tool iterations reached — fixed fallback string ──────────────

@pytest.mark.asyncio
async def test_max_tool_iterations_returns_fixed_fallback():
    """
    Characterizes bot.py:527/616-618: if the model keeps requesting tool
    calls for _MAX_TOOL_ITER turns in a row, the loop gives up and returns
    the fixed escalation string, without ever returning plain text.
    """
    responses = [
        _llm_response(tool_calls=[
            _make_tool_call(tc_id=f"call_{i}", name="show_schedule", args={"week": "2026-07-06"})
        ])
        for i in range(bot._MAX_TOOL_ITER)
    ]

    fake_context = MagicMock()
    bot._history[99999] = [{"role": "user", "content": "show me everything, forever"}]

    with patch.object(bot._client.chat.completions, "create",
                      side_effect=responses) as mock_llm, \
         patch("bot._run_tool", return_value=json.dumps({"ok": True})) as mock_run_tool:

        result = await bot._call_grok(
            user_id=99999, user_type="admin", internal_id="marielle", context=fake_context,
        )

    assert mock_llm.call_count == bot._MAX_TOOL_ITER
    assert mock_run_tool.call_count == bot._MAX_TOOL_ITER
    assert result == (
        "Something complex came up and I couldn't finish. "
        "I've flagged it for Moises to take a look."
    )
