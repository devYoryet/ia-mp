"""Worker principal del clasificador IA.

Loop liviano: cada `INTERVALO_SEGUNDOS` toma un lote chico, lo clasifica en
cascada y persiste el resultado según el modo:

- MODO=test       → backtest: clasifica filas que YA clasificó una persona y
  compara, sin escribir en compra_agil. Es el modo por defecto y seguro.
- MODO=produccion → escribe la clasificación en compra_agil.

Las estructuras pesadas (catálogo, índices, descartes, cruce Base) se cargan
UNA vez al arrancar y se REFRESCAN cada día — así el worker toma las altas
nuevas sin reiniciarse y, por fila, no vuelve a la BD por estas cosas.

Uso:
    python3 worker.py            # loop continuo
    python3 worker.py --once     # una sola pasada (prueba)
"""

from __future__ import annotations

import logging
import sys
import time

import detector
import escritor
from cascada import clasificar_fila
from config import config
from cruce_base import cargar_cruce_base
from db import reintentar
from descarte_items import cargar_descartes
from descarte_modelo import cargar_modelo_descarte
from ejemplos import cargar_ejemplos
from preclasificador import precargar_comp_pres
from reglas import indexar_combinaciones, indexar_pactivos
from reglas_negocio import cargar_feedback
from taxonomia import cargar_taxonomia

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("worker")

TABLAS = ["compra_agil", "Licitaciones_diarias"]
MAX_FALLOS_SEGUIDOS = 5  # corte de circuito ante fallos repetidos de la API
REFRESCO_SEGUNDOS = 24 * 3600  # refresco diario de las estructuras en memoria


def cargar_recursos() -> dict:
    """Carga TODAS las estructuras en memoria — catálogo, índices, descartes,
    cruce Base, contexto humano. Se llama al arrancar y en cada refresco diario,
    para tomar las altas nuevas (pactivos, histórico, reglas) sin reiniciar."""
    log.info("Cargando recursos (Base + %s.diccionario)...", config.db_diccionario)
    taxonomia = reintentar(cargar_taxonomia)
    reintentar(lambda: precargar_comp_pres(TABLAS))
    contexto = "\n\n".join(p for p in (cargar_ejemplos(), cargar_feedback()) if p)
    recursos = {
        "taxonomia": taxonomia,
        "pactivos_norm": indexar_pactivos(taxonomia.pactivos),
        "combinaciones": indexar_combinaciones(taxonomia.pactivos),
        "descartes": reintentar(cargar_descartes),
        "cruce": reintentar(cargar_cruce_base),
        "modelo_descarte": cargar_modelo_descarte(),
        "contexto": contexto,
    }
    log.info(
        "Recursos: %d pactivos (%d combinados), %d composiciones, %d presentaciones.",
        len(taxonomia.pactivos), len(recursos["combinaciones"]),
        len(taxonomia.composiciones), len(taxonomia.presentaciones),
    )
    return recursos


def ciclo(r: dict) -> int:
    """Procesa un lote por tabla. Devuelve cuántas filas se clasificaron."""
    es_test = config.modo == "test"
    total = 0
    fallos_seguidos = 0
    for tabla in TABLAS:
        if es_test:
            filas = reintentar(lambda: detector.filas_para_backtest(tabla))
        else:
            filas = reintentar(lambda: detector.filas_pendientes(tabla))
        if filas:
            log.info("%s: %d filas a clasificar", tabla, len(filas))
        for fila in filas:
            try:
                resultado = clasificar_fila(
                    tabla, fila, r["taxonomia"], r["pactivos_norm"],
                    r["descartes"], r["cruce"], r["combinaciones"],
                    r["modelo_descarte"], r["contexto"],
                )
                if es_test:
                    reintentar(lambda: escritor.registrar_backtest(tabla, fila, resultado))
                else:
                    reintentar(lambda: escritor.aplicar_produccion(tabla, fila, resultado))
                total += 1
                fallos_seguidos = 0
            except Exception as exc:  # noqa: BLE001
                fallos_seguidos += 1
                log.error("fila %s de %s falló: %s", fila.get("id"), tabla, exc)
                if fallos_seguidos >= MAX_FALLOS_SEGUIDOS:
                    log.critical(
                        "%d fallos seguidos — se corta el ciclo (reintenta luego)",
                        fallos_seguidos,
                    )
                    return total
    return total


def main() -> None:
    if not config.anthropic_api_key:
        log.error("Falta ANTHROPIC_API_KEY en .env — no se puede clasificar.")
        sys.exit(1)

    log.info("=== Clasificador IA · modo: %s ===", config.modo.upper())
    if config.modo not in ("test", "produccion"):
        log.error("MODO inválido: %s (usar 'test' o 'produccion')", config.modo)
        sys.exit(1)
    if config.modo == "produccion":
        log.warning("MODO PRODUCCION: se ESCRIBIRÁ en compra_agil / Licitaciones_diarias.")

    recursos = cargar_recursos()
    ultimo_refresco = time.time()
    una_vez = "--once" in sys.argv
    while True:
        try:
            if not una_vez and time.time() - ultimo_refresco >= REFRESCO_SEGUNDOS:
                log.info("Refresco diario de las estructuras en memoria.")
                recursos = cargar_recursos()
                ultimo_refresco = time.time()
            n = ciclo(recursos)
            log.info("Ciclo terminado: %d filas procesadas.", n)
        except Exception as exc:  # noqa: BLE001
            log.error("Error de ciclo: %s", exc)
        if una_vez:
            break
        time.sleep(config.intervalo_segundos)


if __name__ == "__main__":
    main()
