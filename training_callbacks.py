"""
training_callbacks.py — Lógica pura de la máquina de estados de confirmación de training.

Sin imports de telegram. Sin IO. Testeable de forma aislada.
El CallbackQueryHandler en bot.py llama handle_training_callback() y despacha
los mensajes según el dict retornado.

Estados del callback_data:
  training_confirm:{vid}:{module}:yes      → primera pantalla Sí → pedir confirmación
  training_confirm:{vid}:{module}:no       → primera pantalla No → pedir confirmación
  training_confirm:{vid}:{module}:yes_sure → confirmó Sí → CLI + avisar voluntario
  training_confirm:{vid}:{module}:no_sure  → confirmó No → CLI + avisar voluntario + admins
  training_confirm:{vid}:{module}:back     → "no, en realidad no" → re-muestra P1 sin DB write
"""

from __future__ import annotations

import json
import logging
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).parent.parent / "aquarela"))
from training_texts import _MODULE_LABEL, _QUESTION, _BTN_YES, _BTN_NO

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Textos multilingüe (propios de training_callbacks)
# ---------------------------------------------------------------------------

_CONFIRM_LABEL: dict[str, str] = {
    "en": "✅ Yes, I confirm",
    "es": "✅ Sí, confirmo",
    "pt": "✅ Sim, confirmo",
    "fr": "✅ Oui, je confirme",
}

_BACK_LABEL: dict[str, str] = {
    "en": "↩ No, go back",
    "es": "↩ No, en realidad no",
    "pt": "↩ Não, voltar",
    "fr": "↩ Non, revenir",
}

_SURE_TEXT: dict[str, str] = {
    "en": "Are you sure?",
    "es": "¿Estás seguro/a?",
    "pt": "Tem certeza?",
    "fr": "Tu es sûr(e)?",
}

_YES_COMPLETED: dict[str, str] = {
    "en": "✅ Great! Your training has been recorded as completed. You're now eligible for those shifts.",
    "es": "✅ ¡Perfecto! Tu entrenamiento quedó registrado como completado. Ya estás habilitado/a para esos turnos.",
    "pt": "✅ Ótimo! Seu treinamento foi registrado como concluído. Agora você está habilitado/a para esses turnos.",
    "fr": "✅ Super! Ta formation a été enregistrée comme complétée. Tu es maintenant éligible pour ces shifts.",
}

_NO_VOLUNTEER: dict[str, str] = {
    "en": "Got it. Please speak with the manager so they can assign you a new training session as soon as possible. You'll be asked again daily until your training is confirmed. ✊",
    "es": "Entendido. Por favor dirígete con el manager para que te asigne un turno de entrenamiento lo antes posible. Diariamente se te volverá a preguntar hasta que tu entrenamiento sea confirmado. ✊",
    "pt": "Entendido. Por favor fale com o gerente para que ele te atribua uma nova sessão de treinamento o quanto antes. Você será perguntado/a diariamente até que seu treinamento seja confirmado. ✊",
    "fr": "Compris. Parle au manager pour qu'il te planifie une nouvelle session de formation dès que possible. Tu seras relancé(e) chaque jour jusqu'à confirmation. ✊",
}

_ERROR_TEXT: dict[str, str] = {
    "en": "Something went wrong. Please try again or contact the manager.",
    "es": "Algo salió mal. Intenta de nuevo o contacta al manager.",
    "pt": "Algo deu errado. Tente novamente ou fale com o gerente.",
    "fr": "Quelque chose s'est mal passé. Réessaie ou contacte le manager.",
}


def _admin_message(vid: int, module: str) -> str:
    module_dict = _MODULE_LABEL.get(module, {})
    label = module_dict.get("en", module)
    return (
        f"⚠️ Volunteer (id={vid}) did not complete their {label} training. "
        f"Please assign them a new session as soon as possible."
    )


# ---------------------------------------------------------------------------
# Función pura principal
# ---------------------------------------------------------------------------

