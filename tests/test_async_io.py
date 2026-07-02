"""
Hueco H (Sesión 10) — criterios de aceptación RED del event loop no bloqueante.

Fija el contrato nuevo de la frontera de efectos: Deps.call_model pasa a ser
AWAITABLE (AsyncOpenAI) y el driver hace `await deps.call_model(...)`. Los
fakes de este archivo son `async def` A PROPÓSITO — con el driver actual
(síncrono) fallan de forma controlada, demostrando que el loop no espera
corutinas. Eso es el RED.

Dos criterios del ROADMAP quedan fijados acá:

  1. Test estrella (concurrencia): el turno lento de un usuario (call_model
     congelado en un asyncio.Event) NO bloquea el flujo agéntico completo de
     otro usuario. Hoy es imposible en verde: el event loop es único y las
     llamadas costosas son síncronas.

  2. Deadline de turno (120s en producción, parcheado corto acá): si el turno
     excede bot._TURN_DEADLINE, el usuario recibe bot._TIMEOUT_REPLY (degradación
     amable, no silencio), el lock por-usuario queda liberado y el historial
     queda coherente (el turno cierra con un mensaje assistant).
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# conftest.py already stubs out .env / openai / telegram before this import.
import bot


# ─── Helpers (mismos shapes que test_call_grok_characterization.py) ───────────

def _make_tool_call(tc_id: str, name: str, args: dict):
    """Build a fake ToolCall object matching the OpenAI SDK shape."""
    fc = SimpleNamespace(name=name, arguments=json.dumps(args))
    return SimpleNamespace(id=tc_id, function=fc)


def _llm_response(tool_calls=None, content=None):
    """Return a fake chat completion response."""
    msg = SimpleNamespace(tool_calls=tool_calls, content=content)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


# ─── 1. Test estrella: Alice colgada no bloquea a Bob ─────────────────────────

@pytest.mark.asyncio
async def test_slow_model_call_for_one_user_does_not_block_another():
    """
    Alice (call_model congelado en un Event) y Bob (flujo agéntico completo:
    tool call + respuesta final) corren concurrentes. Bob debe terminar su
    turno entero mientras Alice sigue pendiente. Éste es el criterio de
    aceptación de la Sesión 10 en el ROADMAP.
    """
    ALICE, BOB = 101010, 202020

    alice_gate = asyncio.Event()

    async def alice_call_model(messages):
        await alice_gate.wait()          # congelada hasta que el test la libere
        return _llm_response(tool_calls=None, content="alice reply")

    bob_responses = [
        _llm_response(tool_calls=[
            _make_tool_call("call_bob_1", "show_schedule", {"week": "2026-07-06"})
        ]),
        _llm_response(tool_calls=None, content="bob reply"),
    ]

    async def bob_call_model(messages):
        return bob_responses.pop(0)

    perform = AsyncMock(return_value='{"ok": true}')
    bot._store.append_history(ALICE, "user", "algo que tarda")
    bot._store.append_history(BOB, "user", "muéstrame el horario")

    alice_task = asyncio.create_task(bot._call_grok(
        user_id=ALICE, user_type="admin", internal_id="moises",
        context=MagicMock(), deps=bot.Deps(call_model=alice_call_model, perform=perform),
    ))
    bob_task = asyncio.create_task(bot._call_grok(
        user_id=BOB, user_type="admin", internal_id="marielle",
        context=MagicMock(), deps=bot.Deps(call_model=bob_call_model, perform=perform),
    ))

    done, _ = await asyncio.wait({bob_task}, timeout=2.0)
    try:
        assert bob_task in done, (
            "Bob quedó bloqueado: el turno colgado de Alice congeló el event "
            "loop para todos los demás usuarios."
        )
        assert bob_task.result() == "bob reply"
        assert not alice_task.done(), (
            "Alice debería seguir pendiente (su call_model está gateado)"
        )
    finally:
        alice_gate.set()

    assert await alice_task == "alice reply"

    # El flujo de Bob fue completo: su tool call sí se ejecutó.
    assert any(c.args[0] == "show_schedule" for c in perform.call_args_list)


# ─── 2. Deadline de turno: degradación amable + lock liberado ─────────────────

@pytest.mark.asyncio
async def test_turn_deadline_degrades_gracefully_and_releases_the_lock(monkeypatch):
    """
    Con el modelo colgado indefinidamente, el turno debe cortarse en
    bot._TURN_DEADLINE (parcheado a 0.2s), responder bot._TIMEOUT_REPLY al
    usuario, liberar el lock por-usuario y cerrar el historial con un turno
    assistant — nunca un cuelgue sosteniendo el lock.
    """
    USER = 424242

    # Contrato: estas dos constantes deben existir en bot (RED si faltan).
    monkeypatch.setattr(bot, "_TURN_DEADLINE", 0.2)
    timeout_reply = bot._TIMEOUT_REPLY

    # Acceso stubbeado: sin tocar aquarela.db real.
    monkeypatch.setattr(bot, "_check_access", lambda uid: ("admin", "moises"))

    hang_forever = asyncio.Event()

    async def hung_call_model(messages):
        await hang_forever.wait()
        return _llm_response(tool_calls=None, content="too late")

    monkeypatch.setattr(bot, "_PROD_DEPS", bot.Deps(
        call_model=hung_call_model,
        perform=AsyncMock(return_value='{"ok": true}'),
    ))

    replies = []

    async def reply_text(text):
        replies.append(text)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=USER),
        message=SimpleNamespace(text="hola", reply_text=reply_text),
    )

    # El handler entero debe resolverse en ~_TURN_DEADLINE; si el deadline no
    # existe, el wait_for de acá corta el test de forma controlada (RED).
    await asyncio.wait_for(bot.handle_message(update, MagicMock()), timeout=2.0)

    assert replies == [timeout_reply], (
        f"El usuario debía recibir la degradación amable {timeout_reply!r}, "
        f"recibió: {replies!r}"
    )
    assert not bot._store.lock(USER).locked(), (
        "El lock por-usuario quedó tomado tras el timeout — el próximo "
        "mensaje del usuario quedaría bloqueado para siempre."
    )
    history = bot._store.get_history(USER)
    assert history and history[-1]["role"] == "assistant", (
        "El turno debe cerrar el historial con un mensaje assistant."
    )
