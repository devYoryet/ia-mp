"""Etapa 0 — descarte por código de rubro (sin consulta por fila, sin API).

Muchos códigos de rubro corresponden a bienes y servicios que NUNCA son
medicamentos ni insumos médicos: papelería, alimentos, construcción, servicios.
Si un código apareció muchas veces en el histórico y SIEMPRE terminó descartado,
una fila nueva con ese mismo código se descarta sin gastar una llamada a Claude.

- `compra_agil` cruza por `Item` (ahí `Item` ES el código ONU del rubro).
- `Licitaciones_diarias` cruza por `Cod_Onu` (ahí `Item` es solo un nº de línea;
  `Cod_Onu` está 100% poblado y 3.484 códigos cubren ~53% de sus descartes).

Los conjuntos se cargan UNA vez al arrancar el worker (y en el refresco diario)
y se consultan en memoria por igualdad exacta.
"""

from __future__ import annotations

import logging

from config import config
from db import conexion_worker

log = logging.getLogger("descarte_items")

# Columna de rubro por tabla — es lo que se cruza contra el conjunto.
COLUMNA_RUBRO = {"compra_agil": "Item", "Licitaciones_diarias": "Cod_Onu"}

# Códigos clasificados por personas que, vistos al menos `min_vistas` veces,
# terminaron SIEMPRE descartados: SUM(estado_gestor <> 0) = 0.
_SQL = """
SELECT `{col}` AS cod
FROM `{tabla}`
WHERE `{col}` IS NOT NULL AND TRIM(`{col}`) <> ''
  AND estado_gestor IS NOT NULL
  AND nombre_clasificador IS NOT NULL
  AND nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_)'
GROUP BY `{col}`
HAVING COUNT(*) >= %s AND SUM(estado_gestor <> 0) = 0
"""


def cargar_descartes(min_vistas: int | None = None) -> dict:
    """Devuelve {tabla: frozenset(códigos)} — los códigos de rubro que el
    histórico humano descartó SIEMPRE. Lookup por igualdad exacta del texto."""
    umbral = min_vistas if min_vistas is not None else config.descarte_item_min_vistas
    conn = conexion_worker()
    resultado: dict[str, frozenset] = {}
    for tabla, col in COLUMNA_RUBRO.items():
        with conn.cursor() as cur:
            cur.execute(_SQL.format(tabla=tabla, col=col), (umbral,))
            cods = {(r["cod"] or "").strip() for r in cur.fetchall()}
        cods.discard("")
        resultado[tabla] = frozenset(cods)
        log.info(
            "Etapa 0: %s -> %d códigos siempre-descartados (columna %s, >= %d vistas)",
            tabla, len(resultado[tabla]), col, umbral,
        )
    return resultado
