"""
engine_runner.py — Runner asíncrono del engine CLI (Hueco H, Sesión 10).

Reemplaza el subprocess.run bloqueante de bot._run_tool. Módulo profundo con
una sola operación pública:

    EngineRunner(max_concurrent=4).run(argv, timeout=30) -> str (awaitable)

Tres responsabilidades, todas invisibles para el llamante:

  1. Async nativo (asyncio.create_subprocess_exec) — cero hilos consumidos
     por invocación. El pool de to_thread queda reservado para las lecturas
     sqlite de milisegundos; los bloqueadores largos no compiten por él.

  2. Timeout con kill + reap. subprocess.run mataba y cosechaba solo; en
     async nativo es responsabilidad nuestra: sin el communicate() posterior
     al kill(), un engine colgado acumula zombies bajo Restart=always.

  3. Semáforo global de procesos engine en vuelo. El event loop bloqueante
     serializaba ACCIDENTALMENTE todas las invocaciones al CLI; al liberarlo
     (Hueco H) aparece una race nueva: escritores concurrentes sobre
     aquarela.db. WAL admite UN escritor y db.py del engine usa el
     busy-timeout default de sqlite3 (5s) — sin cota, una ráfaga real
     produciría errores "database is locked". max_concurrent=4 mantiene la
     espera del peor escritor muy por debajo de esos 5s. La mitigación vive
     del lado de Atlas: el engine no se toca (contrato del Hueco A).

Contrato de salida (idéntico al _run_tool previo): el stdout del engine tal
cual, o un JSON {"ok": false, "error": ...} — este módulo nunca lanza hacia
el loop agéntico.
"""
from __future__ import annotations

import asyncio
import json


class EngineRunner:
    """Ejecuta comandos del engine CLI: async nativo, timeout fail-safe, concurrencia acotada."""

    def __init__(self, max_concurrent: int = 4) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def run(self, argv: list[str], *, timeout: float = 30) -> str:
        """Corre argv y devuelve su stdout, o un JSON de error. Nunca lanza."""
        async with self._semaphore:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as e:
                return json.dumps({"ok": False, "error": str(e)})

            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
            except TimeoutError:
                self._kill(proc)
                await proc.communicate()  # cosecha: sin esto, zombie
                return json.dumps(
                    {"ok": False, "error": f"engine timeout after {timeout}s"}
                )
            except asyncio.CancelledError:
                # El deadline de turno (asyncio.timeout en handle_message)
                # puede cancelarnos a mitad de un engine call: no dejar el
                # proceso corriendo huérfano.
                self._kill(proc)
                raise

            out = stdout.decode().strip()
            return out or json.dumps({"ok": False, "error": "empty output"})

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        """kill() tolerante a la carrera proceso-ya-muerto."""
        try:
            proc.kill()
        except ProcessLookupError:
            pass