def handle_training_callback(
    callback_data: str,
    acting_telegram_id: str,
    run_tool_fn,
    load_admin_ids_fn,
    lang: str = "en",
) -> dict:
    """
    Procesa un callback de confirmación de training.

    Parámetros
    ----------
    callback_data      : string del botón pulsado
    acting_telegram_id : telegram_id del voluntario que pulsó
    run_tool_fn        : callable(name, args) → JSON string  (inyectado, mockeable)
    load_admin_ids_fn  : callable() → set[str]               (inyectado, mockeable)
    lang               : código de idioma del voluntario (default 'en')

    Retorna
    -------
    dict con las claves:
      action            : 'confirm_prompt' | 'completed' | 'pending' | 'error'
      reply_text        : texto a enviar al voluntario
      confirm_yes_data  : (solo en confirm_prompt) callback_data del botón primario
      confirm_yes_label : (solo en confirm_prompt) etiqueta del botón primario
      confirm_no_data   : (solo en confirm_prompt) callback_data del botón secundario
      confirm_no_label  : (solo en confirm_prompt) etiqueta del botón secundario
      notify_admins     : bool — True si hay que notificar a admins
      admin_message     : str  — mensaje para los admins (vacío si notify_admins=False)
    """
    lang = lang if lang in _YES_COMPLETED else "en"

    # --- Parseo del callback_data ---
    try:
        parts = callback_data.split(":")
        if len(parts) != 4:
            raise ValueError(f"expected 4 parts, got {len(parts)}")
        _, vid_str, module, state = parts
        vid = int(vid_str)
    except Exception as exc:
        logger.warning(f"Malformed callback_data {callback_data!r}: {exc}")
        return {
            "action":        "error",
            "reply_text":    _ERROR_TEXT[lang],
            "notify_admins": False,
            "admin_message": "",
        }

    # --- Primera pantalla: pedir confirmación ---
    if state in ("yes", "no"):
        sure_state = "yes_sure" if state == "yes" else "no_sure"
        return {
            "action":            "confirm_prompt",
            "reply_text":        _SURE_TEXT[lang],
            "confirm_yes_data":  f"training_confirm:{vid}:{module}:{sure_state}",
            "confirm_yes_label": _CONFIRM_LABEL.get(lang, _CONFIRM_LABEL["en"]),
            "confirm_no_data":   f"training_confirm:{vid}:{module}:back",
            "confirm_no_label":  _BACK_LABEL.get(lang, _BACK_LABEL["en"]),
            "notify_admins":     False,
            "admin_message":     "",
        }

    # --- Volver a P1 sin escribir DB ---
    if state == "back":
        module_dict = _MODULE_LABEL.get(module, _MODULE_LABEL["night_rec"])
        label  = module_dict.get(lang, module_dict["en"])
        q_tmpl = _QUESTION.get(lang, _QUESTION["en"])
        return {
            "action":            "confirm_prompt",
            "reply_text":        q_tmpl.format(module=label),
            "confirm_yes_data":  f"training_confirm:{vid}:{module}:yes",
            "confirm_yes_label": _BTN_YES.get(lang, _BTN_YES["en"]),
            "confirm_no_data":   f"training_confirm:{vid}:{module}:no",
            "confirm_no_label":  _BTN_NO.get(lang, _BTN_NO["en"]),
            "notify_admins":     False,
            "admin_message":     "",
        }

    # --- Segunda pantalla: confirmó Sí ---
    if state == "yes_sure":
        try:
            result = json.loads(run_tool_fn(
                "confirm_training",
                {"volunteer_id": vid, "module": module, "confirmed": True},
            ))
            if not result.get("ok"):
                raise ValueError(result.get("error", "CLI error"))
        except Exception as exc:
            logger.error(f"confirm_training yes_sure failed: {exc}")
            return {
                "action":        "error",
                "reply_text":    _ERROR_TEXT[lang],
                "notify_admins": False,
                "admin_message": "",
            }
        return {
            "action":        "completed",
            "reply_text":    _YES_COMPLETED[lang],
            "notify_admins": False,
            "admin_message": "",
        }

    # --- Segunda pantalla: confirmó No ---
    if state == "no_sure":
        try:
            result = json.loads(run_tool_fn(
                "confirm_training",
                {"volunteer_id": vid, "module": module, "confirmed": False},
            ))
            if not result.get("ok"):
                raise ValueError(result.get("error", "CLI error"))
        except Exception as exc:
            logger.error(f"confirm_training no_sure failed: {exc}")
            return {
                "action":        "error",
                "reply_text":    _ERROR_TEXT[lang],
                "notify_admins": False,
                "admin_message": "",
            }
        return {
            "action":        "pending",
            "reply_text":    _NO_VOLUNTEER[lang],
            "notify_admins": True,
            "admin_message": _admin_message(vid, module),
        }

    # --- Estado desconocido ---
    logger.warning(f"Unknown state {state!r} in callback_data {callback_data!r}")
    return {
        "action":        "error",
        "reply_text":    _ERROR_TEXT[lang],
        "notify_admins": False,
        "admin_message": "",
    }
