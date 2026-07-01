"""
call_grok_core.py — Núcleo funcional del loop agéntico de _call_grok (bot.py:522-618).

Sin imports de bot, de telegram, ni de openai. Sin IO. Testeable de forma
aislada. Mismo patrón que training_callbacks.handle_training_callback: el
driver imperativo en bot.py llama a estas funciones puras y actúa según lo
que devuelven.

Hueco B (strangler-fig, por fases):
  B.1 (este archivo, por ahora) → authorize() + pin_identity()
  B.2 → parseo de tool calls + ensamblado de mensajes assistant/tool
  B.3 → plan_post_approve_notifications()
  B.4 → frontera de efectos (deps.perform)

authorize() — invariantes de seguridad (Hueco B / B.1 Grill Me):
  1. Fail-closed — cualquier tool_name fuera de KNOWN_TOOL_NAMES es denegado,
     sin importar el rol. Un nombre no registrado (typo, tool retirado, o uno
     inventado por un modelo con prompt injection) nunca debe llegar al
     dispatcher. Antes de B.1 esto era fail-open: caía al "else" y solo
     fallaba en _run_tool como error de ejecución, no de autorización
     (ver tests/test_call_grok_characterization.py Caso 1, B.0).
  2. notify_admins y notify_volunteer son admin-only sin excepción. Antes de
     B.1, notify_admins no tenía NINGUNA restricción de rol — un volunteer
     podía dispararlo directo, incluida la ruta de prompt injection sobre
     Grok (ver Caso 2, B.0). Ahora toda la familia notify_* comparte la
     misma regla que ya tenía notify_volunteer.
  3. El resto de ADMIN_ONLY_TOOLS sigue siendo admin-only (comportamiento
     preexistente, sin cambios).
  4. Todo lo demás (OPEN_TOOLS) queda abierto a cualquier user_type conocido.
"""
from __future__ import annotations

ADMIN_ONLY_TOOLS = frozenset({
    "generate_draft",
    "delete_draft",
    "approve_draft",
    "deactivate_volunteer",
    "update_volunteer_dates",
    "list_volunteers",
    "confirm_training",
    "report_no_show",
    "schedule_admin_training",
})

NOTIFY_TOOLS = frozenset({
    "notify_admins",
    "notify_volunteer",
})

OPEN_TOOLS = frozenset({
    "show_schedule",
    "save_preferences",
    "show_volunteer",
})

KNOWN_TOOL_NAMES = ADMIN_ONLY_TOOLS | NOTIFY_TOOLS | OPEN_TOOLS


def authorize(user_type: str, tool_name: str) -> tuple[bool, str | None]:
    """
    Pure authorization gate for the _call_grok tool-dispatch loop.

    Retorna (allowed, error_message). error_message es None cuando allowed
    es True; en caso contrario trae el mismo texto que el driver ya venía
    devolviendo al modelo antes de la extracción, para no romper el contrato
    observado por B.0.
    """
    if tool_name not in KNOWN_TOOL_NAMES:
        return False, "permission denied: unknown tool"
    if user_type == "admin":
        return True, None
    if tool_name in NOTIFY_TOOLS:
        return False, "permission denied"
    if tool_name in ADMIN_ONLY_TOOLS:
        return False, "permission denied: this action is for admins only"
    return True, None


def pin_identity(user_type: str, tool_name: str, tool_args: dict, internal_id) -> dict:
    """
    Pinea los campos de identidad self-service a la sesión autenticada — el
    LLM nunca debe ser la autoridad sobre quién es el llamante.

    No muta tool_args; retorna un dict nuevo cuando aplica una regla de
    pinning, o el mismo dict recibido cuando no aplica ninguna.
    """
    if user_type == "volunteer" and tool_name == "save_preferences":
        return {**tool_args, "volunteer_id": internal_id}
    return tool_args
