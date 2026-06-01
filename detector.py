"""Selecciona filas para clasificar, según el modo.

- modo 'test'      → `filas_para_backtest`: filas que YA clasificó una persona,
  para comparar humano vs IA sin tocar producción.
- modo 'produccion'→ `filas_pendientes`: filas nuevas sin clasificar.

Usa la conexión compartida del worker (no abre una por consulta)."""

from __future__ import annotations

import os

from config import config
from db import conexion_worker

# Offset opcional sobre el ORDER BY del backtest — permite correr la misma
# cascada sobre una ventana de filas DISTINTA a las más recientes, para
# verificar que un cambio no está sobreajustado a un sample puntual.
_BACKTEST_OFFSET = int(os.getenv("BACKTEST_OFFSET", "0"))

TABLAS_VALIDAS = ("compra_agil", "Licitaciones_diarias")

# --- producción: filas pendientes en el LEGACY que la IA aún no procesó ---
# Criterio: estado_gestor IS NULL (no clasificada por HUMANO en gestor_licitaciones)
# Y no está aún en clasificador_ia_log. NO se mira `nombre_clasificador` porque
# el legacy tiene un "Bot Clasificado"/"Bot Eliminado" que preclasifica pero NO
# es decisión humana — el worker la debe tomar igual. Orden: FIFO por publicación
# (más antigua primero) + urgencia (cierre más próximo después).
_SQL_PENDIENTES = """
SELECT t.id, t.Titulo, t.Descripcion, t.VINCULOS, t.Item, t.Cod_Onu, t.Fecha_Publicacion
FROM `{tabla}` t
WHERE t.estado_gestor IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM clasificador_ia_log log
    WHERE log.tabla_origen = %s AND log.fila_id = t.id
  )
ORDER BY t.Fecha_Publicacion ASC, t.Fecha_Cierre ASC
LIMIT %s
"""

# --- test/backtest: filas ya clasificadas por una PERSONA, aún no comparadas ---
_SQL_BACKTEST = """
SELECT t.id, t.Titulo, t.Descripcion, t.VINCULOS, t.Item, t.Cod_Onu,
       t.estado_gestor   AS humano_estado,
       t.pactivo         AS humano_pactivo,
       t.composicion     AS humano_composicion,
       t.presentacion    AS humano_presentacion,
       t.nombre_clasificador
FROM `{tabla}` t
LEFT JOIN clasificador_ia_backtest b
       ON b.tabla_origen = %s AND b.fila_id = t.id
WHERE t.estado_gestor IS NOT NULL
  AND t.nombre_clasificador IS NOT NULL
  AND t.nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_)'
  AND b.id IS NULL
ORDER BY t.fecha_clasificacion DESC
LIMIT %s OFFSET %s
"""


def _check(tabla: str) -> None:
    if tabla not in TABLAS_VALIDAS:
        raise ValueError(f"Tabla no permitida: {tabla}")


def filas_pendientes(tabla: str, limite: int | None = None) -> list[dict]:
    _check(tabla)
    conn = conexion_worker()
    with conn.cursor() as cur:
        cur.execute(
            _SQL_PENDIENTES.format(tabla=tabla),
            (tabla, limite or config.lote_max),
        )
        return list(cur.fetchall())


def filas_para_backtest(tabla: str, limite: int | None = None) -> list[dict]:
    _check(tabla)
    conn = conexion_worker()
    with conn.cursor() as cur:
        cur.execute(
            _SQL_BACKTEST.format(tabla=tabla),
            (tabla, limite or config.lote_max, _BACKTEST_OFFSET),
        )
        return list(cur.fetchall())
