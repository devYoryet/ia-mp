"""Sub-capa de la Etapa 1 — cruce contra el catálogo de OC reales `0001_td_oc.Base`.

`Base` son órdenes de compra REALES ya clasificadas: cada fila trae el texto del
comprador (`EspComprador`) y del proveedor (`EspProveedor`) junto al `Pactivo`,
`Comp` (=composición) y `MedidaPHT` (=presentación) con que se cerró la OC.

Cruzar la `Descripcion` de una compra nueva (normalizada, match exacto) contra
esos ~880K textos resuelve ~18-20% del volumen con ~98% de acierto de pactivo,
GRATIS (sin API). Verificado 2026-05-20: 97,6% (compra_agil) / 99,2%
(Licitaciones_diarias) de coincidencia con la persona cuando hay match.

El índice se carga UNA vez al arrancar el worker —en lotes con `fetchmany`, sin
traer los 440K registros de golpe— y luego se consulta en memoria por igualdad
exacta. Para textos ambiguos (un mismo texto con varias clasificaciones) gana la
clasificación más frecuente.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict

from config import config
from db import conexion_worker
from reglas import normalizar

log = logging.getLogger("cruce_base")

# Textos muy cortos son genéricos/ambiguos — no se indexan.
_LARGO_MIN = 8
_LOTE = 5000

_SQL = """
SELECT EspComprador, EspProveedor, Pactivo, Comp, MedidaPHT
FROM `{cat}`.`Base`
WHERE Pactivo IS NOT NULL AND Pactivo <> ''
"""


def cargar_cruce_base() -> dict:
    """Devuelve {texto_normalizado: (pactivo, comp, pres)} — la clasificación
    MÁS frecuente para cada texto de comprador/proveedor visto en `Base`."""
    conn = conexion_worker()
    votos: "dict[str, Counter]" = defaultdict(Counter)
    n_oc = 0
    with conn.cursor() as cur:
        cur.execute(_SQL.format(cat=config.db_catalogo))
        while True:
            lote = cur.fetchmany(_LOTE)
            if not lote:
                break
            for f in lote:
                n_oc += 1
                valor = (f["Pactivo"], f["Comp"], f["MedidaPHT"])
                for campo in (f["EspComprador"], f["EspProveedor"]):
                    t = normalizar(campo)
                    if len(t) >= _LARGO_MIN:
                        votos[t][valor] += 1
    indice = {t: c.most_common(1)[0][0] for t, c in votos.items()}
    log.info("Cruce Base: %d OC reales -> %d textos indexados", n_oc, len(indice))
    return indice


def buscar(indice: dict, descripcion: str | None) -> tuple | None:
    """(pactivo, comp, pres) si la descripción coincide EXACTO con un texto de
    `Base`, o None. Valores VERBATIM — son de OC reales, no se canonizan."""
    if not indice or not descripcion:
        return None
    t = normalizar(descripcion)
    if len(t) < _LARGO_MIN:
        return None
    return indice.get(t)
