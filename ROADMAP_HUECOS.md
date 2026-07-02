# ROADMAP — Huecos (backlog priorizado)

> **Cómo se usa este documento (SOP).**
> 1. El usuario inicia una sesión diciendo: **"Inicia la Sesión X según el ROADMAP_HUECOS.md"**.
> 2. La IA lee `AQUARELA_SYSTEM_CONTEXT.md` + este archivo, y ejecuta bajo **TDD estricto
>    (RED → GREEN → REFACTOR)** en un **worktree**, respetando `DEVELOPMENT_PHILOSOPHY.md`.
> 3. Al hacer merge a `master`, la **última tarea obligatoria** es actualizar este archivo:
>    marcar la sesión como ✅ completada y ajustar el contexto pendiente (y, si cambió una regla
>    arquitectónica, sincronizar `AQUARELA_SYSTEM_CONTEXT.md`).
>
> **Convención de estado:** ✅ Completado · 🔜 Siguiente · ⏳ Pendiente · 💤 Diferido (a demanda)

---

## Historial — Huecos cerrados (Sesiones 1–9)

| Hueco | Sesión | Estado | Resumen |
|---|---|---|---|
| **A** — Propiedad del estado | base | ✅ | `aquarela.db` es el dueño; Atlas solo cruza por el CLI. Contrato mínimo. |
| **B** — `_call_grok` a núcleo puro + `Deps` | 5–6 | ✅ | Strangler-fig B.0–B.4. *(B.5 intérprete puro → 💤 diferido, ver abajo.)* |
| **C** — Spam a admins / fail-open | 6–7 | ✅ | `authorize()` fail-closed; `notify_*` admin-only. |
| **D** — Flag injection en subprocess | 7 | ✅ | `sanitize_tool_args`: allowlist por-campo + anti-flag. |
| **E** — Token del bot en journald | 7 | ✅ | `log_policy`: `httpx`/`httpcore` a WARNING. |
| **F** — Disclosure entre voluntarios | 8 | ✅ | `pin_identity` ata `show_volunteer` al propio `telegram_id`. |
| **G** — `_history` global sin límite | 9 | ✅ | `ConversationStore` acotado (longitud + LRU + TTL) + lock de turno. |
| **H** — I/O bloqueante en el event loop | 10 | ✅ | `AsyncOpenAI` + `engine_runner.py` async (semáforo 4, kill+reap) + `concurrent_updates(32)` + deadline de turno 120s. Detalle abajo. |

**Estado global tras Sesión 10:** seguridad madura (Huecos A–G) y ahora también el modelo de
concurrencia es real (Hueco H): un turno lento de un usuario ya no congela el bot para los
demás. 213 tests verdes. La deuda restante es de **resiliencia operativa** (costo, timeouts de
red, arranque, observabilidad) — el backlog de abajo la ataca en orden de riesgo para
producción.

---

## Backlog pendiente — Huecos I en adelante

### ✅ Sesión 10 — Hueco H: I/O bloqueante en el event loop — COMPLETADA

**Problema (diagnosticado en la auditoría).** `_call_grok` era `async` pero sus tres llamadas
costosas eran síncronas y bloqueaban el *único* event loop: (1) `deps.call_model` (round-trip
al LLM, varios segundos), (2) `_run_tool → subprocess.run(timeout=30)`, (3) lecturas `sqlite3`
de control de acceso. Además, python-telegram-bot procesa updates **secuencialmente por
defecto** (`max_concurrent_updates=1`) — sin tocar eso, ninguna migración async habría
cambiado nada observable. Los locks por-usuario daban correctitud, pero un turno lento de un
usuario congelaba el bot para todos, contradiciendo el caso de uso de ráfagas transaccionales.

**Solución implementada (TDD estricto, RED 02d1bc0 → GREEN ce7acc2 → sin REFACTOR necesario).**
- `engine_runner.py` (nuevo, módulo profundo): `EngineRunner(max_concurrent=4).run(argv,
  timeout)` — subprocess async nativo (`asyncio.create_subprocess_exec`, cero hilos), kill+reap
  en timeout y ante cancelación (sin zombies bajo `Restart=always`), y un `asyncio.Semaphore(4)`
  que acota los procesos engine en vuelo. El loop bloqueante serializaba el CLI *por accidente*;
  al liberar el loop aparecía una race nueva (escritores WAL concurrentes sobre `aquarela.db`,
  busy-timeout default de sqlite3 = 5s) — el semáforo la mitiga del lado de Atlas, sin tocar el
  engine (Hueco A intacto).
