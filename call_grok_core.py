"""
call_grok_core.py — Núcleo funcional del loop agéntico de _call_grok (bot.py:522-618).

Sin imports de bot, de telegram, ni de openai. Sin IO. Testeable de forma
aislada. Mismo patrón que training_callbacks.handle_training_callback: el
driver imperativo en bot.py llama a estas funciones puras y actúa según lo
que devuelven.

Hueco B (strangler-fig, por fases):
  B.1 → authorize() + pin_identity()
  B.2 → parseo de tool calls + ensamblado de mensajes assistant/tool
  B.3 → plan_post_approve_notifications()
  B.4 (este archivo, por ahora) → Deps, el shape de la frontera de efectos
B.5 (colapsar el loop de _call_grok en un intérprete puro) queda fuera de
alcance a menos que se pida explícitamente — B.4 deja el driver en bot.py
como shell imperativo delgado, dependiendo únicamente de Deps para tocar
el mundo real (API del modelo, Telegram, CLI del engine).

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

import json
import re
from datetime import date
from typing import Callable, NamedTuple

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


def pin_identity(
    user_type: str,
    tool_name: str,
    tool_args: dict,
    internal_id,
    caller_telegram_id=None,
) -> dict:
    """
    Pinea los campos de identidad self-service a la sesión autenticada — el
    LLM nunca debe ser la autoridad sobre quién es el llamante.

    No muta tool_args; retorna un dict nuevo cuando aplica una regla de
    pinning, o el mismo dict recibido cuando no aplica ninguna.

    caller_telegram_id es el telegram_id del llamante (user_id de Telegram);
    es opcional para compatibilidad hacia atrás. Solo lo consumen las reglas
    de disclosure entre voluntarios (Hueco F).
    """
    if user_type == "volunteer" and tool_name == "save_preferences":
        return {**tool_args, "volunteer_id": internal_id}
    # Hueco F — disclosure entre voluntarios: un voluntario solo puede resolver
    # su PROPIO registro. Bind, no check: descartamos por completo cualquier
    # criterio ajeno (name / telegram_id de terceros) que el LLM haya inyectado
    # y atamos la consulta a caller_telegram_id. La query insegura queda
    # estructuralmente irrepresentable (fail-closed por construcción).
    if user_type == "volunteer" and tool_name == "show_volunteer":
        return {"telegram_id": caller_telegram_id}
    return tool_args


# ─── B.2: parseo de tool calls + ensamblado de mensajes ────────────────────────

def parse_tool_call_args(arguments_json: str) -> dict:
    """
    Parsea el string JSON de argumentos de un tool call. Envoltorio puro de
    json.loads — sin manejo de error nuevo: un JSON malformado sigue
    propagando la excepción tal como lo hacía el driver antes de B.2.
    """
    return json.loads(arguments_json)


def build_assistant_tool_calls_message(content: str, tool_calls) -> dict:
    """
    Construye el mensaje 'assistant' que hace eco de los tool_calls del
    modelo hacia atrás en la conversación, con la forma exacta que espera
    la API compatible con OpenAI. tool_calls es la lista cruda del SDK
    (cada item expone .id y .function.name/.arguments).
    """
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in tool_calls
        ],
    }


def build_tool_result_message(tool_call_id: str, content: str) -> dict:
    """Construye un mensaje 'tool' que lleva el resultado (o denegación) de un tool call."""
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def build_denial_result(error: str) -> str:
    """Codifica en JSON una denegación de authorize() en la forma {"ok": false, "error": ...} que el modelo espera como tool_result."""
    return json.dumps({"ok": False, "error": error})


# ─── B.3: plan_post_approve_notifications ──────────────────────────────────────

def plan_post_approve_notifications(volunteers_to_notify: list) -> dict:
    """
    Triaje puro de la lista volunteers_to_notify de un approve_draft exitoso.

    Antes de B.3, una entrada sin "name" disparaba
    `vol.get("name", "").split()[0]` -> IndexError, capturado por un
    except Exception genérico que envolvía TODO el bloque de post-approve
    (no solo esa entrada) — así que una única entrada malformada abortaba
    en silencio las notificaciones de TODO el lote, incluidas las entradas
    válidas que venían después (ver B.0 Caso 5). Esta función nunca lanza:
    cada entrada se evalúa de forma independiente y se clasifica, así que
    una entrada inválida no afecta el procesamiento del resto.

    No toca red, no conoce telegram_id como concepto de I/O — solo evalúa
    los datos ya presentes en cada entrada.

    Retorna {"valid": [...], "invalid": [...]}.
      - valid: dicts con {"telegram_id", "name", "language", "shifts"},
        listos para _build_shift_message + _notify_volunteer_tool. "name"
        ya viene reducido al primer nombre (mismo recorte que el driver
        original).
      - invalid: dicts con {"entry": <dict original>, "reason": str} — el
        shell debe loguearlas de forma visible, no descartarlas en silencio.
    """
    valid: list = []
    invalid: list = []
    for vol in volunteers_to_notify:
        tg_id  = vol.get("telegram_id")
        shifts = vol.get("shifts", [])
        name   = vol.get("name", "")

        if not tg_id:
            invalid.append({"entry": vol, "reason": "missing telegram_id"})
            continue
        if not shifts:
            invalid.append({"entry": vol, "reason": "missing shifts"})
            continue
        if not name.strip():
            invalid.append({"entry": vol, "reason": "missing name"})
            continue

        valid.append({
            "telegram_id": tg_id,
            "name": name.split()[0],
            "language": vol.get("language", "en"),
            "shifts": shifts,
        })
    return {"valid": valid, "invalid": invalid}


# ─── B.4: frontera de efectos ───────────────────────────────────────────────────

class Deps(NamedTuple):
    """
    Frontera de efectos inyectada en el driver _call_grok de bot.py. Es solo
    el shape (0 I/O) — las implementaciones reales viven en bot.py, las
    fakes/mocks deterministas viven en los tests.

    call_model(messages) -> response (awaitable)
        Única vía por la que el loop agéntico llega a la API del modelo.
        Async desde Hueco H (AsyncOpenAI); el driver hace await. El SDK ya
        no se llama directamente (_client vive detrás de esta frontera).

    perform(tool_name, tool_args, context, acting_user_id) -> str (awaitable)
        Única vía por la que el loop dispara un efecto real: Telegram
        (notify_admins/notify_volunteer) o el CLI del engine (todo lo
        demás). El driver ya no llama a _run_tool, _notify_admins_tool ni
        _notify_volunteer_tool directamente — todo cruza por acá.
    """
    call_model: Callable
    perform: Callable


# ─── Hueco D: validación de argumentos en la frontera del proceso ───────────────
#
# _run_tool (bot.py) arma argv para subprocess.run -> aquarela_cli.py con args
# producidos por el LLM (no confiables). Como usa lista (no shell=True), no hay
# shell-injection; el riesgo real es FLAG injection: un valor como "--data" o
# "-rf" donde argparse del CLI espera un dato posicional/de valor puede ser
# leído como opción. Además deja cruzar datos con forma inválida (una "week"
# inventada, un volunteer_id no numérico) sin chequear.
#
# Mitigación en la frontera del proceso, NO dentro del engine (aquarela_cli.py
# sigue siendo el contrato mínimo de Hueco A: solo se cruza por subprocess).
# Allowlist por CAMPO (Grill Me Sesión 7): la semántica de cada valor depende
# del NOMBRE del argumento, que es consistente en las 12 tools que cruzan a la
# CLI. Un validador puro por semántica; el registro FIELD_VALIDATORS mapea cada
# nombre de argumento a su validador. Funciones puras (input -> str saneado |
# ValidationError), testeables sin subprocess real. No es un parser genérico:
# solo valida los campos que _run_tool ya conoce.


class ValidationError(ValueError):
    """Un argumento del LLM no pasó la validación de frontera de _run_tool."""


_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MODULES = frozenset({"breakfast", "day_rec", "night_rec"})
_MAX_NAME = 100


def _reject_leading_dash(field: str, value: str) -> None:
    """Guarda anti-flag fail-closed: un '-' inicial haría que argparse lea el
    valor como opción. Los guiones interiores ("Jean-Paul") sí se permiten."""
    if value.startswith("-"):
        raise ValidationError(f"{field}: value may not start with '-' (flag injection)")


def _v_date(field: str, value) -> str:
    """ISO YYYY-MM-DD, zero-padded y fecha de calendario real."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValidationError(f"{field}: expected date YYYY-MM-DD, got {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValidationError(f"{field}: not a real calendar date: {value!r}")
    return value


def _v_pos_int(field: str, value) -> str:
    """Entero positivo. Acepta int o string de solo dígitos; str.isdigit()
    rechaza signos, decimales, espacios y vacío. bool no es un id válido."""
    if isinstance(value, bool):
        raise ValidationError(f"{field}: expected a positive integer, got bool")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and value.isdigit():
        n = int(value)
    else:
        raise ValidationError(f"{field}: expected a positive integer, got {value!r}")
    if n <= 0:
        raise ValidationError(f"{field}: must be positive, got {n}")
    return str(n)


def _v_telegram_id(field: str, value) -> str:
    """ID de Telegram: string numérico positivo (o int). Sin signos ni espacios."""
    if isinstance(value, bool):
        raise ValidationError(f"{field}: expected a numeric telegram id, got bool")
    if isinstance(value, int):
        if value <= 0:
            raise ValidationError(f"{field}: must be positive, got {value}")
        return str(value)
    if isinstance(value, str) and value.isdigit():
        return value
    raise ValidationError(f"{field}: expected a numeric telegram id, got {value!r}")


def _v_module(field: str, value) -> str:
    if value not in _MODULES:
        raise ValidationError(f"{field}: expected one of {sorted(_MODULES)}, got {value!r}")
    return value


def _v_active_only(field: str, value) -> str:
    if value not in ("true", "false"):
        raise ValidationError(f"{field}: expected 'true' or 'false', got {value!r}")
    return value


def _v_json(field: str, value) -> str:
    """JSON válido. Guarda estricta de guion inicial (un '-' delante haría que
    argparse lo lea como flag) aunque sea JSON válido; los guiones interiores
    dentro del string JSON no se tocan."""
    if not isinstance(value, str):
        raise ValidationError(f"{field}: expected a JSON string, got {value!r}")
    _reject_leading_dash(field, value)
    try:
        json.loads(value)
    except (json.JSONDecodeError, ValueError):
        raise ValidationError(f"{field}: not valid JSON")
    return value


def _v_name(field: str, value) -> str:
    """Texto libre acotado. Guarda estricta de guion inicial; los guiones
    interiores ("Jean-Paul") se permiten."""
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field}: expected a non-empty name, got {value!r}")
    _reject_leading_dash(field, value)
    if len(value) > _MAX_NAME:
        raise ValidationError(f"{field}: name too long ({len(value)} > {_MAX_NAME})")
    return value


