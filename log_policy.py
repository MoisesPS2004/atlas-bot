"""
log_policy.py — Higiene de secretos en logs (Hueco E).

Funcional Core / Imperative Shell, mismo patrón que call_grok_core y
training_callbacks: la política es una función pura; el shell (bot.py) la aplica.

La fuga (confirmada en código): el bot hace long-polling de getUpdates vía PTB
-> httpx. httpx loguea cada request a INFO por el logger "httpx":
`HTTP Request: {method} {request.url} "..."` (httpx/_client.py:1025-1028).
Para Telegram, request.url es `https://api.telegram.org/bot<TOKEN>/getUpdates`,
así que con el root en INFO (bot.py: logging.basicConfig(level=logging.INFO))
el token del bot cae en journald en texto plano.

Mitigación en la política de logging, NO en el token (rotarlo trata el síntoma:
el siguiente poll re-filtra el token nuevo). Subimos los loggers de terceros que
emiten la URL de la request (httpx y, por defensa ante un futuro DEBUG, httpcore)
a WARNING, silenciando la línea INFO que carga el token, SIN tocar el root ni
nuestro logger operativo (que siguen en INFO para no perder "Atlas bot started",
resultados de tools y denegaciones).
"""
from __future__ import annotations

import logging

# Loggers de terceros que renderizan la URL de la request (con el token del bot
# en el path) en sus logs de nivel INFO. httpcore no emite la URL a INFO hoy,
# pero se incluye como defensa ante un cambio futuro del root a DEBUG.
_SECRET_LEAKING_LOGGERS = ("httpx", "httpcore")


def secret_safe_log_levels() -> dict[str, int]:
    """Política pura: nivel mínimo por logger de tercero que filtraría el token
    del bot. WARNING los silencia a INFO sin tocar el root ni nuestro logger
    operativo. Retorna un mapping fresco en cada llamada (nadie puede envenenar
    la fuente mutando el resultado)."""
    return {name: logging.WARNING for name in _SECRET_LEAKING_LOGGERS}


def apply_secret_safe_logging() -> None:
    """Shell imperativo: aplica secret_safe_log_levels() al logging global.
    Idempotente. Debe llamarse después de logging.basicConfig() para que el
    setLevel por-logger gane sobre el nivel del root."""
    for name, level in secret_safe_log_levels().items():
        logging.getLogger(name).setLevel(level)
