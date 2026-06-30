# Development Philosophy

## Regla 1 — Fase de Alineación ("GRILL ME")

Antes de proponer código o soluciones, debes entrevistarme con preguntas críticas sobre las reglas de negocio, **una a la vez**, ofreciendo una "Respuesta Recomendada" basada en buenas prácticas. No avanzaremos hasta lograr un Shared Design Concept.

## Regla 2 — Módulos Profundos y Vertical Slices

Prohibido programar en capas horizontales aisladas. Cada tarea se divide en **Vertical Slices** que cruzan desde la DB hasta la interfaz/bot. Los componentes deben exponer interfaces simples (**módulos profundos**).

## Regla 3 — TDD Estricto

Flujo **RED → GREEN → REFACTOR**. Nunca escribir código de producción sin un test previo en RED corriendo en la suite.
