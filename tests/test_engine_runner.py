"""
Hueco H (Sesión 10) — contrato RED del runner asíncrono del engine CLI.

Fija el contrato de un módulo NUEVO, engine_runner.py, que reemplaza el
subprocess.run bloqueante de bot._run_tool por async nativo
(asyncio.create_subprocess_exec) — cero hilos consumidos por invocación:

    EngineRunner(max_concurrent=4)
        .run(argv: list[str], *, timeout: float = 30) -> str

Contrato:
  1. Happy path: devuelve el stdout del proceso (mismo contrato observable
     que _run_tool: un string JSON del engine).
  2. Timeout: mata el proceso (kill) Y lo cosecha (reap) — sin zombies.
     subprocess.run hacía esto solo; en async nativo es responsabilidad
     nuestra. Devuelve el JSON de error {"ok": false, "error": ...timeout...}
     (mismo shape que el except de _run_tool hoy).
  3. Semáforo: como el event loop bloqueante serializaba ACCIDENTALMENTE
     todas las invocaciones al CLI, liberarlo crea una race nueva: escritores
     concurrentes sobre aquarela.db (WAL admite UNO; busy-timeout default 5s
     en db.py del engine). Un asyncio.Semaphore acota los procesos engine en
     vuelo a max_concurrent — mitigación del lado de Atlas, el engine no se
     toca (Hueco A intacto).

Estos tests usan subprocesos reales de Python (sys.executable -c ...), no
mocks: el kill/reap y el semáforo son exactamente el tipo de efecto que un
mock no puede validar.
"""
import asyncio
import json
import os
import subprocess
import sys

import pytest

import engine_runner


def _zombie_children() -> list[str]:
    """Hijos directos de este proceso en estado Z (defunct), vía ps."""
    out = subprocess.run(
        ["ps", "--ppid", str(os.getpid()), "-o", "pid=,stat="],
        capture_output=True, text=True,
    ).stdout
    return [
        line for line in out.splitlines()
        if line.split() and "Z" in line.split()[-1]
    ]


# ─── 1. Happy path: stdout del proceso, tal cual ──────────────────────────────

@pytest.mark.asyncio
async def test_run_returns_process_stdout():
    payload = json.dumps({"ok": True, "volunteers": []})
    runner = engine_runner.EngineRunner(max_concurrent=4)

    result = await runner.run(
        [sys.executable, "-c", f"print({payload!r})"], timeout=10,
    )

    assert json.loads(result) == {"ok": True, "volunteers": []}


# ─── 2. Timeout: kill + reap, sin zombies, error JSON con el shape actual ─────

@pytest.mark.asyncio
async def test_timeout_kills_and_reaps_the_process():
    """
    Un engine colgado no debe: (a) bloquear más allá del timeout, (b) dejar
    el proceso vivo, ni (c) dejar un zombie sin cosechar — bajo systemd
    Restart=always, los zombies de un engine enfermo se acumularían.
    """
    runner = engine_runner.EngineRunner(max_concurrent=4)
    loop = asyncio.get_running_loop()

    started = loop.time()
    result = await runner.run(
        [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.3,
    )
    elapsed = loop.time() - started

    assert elapsed < 5, f"run() tardó {elapsed:.1f}s — no respetó el timeout de 0.3s"

    data = json.loads(result)
    assert data["ok"] is False
    assert "timeout" in data["error"].lower()

    assert _zombie_children() == [], (
        "El proceso del engine quedó zombie: run() debe hacer proc.kill() "
        "y luego cosecharlo (await communicate/wait) tras el timeout."
    )


# ─── 3. Semáforo: máximo max_concurrent procesos engine en vuelo ──────────────

# Hijo real que anuncia su arranque (archivo 'started') y queda gateado hasta
# que exista el archivo 'gate' — determinista, sin dormir a ciegas.
_GATED_CHILD = (
    "import sys, time, os, json\n"
    "started, gate = sys.argv[1], sys.argv[2]\n"
    "open(started, 'w').write('x')\n"
    "while not os.path.exists(gate):\n"
    "    time.sleep(0.01)\n"
    "print(json.dumps({'ok': True}))\n"
)


@pytest.mark.asyncio
async def test_semaphore_bounds_in_flight_processes_to_max_concurrent(tmp_path):
    """
    N+1 llamadas simultáneas con max_concurrent=4: exactamente 4 procesos
    arrancan; el 5º queda retenido por el semáforo hasta que uno termina.
    """
    MAX = 4
    runner = engine_runner.EngineRunner(max_concurrent=MAX)
    gate = tmp_path / "gate"

    def argv(i: int) -> list[str]:
        return [sys.executable, "-c", _GATED_CHILD,
                str(tmp_path / f"started_{i}"), str(gate)]

    def started_count() -> int:
        return sum((tmp_path / f"started_{i}").exists() for i in range(MAX + 1))

    tasks = [asyncio.create_task(runner.run(argv(i), timeout=30))
             for i in range(MAX + 1)]

    # Espera determinista a que los 4 primeros anuncien arranque.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while started_count() < MAX and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert started_count() == MAX, "los 4 primeros procesos no llegaron a arrancar"

    # Periodo de gracia: si el semáforo funciona, el 5º NO PUEDE arrancar
    # (nadie termina hasta que exista 'gate'); si está roto, arranca en ms.
    await asyncio.sleep(0.3)
    assert started_count() == MAX, (
        f"{started_count()} procesos en vuelo — el semáforo no limitó a {MAX}"
    )

    # Abrir la compuerta: todos terminan y el 5º pasa a ejecutarse.
    gate.write_text("go")
    results = await asyncio.gather(*tasks)

    assert all(json.loads(r)["ok"] for r in results)
    assert started_count() == MAX + 1
