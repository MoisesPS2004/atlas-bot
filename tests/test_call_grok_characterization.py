"""
Vertical Slice: characterization suite for _call_grok (bot.py:522-618) — Hueco B / B.0.

These tests pin the behavior of _call_grok. They are the strangler-fig
safety net: every phase (B.0-B.4) must keep this suite GREEN except where a
test is explicitly marked "FIXED IN B.n" — those document an intentional
behavior change agreed in that phase's Grill Me, not a regression.

This file intentionally does NOT duplicate the contract already locked by
tests/test_access_control.py (pin_identity for save_preferences, admin-only
block for the pre-existing admin-only tools). It covers the paths that file
leaves uncharacterized: the default for tool_names outside any explicit
allow/deny list, the two notify_* tools, the auto-notify-after-approve_draft
branch, and the max-iteration fallback.

Since Hueco B / B.4, _call_grok no longer reaches for module globals
(_client, _run_tool, _notify_admins_tool, _notify_volunteer_tool) — it
depends only on an injected call_grok_core.Deps(call_model, perform). These
tests construct fake deps directly (no bot.* patching, no network).
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, AsyncMock

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


def _make_call_model(*responses):
    """Fake deps.call_model: yields each fake LLM response in order, one per agentic-loop turn."""
    return Mock(side_effect=list(responses))


def _make_perform(**responses):
    """
    Fake deps.perform: a single AsyncMock whose side_effect branches on
    tool_name. `responses` maps tool_name -> the JSON string to return; any
    tool_name not listed returns '{"ok": true}'. Every call is recorded on
    the returned mock via the normal AsyncMock call_args_list/assert_* API.
    """
    def _side_effect(tool_name, tool_args, context, acting_user_id):
        return responses.get(tool_name, json.dumps({"ok": True}))
    return AsyncMock(side_effect=_side_effect)


def _calls_for(mock_perform, tool_name):
    """Filter a fake perform's call_args_list down to calls for one tool_name."""
    return [c for c in mock_perform.call_args_list if c.args[0] == tool_name]


# ─── Case 1: unknown tool_name — fail-closed since B.1 ────────────────────────

@pytest.mark.asyncio
async def test_unknown_tool_name_is_denied_fail_closed():
    """
    FIXED IN B.1 — before this phase, the volunteer gate only blocked names
    present in the admin-only set, and the notify_volunteer check only
    matched that one literal name; a tool_name in neither set fell through
    to the tool runner and only failed there as an execution error, not a
    permission error (see B.0's original version of this test).

    Since B.1, call_grok_core.authorize() denies any tool_name outside
    KNOWN_TOOL_NAMES up front, before deps.perform is ever reached — for any
    user_type, including admin (see tests/test_call_grok_core.py invariant
    2 for the admin case).
    """
    tool_call = _make_tool_call(
        tc_id="call_unknown", name="totally_unknown_tool", args={"x": "y"},
    )
    first_response  = _llm_response(tool_calls=[tool_call])
    second_response = _llm_response(tool_calls=None, content="done")

    fake_context = MagicMock()
    bot._store.append_history(44444, "user", "do the thing")

    mock_call_model = _make_call_model(first_response, second_response)
    mock_perform = _make_perform()

    result = await bot._call_grok(
        user_id=44444, user_type="volunteer", internal_id=1, context=fake_context,
        deps=bot.Deps(call_model=mock_call_model, perform=mock_perform),
    )

    mock_perform.assert_not_called()
    assert result == "done"

    second_call_messages = mock_call_model.call_args_list[1].args[0]
    tool_msg = next(m for m in second_call_messages if m.get("tool_call_id") == "call_unknown")
    assert json.loads(tool_msg["content"]) == {
        "ok": False, "error": "permission denied: unknown tool",
    }


# ─── Case 2: notify_admins by a volunteer — admin-only since B.1 ──────────────

