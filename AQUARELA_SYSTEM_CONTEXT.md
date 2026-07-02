# AQUARELA — System Context

> **Propósito de este documento.** Contexto de alto nivel para que cualquier IA (o humano)
> entienda el sistema en segundos sin leer todo el código. Es la fuente de verdad conceptual;
> el código es la fuente de verdad de detalle. Si ambos discrepan, gana el código — y este
> documento debe corregirse.
>
> **Última auditoría:** Sesión de Auditoría (post-Sesión 9). Estado: 208 tests verdes.

---

## 1. Qué es Aquarela

Aquarela es el sistema de gestión de voluntarios del hostel **Aquarela do Leme**. Coordina
turnos semanales, preferencias, entrenamientos y notificaciones para una población pequeña
(decenas de voluntarios rotativos) gobernada por unos pocos admins (Moises, Marielle, Thibaut).

El sistema tiene **dos mitades que viven en repos/directorios distintos**:

| Componente | Ubicación | Rol |
|---|---|---|
| **Engine (`aquarela`)** | `/home/moises/aquarela/` | Dueño del estado. DB SQLite, lógica de scheduling, CLI. |
| **Atlas bot (`atlas-bot`)** | `/home/moises/atlas-bot/` | Interfaz conversacional. Bot de Telegram + loop agéntico con un LLM (Grok vía OpenRouter). |

Atlas es la cara conversacional; el engine es el cerebro transaccional. **Nunca se mezclan
en el mismo proceso.**

---

## 2. Regla arquitectónica maestra: `aquarela.db` es el dueño

Esta es la regla que ordena todo lo demás (formalizada como **Hueco A**):

- **El engine (`aquarela.db` + `aquarela_cli.py` + `db.py`) es el ÚNICO dueño del estado.**
  Es el único que **escribe**. Aplica WAL, `foreign_keys=ON`, y primitivas de *atomic-claim*
  (`UPDATE ... WHERE ... IS NULL`) para serializar escritores en ráfagas (dos admins aprobando
  el mismo draft, voluntarios compitiendo por un turno pagado, etc.).
- **Atlas bot NO escribe la DB directamente. Jamás.** Toda mutación cruza por un único borde:
  `subprocess.run([engine_python, aquarela_cli.py, <subcomando>, ...])`. Atlas no conoce el
  esquema; solo conoce la superficie del CLI (los subcomandos y sus flags).
- **Atlas bot SÍ lee la DB directamente** (solo lectura) para 3 cosas puntuales de control de
  acceso: resolver `telegram_id → (admin|volunteer)`, cargar la lista de admins, y el idioma
  del voluntario. Son lecturas de una fila, sin transacción. *(Deuda menor: estas lecturas
  abren conexiones crudas con `sqlite3.connect` en vez de reusar `db.get_connection`; ver
  ROADMAP.)*

**Consecuencia de diseño:** el CLI del engine es un *contrato mínimo y estable*. Mientras
Atlas solo cruce por ahí, las dos mitades pueden evolucionar por separado. Cambiar el esquema
de la DB no rompe a Atlas si el contrato del CLI se mantiene.

---

## 3. Data Flow — el camino de un mensaje

```
Voluntario/Admin (Telegram)
        │  texto plano
        ▼
handle_message (bot.py)  ──►  _check_access(telegram_id)   [LECTURA directa a aquarela.db]
        │                         └─ None → "no estás registrado", fin (fail-closed)
        │                         └─ ("admin", role) | ("volunteer", vol_id)
        ▼
async with _store.lock(user_id):        ← serializa TODO el turno por-usuario (Hueco G)
        │
        ├─ _store.append_history(user, text)     [estado volátil en memoria]
        │
        ▼
_call_grok(...)  → loop agéntico (máx 5 iteraciones)
        │
        │   for iteration in range(_MAX_TOOL_ITER):
        │       response = deps.call_model(messages)     ← LLM (OpenRouter/Grok)  [SÍNCRONO ⚠]
        │       if no tool_calls: return texto
        │       for tc in tool_calls:
        │           pin_identity(...)        ← ata identidad self-service a la sesión autenticada
        │           authorize(...)           ← gate fail-closed por rol (pura)
        │           sanitize_tool_args(...)  ← validación por-campo + anti-flag (pura)
        │           deps.perform(tool, args) ← efecto real:
        │                                        · notify_* → Telegram API
        │                                        · resto    → subprocess → aquarela_cli.py [SÍNCRONO ⚠]
        │
        ├─ (caso especial) approve_draft OK → plan_post_approve_notifications → auto-notifica voluntarios
        │
        ▼
_store.append_history(user, reply)
reply_text(reply)  → de vuelta al usuario en Telegram
```

**Segundo camino — confirmación de training (botones inline):** `handle_callback_query`
→ `handle_training_callback` (máquina de estados pura, doble pantalla Sí/No con
"¿estás seguro?") → CLI `confirm-training` → notifica voluntario y, si es "no", a los admins.
Este camino **no pasa por el LLM**; es determinista.

---

## 4. Modelo de concurrencia (estado actual)

- **Un solo proceso, un solo event loop asyncio** (python-telegram-bot en long-polling).
- **Serialización por-usuario:** `ConversationStore.lock(user_id)` es un `asyncio.Lock` por
  `telegram_id`. Garantiza que una ráfaga de mensajes del *mismo* usuario no se entrelace y
  corrompa el orden del historial enviado al LLM. **Correctitud: sólida.**
