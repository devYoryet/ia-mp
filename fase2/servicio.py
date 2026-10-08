"""Servicio de la Fase 2 — loop del container `ia-mp-fase2`.

- Cada F2_INTERVALO_SEGUNDOS (600): barrido RÁPIDO de los últimos F2_DIAS_RAPIDO (3) días.
- Una vez al día, desde la hora F2_HORA_PROFUNDO del MySQL (hora de Chile, 2 por
  defecto): barrido PROFUNDO de F2_DIAS_PROFUNDO (60) días. Así se recoge lo que
  se eliminó o rescató tarde, mirando hacia atrás.
- Si cambian las reglas (versión distinta a la de la última corrida): barrido
  profundo inmediato, para re-evaluar con las reglas nuevas.
- El diccionario código ONU → nombre se recalcula una vez al día.

El estado vive en la BD (clasificador_f2_corridas), no en memoria: reiniciar el
container no repite ni salta nada. $0 de API: no llama a Claude.
"""

from __future__ import annotations

import logging
import os
import time

import barrido
import motor
from bd import BD

log = logging.getLogger("fase2.servicio")

INTERVALO = int(os.getenv("F2_INTERVALO_SEGUNDOS", "600"))
DIAS_RAPIDO = int(os.getenv("F2_DIAS_RAPIDO", "3"))
DIAS_PROFUNDO = int(os.getenv("F2_DIAS_PROFUNDO", "60"))
HORA_PROFUNDO = int(os.getenv("F2_HORA_PROFUNDO", "2"))
ONU_CADA = 24 * 3600


def motivo_profundo(bd: BD, version: str) -> str | None:
    ahora = bd.uno("SELECT CURDATE() AS hoy, HOUR(NOW()) AS hora")
    ultima = bd.uno("SELECT version_reglas FROM clasificador_f2_corridas WHERE error IS NULL ORDER BY id DESC LIMIT 1")
    if ultima and ultima["version_reglas"] != version:
        return "cambiaron las reglas"
    p = bd.uno("SELECT MAX(creado_en) AS m FROM clasificador_f2_corridas WHERE tipo='profundo' AND error IS NULL")
    if (p["m"] is None or p["m"].date() < ahora["hoy"]) and ahora["hora"] >= HORA_PROFUNDO:
        return "diario"
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bd = BD()
    bd.exigir_usuario_restringido()
    log.info("=== Fase 2 · usuario %s · rápido %d d cada %d s · profundo %d d desde las %d h ===",
             bd.usuario_actual(), DIAS_RAPIDO, INTERVALO, DIAS_PROFUNDO, HORA_PROFUNDO)
    onu, onu_ts = None, 0.0
    while True:
        try:
            if onu is None or time.time() - onu_ts >= ONU_CADA:
                onu = barrido.cargar_onu(bd)
                barrido.persistir_onu(bd, onu)
                onu_ts = time.time()
                log.info("Diccionario ONU: %d códigos", len(onu))
            config = barrido.cargar_config(bd)
            version = motor.version(config)
            motivo = motivo_profundo(bd, version)
            if motivo:
                log.info("Barrido profundo (%s)", motivo)
                barrido.correr(bd, "profundo", DIAS_PROFUNDO, onu, config)
            else:
                barrido.correr(bd, "rapido", DIAS_RAPIDO, onu, config)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ciclo falló: %s", exc)
            bd.cerrar()  # reconecta en el próximo ciclo
        time.sleep(INTERVALO)


if __name__ == "__main__":
    main()