@pytest.mark.asyncio
async def test_notify_admins_is_blocked_for_a_volunteer_since_b1():
    """
    FIXED IN B.1 — before this phase, notify_admins was dispatched for ANY
    user_type; there was no admin-only guard on that branch, unlike
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
    bot._store.append_history(55555, "user", "tell the admins")

    mock_call_model = _make_call_model(first_response, second_response)
    mock_perform = _make_perform()

    result = await bot._call_grok(
        user_id=55555, user_type="volunteer", internal_id=7, context=fake_context,
        deps=bot.Deps(call_model=mock_call_model, perform=mock_perform),
    )

    mock_perform.assert_not_called()
    assert result == "I can't do that."

    second_call_messages = mock_call_model.call_args_list[1].args[0]
    tool_msg = next(m for m in second_call_messages if m.get("tool_call_id") == "call_notify_admins")
    assert json.loads(tool_msg["content"]) == {"ok": False, "error": "permission denied"}


# ─── Case 3: notify_volunteer by a volunteer — blocked explicitly ─────────────

@pytest.mark.asyncio
async def test_notify_volunteer_is_blocked_for_a_volunteer():
    """
    Regression guard for the notify_volunteer block. Unlike notify_admins
    (case 2), this branch already denied volunteers before B.1 — every phase
    since should preserve this outcome (now via authorize()), not change it.
    """
    tool_call = _make_tool_call(
        tc_id="call_notify_vol", name="notify_volunteer",
        args={"telegram_id": "123", "message": "hey"},
    )
    first_response  = _llm_response(tool_calls=[tool_call])
    second_response = _llm_response(tool_calls=None, content="I can't do that.")

    fake_context = MagicMock()
    bot._store.append_history(66666, "user", "message this volunteer")

    mock_call_model = _make_call_model(first_response, second_response)
    mock_perform = _make_perform()

    result = await bot._call_grok(
        user_id=66666, user_type="volunteer", internal_id=9, context=fake_context,
        deps=bot.Deps(call_model=mock_call_model, perform=mock_perform),
    )

    mock_perform.assert_not_called()
    assert result == "I can't do that."

    second_call_messages = mock_call_model.call_args_list[1].args[0]
    tool_msg = next(m for m in second_call_messages if m.get("tool_call_id") == "call_notify_vol")
    assert json.loads(tool_msg["content"]) == {"ok": False, "error": "permission denied"}


# ─── Case 4a: auto-notify after approve_draft — happy path ────────────────────

@pytest.mark.asyncio
async def test_auto_notify_after_approve_draft_notifies_each_volunteer():
    """
    On a successful approve_draft with volunteers_to_notify, each entry with
    a telegram_id and shifts gets a built shift message pushed via
    deps.perform("notify_volunteer", ...) — without the model having to call
    notify_volunteer itself.
    """
    approve_call = _make_tool_call(
        tc_id="call_approve", name="approve_draft",
        args={"week": "2026-07-06", "telegram_id": "8632082731"},
    )
    first_response  = _llm_response(tool_calls=[approve_call])
    second_response = _llm_response(tool_calls=None, content="Approved and notified.")

    fake_context = MagicMock()
    bot._store.append_history(77777, "user", "approve the draft")

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

    mock_call_model = _make_call_model(first_response, second_response)
    mock_perform = _make_perform(approve_draft=run_tool_result)

    result = await bot._call_grok(
        user_id=77777, user_type="admin", internal_id="marielle", context=fake_context,
        deps=bot.Deps(call_model=mock_call_model, perform=mock_perform),
    )

    notify_calls = _calls_for(mock_perform, "notify_volunteer")
    assert len(notify_calls) == 1
    tool_name, tool_args, ctx, acting_user_id = notify_calls[0].args
    assert tool_args["telegram_id"] == "111"
    assert "Ana" in tool_args["message"]
    assert ctx is fake_context
    assert result == "Approved and notified."


# ─── Case 4b: auto-notify after approve_draft — incomplete entries are logged ──

@pytest.mark.asyncio
async def test_auto_notify_after_approve_draft_logs_incomplete_entries_since_b3(caplog):
    """
    FIXED IN B.3 — before this phase, `if not tg_id or not shifts: continue`
    dropped a volunteer entry with no error surfaced anywhere: not to the
    admin, not to the model, not even to the logs (see B.0's original
    version of this test, named "silently_skips").

    Since B.3, call_grok_core.plan_post_approve_notifications() classifies
    each entry and returns the invalid ones with a reason; bot.py logs a
    warning for each one instead of dropping it silently. The volunteers
    still don't get a Telegram message (there's nothing valid to send), but
    the failure is now visible in the logs.
    """
    approve_call = _make_tool_call(
        tc_id="call_approve_incomplete", name="approve_draft",
        args={"week": "2026-07-06", "telegram_id": "8632082731"},
    )
    first_response  = _llm_response(tool_calls=[approve_call])
    second_response = _llm_response(tool_calls=None, content="Approved.")

    fake_context = MagicMock()
    bot._store.append_history(78888, "user", "approve the draft")

    run_tool_result = json.dumps({
        "ok": True,
        "week": "2026-07-06",
        "volunteers_to_notify": [
            {"telegram_id": "", "name": "No Phone", "language": "en",
             "shifts": [{"date": "2026-07-06", "type": "breakfast"}]},
            {"telegram_id": "222", "name": "No Shifts", "language": "en", "shifts": []},
        ],
    })

    mock_call_model = _make_call_model(first_response, second_response)
    mock_perform = _make_perform(approve_draft=run_tool_result)

    with caplog.at_level("WARNING"):
        result = await bot._call_grok(
            user_id=78888, user_type="admin", internal_id="marielle", context=fake_context,
            deps=bot.Deps(call_model=mock_call_model, perform=mock_perform),
        )

    assert _calls_for(mock_perform, "notify_volunteer") == []
    assert result == "Approved."

    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("missing telegram_id" in w for w in warnings)
    assert any("missing shifts" in w for w in warnings)


# ─── Case 5: a malformed entry no longer takes down the whole batch ───────────

@pytest.mark.asyncio
async def test_auto_notify_isolates_a_malformed_entry_from_the_rest_of_the_batch(caplog):
    """
    FIXED IN B.3 — before this phase, one volunteer entry missing "name"
    made `vol.get("name", "").split()[0]` raise IndexError, caught by a
    generic `except Exception` that wrapped the WHOLE post-approve block —
    not just that one entry. That silently aborted notifications for EVERY
    volunteer in the same approve_draft batch, including ones after the bad
    entry that were otherwise perfectly valid (see B.0's original version
    of this test, named "..._is_swallowed_and_loop_continues").

    Since B.3, plan_post_approve_notifications() never raises: it evaluates
    each entry independently. A malformed entry is skipped and logged; every
    other, valid entry in the same batch still gets notified.
    """
    approve_call = _make_tool_call(
        tc_id="call_approve_boom", name="approve_draft",
        args={"week": "2026-07-06", "telegram_id": "8632082731"},
    )
    first_response  = _llm_response(tool_calls=[approve_call])
    second_response = _llm_response(tool_calls=None, content="Approved.")

    fake_context = MagicMock()
    bot._store.append_history(88888, "user", "approve the draft")

    run_tool_result = json.dumps({
        "ok": True,
        "week": "2026-07-06",
        "volunteers_to_notify": [
            {
                # No "name" key at all — used to raise IndexError and take
                # down the whole batch (see docstring above).
                "telegram_id": "333",
                "shifts": [{"date": "2026-07-06", "type": "breakfast"}],
            },
            {
                "telegram_id": "444",
                "name": "Bob Volunteer",
                "language": "en",
                "shifts": [{"date": "2026-07-06", "type": "breakfast"}],
            },
        ],
    })

    mock_call_model = _make_call_model(first_response, second_response)
    mock_perform = _make_perform(approve_draft=run_tool_result)

    with caplog.at_level("WARNING"):
        result = await bot._call_grok(
            user_id=88888, user_type="admin", internal_id="marielle", context=fake_context,
            deps=bot.Deps(call_model=mock_call_model, perform=mock_perform),
        )

    assert result == "Approved."

    # The valid entry (Bob, 444) still gets notified despite its malformed
    # neighbor in the same batch.
    notify_calls = _calls_for(mock_perform, "notify_volunteer")
    assert len(notify_calls) == 1
    tool_name, tool_args, ctx, acting_user_id = notify_calls[0].args
    assert tool_args["telegram_id"] == "444"
    assert "Bob" in tool_args["message"]

    # The malformed entry is visible in the logs, not silently dropped.
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("missing name" in w for w in warnings)


# ─── Case 6: max tool iterations reached — fixed fallback string ──────────────

@pytest.mark.asyncio
async def test_max_tool_iterations_returns_fixed_fallback():
    """
    If the model keeps requesting tool calls for _MAX_TOOL_ITER turns in a
    row, the loop gives up and returns the fixed escalation string, without
    ever returning plain text.
    """
    responses = [
        _llm_response(tool_calls=[
            _make_tool_call(tc_id=f"call_{i}", name="show_schedule", args={"week": "2026-07-06"})
        ])
        for i in range(bot._MAX_TOOL_ITER)
    ]

    fake_context = MagicMock()
    bot._store.append_history(99999, "user", "show me everything, forever")

    mock_call_model = _make_call_model(*responses)
    mock_perform = _make_perform()

    result = await bot._call_grok(
        user_id=99999, user_type="admin", internal_id="marielle", context=fake_context,
        deps=bot.Deps(call_model=mock_call_model, perform=mock_perform),
    )

    assert mock_call_model.call_count == bot._MAX_TOOL_ITER
    assert mock_perform.call_count == bot._MAX_TOOL_ITER
    assert result == (
        "Something complex came up and I couldn't finish. "
        "I've flagged it for Moises to take a look."
    )