- **⚠ Punto débil operativo — I/O bloqueante.** El loop es `async`, pero sus tres llamadas
  costosas son **síncronas** y corren dentro del event loop:
  1. `deps.call_model(...)` — el cliente OpenAI/OpenRouter es síncrono (round-trip de varios
     segundos).
  2. `_run_tool → subprocess.run(..., timeout=30)` — bloqueante hasta 30 s.
  3. Las lecturas de control de acceso (`sqlite3` síncrono).

  **Efecto:** aunque los locks son por-usuario, el *event loop es único y global*. Mientras
  Atlas espera una respuesta del LLM de Alice, **el bot entero queda congelado para Bob,
  Carol y todos los demás**. En el caso de uso planificado (ráfagas transaccionales de
  voluntarios al abrirse turnos) esto degrada la latencia de forma severa: los turnos se
  procesan efectivamente en serie global, no en paralelo. Esta es la deuda técnica #1 y el
  foco de la Sesión 10 (ver `ROADMAP_HUECOS.md`).

- **Estado conversacional volátil por decisión consciente (Hueco G).** El historial vive solo
  en memoria (`ConversationStore`: cap de longitud por-usuario vía `deque(maxlen)`, LRU sobre
  usuarios vía `OrderedDict`, y expiración TTL perezosa). Se pierde al reiniciar. Es scratch
  efímero del loop agéntico, **no** un sistema de registro — eso vive en `aquarela.db`.

---

## 5. Postura de seguridad (los "Huecos" A–G ya cerrados)

La seguridad es donde el sistema es más maduro. Todo el borde LLM asume que **el modelo es
un adversario potencial** (prompt injection) y que **el LLM nunca es autoridad sobre identidad
ni permisos**. Patrón transversal: *Functional Core / Imperative Shell* — la política es una
función pura y testeable (`call_grok_core.py`, `training_callbacks.py`, `log_policy.py`,
`conversation_store.py`); el shell imperativo (`bot.py`) solo la aplica y toca el mundo real.

| Hueco | Qué cierra | Mecanismo |
|---|---|---|
| **A** | Propiedad del estado | Atlas nunca escribe la DB; todo cruza por el CLI (contrato mínimo). |
| **B** | `_call_grok` monolítico y frágil | Extraído a núcleo puro + `Deps` (frontera de efectos inyectable). B.5 —colapsar el loop en intérprete puro— queda diferido. |
| **C** | Spam a admins / fail-open | `authorize()` fail-closed: tool desconocido → denegado; `notify_*` son admin-only. |
| **D** | Flag injection en `subprocess` | `sanitize_tool_args`: allowlist por-campo (fecha/int/telegram_id/módulo/json/nombre) + guarda anti-`-` inicial, **antes** de armar argv. |
| **E** | Token del bot en journald | `httpx`/`httpcore` subidos a WARNING: la URL de `getUpdates` con el token deja de loguearse. |
| **F** | Un voluntario espiando a otro | `pin_identity`: `show_volunteer` de un voluntario se ata a su propio `telegram_id` (bind, no check → consulta insegura irrepresentable). |
| **G** | `_history` global sin límite | `ConversationStore` acotado (longitud + LRU + TTL) + lock de turno por-usuario. |

**Principios que se repiten:**
- **Fail-closed / safe-by-construction:** lo inseguro se vuelve *estructuralmente
  irrepresentable*, no simplemente "chequeado".
- **Pin, don't trust:** los campos de identidad (`volunteer_id`, `telegram_id`) se pinean a la
  sesión autenticada; se descarta lo que el LLM haya inyectado.
- **Validación en el borde del proceso**, no dentro del engine (respeta el contrato de Hueco A).

---

## 6. Mapa de archivos (atlas-bot)

| Archivo | Responsabilidad | Naturaleza |
|---|---|---|
| `bot.py` | Shell imperativo: handlers de Telegram, definición de TOOLS, `_run_tool` (borde subprocess), control de acceso, driver del loop agéntico. | Impuro (I/O). |
| `call_grok_core.py` | Núcleo puro del loop: `authorize`, `pin_identity`, parseo/ensamblado de mensajes, `plan_post_approve_notifications`, `sanitize_tool_args`, `Deps`. | Puro. |
| `conversation_store.py` | Estado conversacional volátil acotado (módulo profundo). | Puro salvo el reloj. |
| `training_callbacks.py` | Máquina de estados pura de confirmación de training (botones inline). | Puro (efectos inyectados). |
| `log_policy.py` | Política de higiene de secretos en logs. | Puro + shell delgado. |
| `tests/` | 208 tests. Caracterización + seguridad (access control, disclosure, validación, store, logs). | — |

**Config y despliegue:** `.env` (token + API key, homegrown parser), `atlas-bot.service`
(systemd, `Restart=always`), `requirements.txt` pineado (runtime: `openai`, `python-telegram-bot`).

---

## 7. Invariantes que NO deben romperse (para futuras sesiones)

1. **Atlas nunca escribe `aquarela.db` directamente.** Toda mutación por el CLL.
2. **El LLM nunca decide identidad ni permisos.** `pin_identity` + `authorize` corren siempre,
   antes de cualquier efecto.
3. **Fail-closed por defecto.** Un tool/rol/valor desconocido se deniega, no se deja pasar.
4. **Núcleo puro sin I/O.** La lógica nueva se prueba en RED antes de existir (TDD estricto,
   ver `DEVELOPMENT_PHILOSOPHY.md`), y vive en un módulo puro si es lógica de decisión.
5. **Los secretos no se loguean.** Cualquier logger nuevo de terceros que renderice URLs se
   audita contra `log_policy`.