- `bot.py`: cliente `AsyncOpenAI(timeout=45.0, max_retries=0)` (el default del SDK era
  `read=600s`; reintentos deliberados quedan para la Sesión 12/Hueco J); `Deps.call_model`
  awaitable y `await` en el driver; `_run_tool` async vía `_ENGINE.run`; lecturas sqlite
  (`_check_access`, `_load_admin_ids`, idioma del voluntario) a `asyncio.to_thread`; deadline de
  turno `asyncio.timeout(_TURN_DEADLINE=120)` con degradación amable (`_TIMEOUT_REPLY`) y el
  lock por-usuario siempre liberado (`async with` de Hueco G intacto); el core puro de training
  (síncrono, con su propio subprocess de hasta 30s) corre en `asyncio.to_thread`, puenteando sus
  llamadas al engine de vuelta al loop vía `run_coroutine_threadsafe` para compartir el mismo
  semáforo del runner; `.concurrent_updates(32)` explícito en el builder de PTB.
- `call_grok_core.py`: solo el docstring de `Deps` (núcleo puro sin cambios, como se esperaba).

**Resultado.** 213 tests verdes (208 preexistentes sin regresión + 5 nuevos). Test estrella
(`test_slow_model_call_for_one_user_does_not_block_another`) demuestra que el turno colgado de
un usuario no bloquea el flujo agéntico completo de otro. Smoke test de import real confirmó el
wiring de producción (`AsyncOpenAI` timeout=45, max_retries=0, deadline=120s, corutinas).

**Hallazgo lateral (alimenta la Sesión 13 / Hueco K).** El smoke test de import real (fuera de
los stubs de `conftest.py`) tropezó primero con dos fragilidades de arranque: `training_callbacks.py`
resuelve `training_texts` con un `sys.path.insert` relativo a `aquarela` que rompe si el CWD o
la ubicación del repo cambian, y `_load_env()`/`_SOUL_PATH` asumen rutas absolutas fijas. Se
resolvió localmente con un symlink (no comiteado); confirma en vivo el diagnóstico ya anotado en
Hueco K — ver ese apartado.

---

### 🔜 Sesión 11 — Hueco I: Rate limiting y control de costo/presupuesto

**Problema.** Cada mensaje dispara hasta `_MAX_TOOL_ITER=5` llamadas pagas al LLM. No hay
límite por-usuario ni presupuesto global. Un usuario (o un registrado malicioso, o un bucle de
re-entrega de Telegram) puede disparar costo sin techo. Tampoco hay dedup de mensajes
repetidos en ráfaga.

