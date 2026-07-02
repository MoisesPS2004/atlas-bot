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

**Estado global tras Sesión 9:** seguridad madura y bien testeada (208 tests verdes). La
deuda restante es **operativa/de resiliencia**, no de seguridad. El backlog de abajo la ataca
en orden de riesgo para producción.

---

## Backlog pendiente — Huecos H en adelante

### 🔜 Sesión 10 — Hueco H: I/O bloqueante en el event loop **(CRÍTICO)**

**Problema.** `_call_grok` es `async` pero sus tres llamadas costosas son síncronas y bloquean
el *único* event loop: (1) `deps.call_model` (round-trip al LLM, varios segundos),
(2) `_run_tool → subprocess.run(timeout=30)`, (3) lecturas `sqlite3` de control de acceso.
Los locks por-usuario dan correctitud, pero un turno lento de un usuario **congela el bot para
todos**. Contradice directamente el caso de uso de *ráfagas transaccionales de voluntarios*.

**Objetivo.** Que una operación lenta de un usuario no bloquee a los demás. El paralelismo
entre usuarios distintos debe ser real; la serialización sigue siendo por-usuario (no romper
Hueco G).

**Enfoque candidato (a decidir en fase Grill Me).**
- Mover las llamadas bloqueantes fuera del loop: `asyncio.to_thread(...)` / `run_in_executor`
  para `subprocess` y para las lecturas sqlite; o migrar a subprocess asíncrono
  (`asyncio.create_subprocess_exec`).
- Para el LLM: usar el cliente **async** de OpenAI (`AsyncOpenAI`) o envolver la llamada
  síncrona en un executor. Preferible el cliente async (una sola forma de I/O).
- `Deps.call_model` pasa a ser awaitable; el driver hace `await deps.call_model(...)`. El
  núcleo puro no cambia (sigue sin I/O); solo cambia la firma de la frontera de efectos.

**Riesgos / notas.** No introducir escrituras concurrentes a la DB desde Atlas (Hueco A
intacto — el engine sigue siendo el único escritor). Verificar que el lock por-usuario se
mantiene sobre `await` (ya lo hace). Añadir tests de concurrencia: dos usuarios, uno lento,
el otro no espera.

**Criterio de aceptación.** Test que demuestre que un `call_model` lento del usuario A no
retrasa la respuesta al usuario B. Suite completa verde.

---

### ⏳ Sesión 11 — Hueco I: Rate limiting y control de costo/presupuesto

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

**Depende de:** Sesión 10 (el modelo de concurrencia define dónde viven los contadores).

---

### ⏳ Sesión 12 — Hueco J: Resiliencia de la llamada al modelo (timeout + retry + degradación)

**Problema.** `_call_model` no tiene timeout ni reintentos. Si OpenRouter cuelga, el turno
cuelga **sosteniendo el lock del usuario** y (tras Sesión 10) ocupando un slot del executor.
Telegram puede re-entregar el update y duplicar trabajo.

**Objetivo.** Timeout explícito por llamada al modelo, reintentos acotados con backoff para
errores transitorios (5xx / timeout), y una degradación limpia al usuario cuando se agota.
Idempotencia razonable frente a re-entregas.

**Depende de:** Sesión 10.

---

### ⏳ Sesión 13 — Hueco K: Robustez de arranque y configuración

**Problema.** Rutas absolutas hardcodeadas (`SOUL`, `_DB_PATH`, `_ENGINE_PY`, `_ENGINE_CLI`);
el SOUL se lee en import-time (si `aquarela` se mueve, el import revienta). Parser de `.env`
casero. No hay validación de precondiciones al boot ni healthcheck.

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

El orden **10 → 11 → 12 → 13 → 14** sigue el riesgo para producción, no la facilidad:

1. **I/O bloqueante primero** porque invalida la premisa de concurrencia de todo lo demás:
   sin resolverlo, rate limiting y timeouts operan sobre un loop que igual se congela en serie.
2. **Rate limiting y timeouts** protegen costo y disponibilidad una vez que la concurrencia es
   real.
3. **Arranque y observabilidad** al final: importan para operar con confianza, pero no cambian
   la corrección del camino feliz ni la postura de seguridad (que ya es sólida).

**No sobre-ingenierizar.** El split Core/Shell y el `ConversationStore` (LRU+TTL+lock) son
**proporcionados** al dominio (borde LLM sensible a seguridad), no excesivos. La regla para las
próximas sesiones: cada Hueco nuevo debe justificar su complejidad contra un riesgo concreto de
producción, con un test en RED que lo demuestre antes de escribir código.
