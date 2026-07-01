"""
Vertical Slice — Hueco F: "Disclosure entre voluntarios".

Business rule (decided explicitly in Sesión 8, no longer a default-by-absence):
  1. show_schedule    → transparencia total: un voluntario ve el horario completo.
  2. save_preferences → escritura SOLO sobre el perfil propio (ya pineado en Hueco B).
  3. show_volunteer   → PII ajeno bloqueado por defecto: un voluntario solo puede
                        resolver su PROPIO registro. El sujeto de la consulta se
                        ata (bind, no check) a caller_telegram_id en pin_identity;
                        cualquier telegram_id/name ajeno que inyecte el LLM se
                        descarta estructuralmente.

Estos tests exercen el boundary real de _call_grok vía Deps inyectadas e
inspeccionan deps.perform.call_args — mismo idiom que test_access_control.py.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, AsyncMock

import pytest

# conftest.py already stubs out .env / openai / telegram before this import.
import bot


# ─── Helpers (mismo shape que test_access_control.py) ──────────────────────────

def _make_tool_call(tc_id: str, name: str, args: dict):
    fc = SimpleNamespace(name=name, arguments=json.dumps(args))
    return SimpleNamespace(id=tc_id, function=fc)


def _llm_response(tool_calls=None, content=None):
    msg = SimpleNamespace(tool_calls=tool_calls, content=content)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


def _make_deps(*responses, perform_result='{"ok": true}'):
    return bot.Deps(
        call_model=Mock(side_effect=list(responses)),
        perform=AsyncMock(return_value=perform_result),
    )


# ─── Test 1: show_schedule — transparencia total para voluntarios ──────────────

@pytest.mark.asyncio
async def test_volunteer_can_see_full_schedule():
    """
    Fija la decisión intencional: un voluntario que pide el horario completo
    debe pasar por authorize() y alcanzar deps.perform sin denegación. Esto
    convierte el "default por ausencia" en una regla explícita y testeada.
    """
    schedule_call = _make_tool_call(
        tc_id="call_sched",
        name="show_schedule",
        args={"week": "2026-07-07"},
    )
    first_response  = _llm_response(tool_calls=[schedule_call])
    second_response = _llm_response(tool_calls=None, content="Here is the full schedule.")

    fake_context = MagicMock()
    deps = _make_deps(first_response, second_response, perform_result=json.dumps({"ok": True}))
    bot._store.append_history(44444, "user", "Show me the whole schedule for next week")

    await bot._call_grok(
        user_id=44444,
        user_type="volunteer",
        internal_id=7,
        context=fake_context,
        deps=deps,
    )

    deps.perform.assert_called_once()
    actual_name, actual_args, _, _ = deps.perform.call_args.args
    assert actual_name == "show_schedule"
    assert actual_args["week"] == "2026-07-07"


# ─── Test 2: save_preferences — bind al perfil propio (regresión Hueco B) ──────

@pytest.mark.asyncio
async def test_volunteer_save_preferences_is_bound_to_self():
    """
    Guarda de regresión sobre el ownership de escritura: si el LLM intenta
    save_preferences con un volunteer_id ajeno, pin_identity() debe forzarlo
    al internal_id autenticado. Fija el comportamiento ya correcto del Hueco B
    dentro del alcance explícito del Hueco F.
    """
    ATTACKER_INTERNAL_ID = 42
    VICTIM_INTERNAL_ID   = 999

    spoofed_call = _make_tool_call(
        tc_id="call_pref",
        name="save_preferences",
        args={"volunteer_id": VICTIM_INTERNAL_ID, "data": '{"day_rec": "avoid"}'},
    )
    first_response  = _llm_response(tool_calls=[spoofed_call])
    second_response = _llm_response(tool_calls=None, content="Preferences saved!")

    fake_context = MagicMock()
    deps = _make_deps(first_response, second_response, perform_result=json.dumps({"ok": True}))
    bot._store.append_history(55555, "user", "Set volunteer 999 to avoid day reception")

    await bot._call_grok(
        user_id=55555,
        user_type="volunteer",
        internal_id=ATTACKER_INTERNAL_ID,
        context=fake_context,
        deps=deps,
    )

    deps.perform.assert_called_once()
    _, actual_args, _, _ = deps.perform.call_args.args
    assert actual_args["volunteer_id"] == ATTACKER_INTERNAL_ID, (
        f"SECURITY: perform() received volunteer_id={actual_args['volunteer_id']} "
        f"instead of the authenticated {ATTACKER_INTERNAL_ID}."
    )


# ─── Test 3 (RED real): show_volunteer no debe filtrar PII ajeno ───────────────

@pytest.mark.asyncio
async def test_volunteer_cannot_look_up_another_volunteer_pii():
    """
    EL RED: un voluntario (telegram_id 77777) intenta show_volunteer contra
    OTRO voluntario, pasando tanto un telegram_id ajeno como un name ajeno.

    La regla de negocio: PII ajeno bloqueado por defecto. pin_identity() debe
    atar el sujeto de la consulta a caller_telegram_id — el telegram_id/name
    del tercero deben desaparecer antes de llegar a deps.perform.

    HOY este test FALLA: pin_identity() no toca show_volunteer, así que el
    telegram_id de la víctima llega intacto a perform() y expone su PII.
    """
    CALLER_TELEGRAM_ID = 77777
    VICTIM_TELEGRAM_ID = "99999"
    VICTIM_NAME        = "Otro Voluntario"

    malicious_call = _make_tool_call(
        tc_id="call_vol",
        name="show_volunteer",
        args={"telegram_id": VICTIM_TELEGRAM_ID, "name": VICTIM_NAME},
    )
    first_response  = _llm_response(tool_calls=[malicious_call])
    second_response = _llm_response(tool_calls=None, content="Here you go.")

    fake_context = MagicMock()
    deps = _make_deps(first_response, second_response, perform_result=json.dumps({"ok": True}))
    bot._store.append_history(77777, "user", "Show me the record for Otro Voluntario")

    await bot._call_grok(
        user_id=CALLER_TELEGRAM_ID,
        user_type="volunteer",
        internal_id=13,
        context=fake_context,
        deps=deps,
    )

    deps.perform.assert_called_once()
    actual_name, actual_args, _, _ = deps.perform.call_args.args
    assert actual_name == "show_volunteer"

    # (a) El sujeto debe quedar atado al llamante.
    assert str(actual_args.get("telegram_id")) == str(CALLER_TELEGRAM_ID), (
        f"SECURITY: show_volunteer dispatched with telegram_id="
        f"{actual_args.get('telegram_id')!r}, pero el llamante autenticado es "
        f"{CALLER_TELEGRAM_ID}. Se filtró PII ajeno."
    )
    # (b) El telegram_id de la víctima no debe sobrevivir.
    assert str(actual_args.get("telegram_id")) != str(VICTIM_TELEGRAM_ID)
    # (c) El name ajeno inyectado por el LLM no debe sobrevivir.
    assert actual_args.get("name") != VICTIM_NAME, (
        "SECURITY: el 'name' del tercero llegó a perform(); el bind a self "
        "debe descartar el criterio de búsqueda ajeno."
    )