# Registro por nombre de argumento. La semántica de cada nombre es consistente
# en las 12 tools de _run_tool, así que un único validador por nombre alcanza.
FIELD_VALIDATORS = {
    "week":              _v_date,
    "date":              _v_date,
    "arrival":           _v_date,
    "departure":         _v_date,
    "volunteer_id":      _v_pos_int,
    "shift_id":          _v_pos_int,
    "stay_id":           _v_pos_int,
    "telegram_id":       _v_telegram_id,
    "admin_telegram_id": _v_telegram_id,
    "module":            _v_module,
    "active_only":       _v_active_only,
    "data":              _v_json,
    "name":              _v_name,
}


def sanitize_arg(field_name: str, value):
    """Sanea un único argumento por la semántica de su nombre. Devuelve el
    valor saneado (string, listo para argv) o levanta ValidationError."""
    validator = FIELD_VALIDATORS.get(field_name)
    if validator is None:
        raise ValidationError(f"no validator registered for field {field_name!r}")
    return validator(field_name, value)


def sanitize_tool_args(args: dict) -> dict:
    """Sanea, campo por campo, los argumentos que cruzarán a argv del CLI.

    Fail-closed: cualquier campo con validador registrado que no pase su forma
    esperada levanta ValidationError ANTES de que _run_tool arme el subprocess.
    Los campos SIN validador registrado (p.ej. 'confirmed', que cmd_map
    coerciona a la literal "true"/"false" y nunca pasa crudo a argv) se dejan
    intactos. No muta el dict recibido.
    """
    out = dict(args)
    for field, value in args.items():
        validator = FIELD_VALIDATORS.get(field)
        if validator is not None:
            out[field] = validator(field, value)
    return out