**Objetivo.** Techo de gasto y de abuso: límite de turnos/tokens por-usuario por ventana de
tiempo, y un *circuit breaker* global. Degradar con un mensaje amable ("estoy saturado, prueba
en un momento"), nunca con un crash.

**Enfoque candidato.** Función pura de política de rate-limit (token bucket / ventana
deslizante por-usuario) en un módulo nuevo, aplicada por el shell antes de entrar al loop.
Contador global de llamadas al modelo con corte. Reusar el patrón Core/Shell.

**Depende de:** Sesión 10 ✅ (el modelo de concurrencia real ya define dónde viven los
contadores: por-usuario, en un módulo nuevo, sin bloquear el loop).

---

### ⏳ Sesión 12 — Hueco J: Resiliencia de la llamada al modelo (timeout + retry + degradación)

**Problema.** Desde Sesión 10, `_call_model` ya tiene timeout (45s) pero **cero reintentos**
(`max_retries=0`, deliberado — fail-fast mientras no exista una política de retry pensada). Si
OpenRouter da un 5xx transitorio, el turno falla directo en vez de reintentar; Telegram puede
además re-entregar el update y duplicar trabajo.

**Objetivo.** Reintentos acotados con backoff para errores transitorios (5xx / timeout), y una
degradación limpia al usuario cuando se agota. Idempotencia razonable frente a re-entregas.

**Depende de:** Sesión 10 ✅ (ya provee el timeout de 45s y el deadline de turno de 120s sobre
los que esta política de retry debe presupuestar sus reintentos sin violarlos).

---

### ⏳ Sesión 13 — Hueco K: Robustez de arranque y configuración

**Problema.** Rutas absolutas hardcodeadas (`SOUL`, `_DB_PATH`, `_ENGINE_PY`, `_ENGINE_CLI`);
el SOUL se lee en import-time (si `aquarela` se mueve, el import revienta). Parser de `.env`
casero. No hay validación de precondiciones al boot ni healthcheck.

**Confirmado en vivo (Sesión 10).** El smoke test de import real de `bot.py` (fuera de los
stubs de `conftest.py`) tropezó primero con exactamente esta fragilidad, antes de poder
verificar nada del wiring async: `training_callbacks.py` resuelve el módulo sibling
`training_texts` de `aquarela` con un `sys.path.insert(0, ...)` relativo a `__file__` que
depende de la ubicación relativa entre los dos repos; y `_load_env()`/`_SOUL_PATH` fallan si el
CWD o la ruta del repo no coinciden con lo hardcodeado. Se resolvió localmente con un symlink
de `.env` (no comiteado) solo para poder correr el smoke test — la fragilidad real permanece
intacta en producción y es exactamente el objetivo de esta sesión.

**Objetivo.** Fallar rápido y claro al arrancar si falta una precondición (env var, DB, CLI,
SOUL), con un mensaje accionable. Centralizar configuración. Opcional: comando `/status` real
que reporte salud (DB accesible, engine responde).

---

### ⏳ Sesión 14 — Hueco L: Observabilidad y alertas de fallo sistémico

**Problema.** Solo logs a journald; sin métricas ni alertas. Un fallo sistémico (DB caída,
OpenRouter caído, engine roto) es invisible hasta que un humano lo nota. Los `except Exception`
genéricos loguean pero no escalan.

**Objetivo.** Alerta a admin ante fallo sistémico recurrente (no ante el error puntual de un
turno). Logs estructurados mínimos para depurar ráfagas. Contadores básicos (turnos, tools,
denegaciones, errores) exportables.

---

### 💤 Diferidos (a demanda, sin sesión asignada)

- **Hueco B.5 — Colapsar el loop de `_call_grok` en un intérprete puro.** El driver ya es un
  shell delgado sobre `Deps`; llevarlo a intérprete puro es refactor de pureza, no de riesgo.
  Solo si se pide explícitamente.
- **Lecturas de control de acceso reusando `db.get_connection`.** Deuda menor: hoy `bot.py`
  abre conexiones crudas `sqlite3.connect` en vez del helper del engine. Funcionalmente
  correcto bajo WAL; unificar es limpieza, no urgencia.
- **Persistencia del historial conversacional.** Hoy volátil por diseño (Hueco G). Solo si el
  negocio pide continuidad tras reinicio; hoy es un no-objetivo consciente.

---

## Notas de priorización

El orden **10 ✅ → 11 → 12 → 13 → 14** sigue el riesgo para producción, no la facilidad:

1. **I/O bloqueante primero (cerrado en Sesión 10)** porque invalidaba la premisa de
   concurrencia de todo lo demás: sin resolverlo, rate limiting y timeouts habrían operado
   sobre un loop que igual se congelaba en serie.
2. **Rate limiting y timeouts** protegen costo y disponibilidad ahora que la concurrencia es
   real.
3. **Arranque y observabilidad** al final: importan para operar con confianza, pero no cambian
   la corrección del camino feliz ni la postura de seguridad (que ya es sólida).

**No sobre-ingenierizar.** El split Core/Shell y el `ConversationStore` (LRU+TTL+lock) son
**proporcionados** al dominio (borde LLM sensible a seguridad), no excesivos. La regla para las
próximas sesiones: cada Hueco nuevo debe justificar su complejidad contra un riesgo concreto de
producción, con un test en RED que lo demuestre antes de escribir código.
