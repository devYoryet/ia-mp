"""Sub-capa de la Etapa 1 — cruce contra catálogos de OC reales.

`Base` son órdenes de compra REALES ya clasificadas: cada fila trae el texto del
comprador y del proveedor junto al pactivo / comp / presentación con que se
cerró la OC. Cruzar la `Descripcion` de una compra nueva (normalizada, match
exacto) contra esos textos resuelve un volumen importante con ~98% de acierto
de pactivo, GRATIS (sin API).

Hoy se usan DOS fuentes (las dos catalogan OC reales históricas):

  - `0001_td_oc.Base` — la base operativa actual (~440K OC).
  - `analisis_precios.Base` — análisis histórico de adjudicaciones (~920K OC).
    Mismos campos, nombres distintos (Esp_Proveedores en vez de EspProveedor,
    Composicion en vez de Comp, Presentacion en vez de MedidaPHT).

El índice se carga UNA vez al arrancar el worker —en lotes con `fetchmany`,
sin traer millones de filas de golpe— y se consulta en memoria por igualdad
exacta. Para textos ambiguos (un mismo texto con varias clasificaciones) gana
la clasificación más frecuente.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict

from db import conexion_worker
from reglas import normalizar

log = logging.getLogger("cruce_base")

# Textos muy cortos son genéricos/ambiguos — no se indexan.
_LARGO_MIN = 8
_LOTE = 5000

# Fuentes históricas de OC reales con clasificación humana. Cada una declara
# qué columnas leer (los nombres difieren entre bases). Si una base no existe
# o no es alcanzable, se loguea y se sigue con las demás.
_FUENTES = [
    {
        "db": "0001_td_oc",
        "tabla": "Base",
        "compr": "EspComprador",
        "prov": "EspProveedor",
        "pact": "Pactivo",
        "comp": "Comp",
        "pres": "MedidaPHT",
    },
    {
        "db": "analisis_precios",
        "tabla": "Base",
        "compr": "EspComprador",
        "prov": "Esp_Proveedores",
        "pact": "Pactivo",
        "comp": "Composicion",
        "pres": "Presentacion",
    },
]


def _cargar_fuente(votos: dict, fuente: dict) -> int:
    """Vuelca los registros de una fuente en el dict de votos. Devuelve cuántas
    OC se procesaron. Si la base no es alcanzable, devuelve 0 y loguea."""
    sql = (
        f"SELECT `{fuente['compr']}` AS compr, `{fuente['prov']}` AS prov, "
        f"`{fuente['pact']}` AS pact, `{fuente['comp']}` AS comp, "
        f"`{fuente['pres']}` AS pres "
        f"FROM `{fuente['db']}`.`{fuente['tabla']}` "
        f"WHERE `{fuente['pact']}` IS NOT NULL AND `{fuente['pact']}` <> ''"
    )
    conn = conexion_worker()
    n = 0
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            while True:
                lote = cur.fetchmany(_LOTE)
                if not lote:
                    break
                for f in lote:
                    n += 1
                    valor = (f["pact"], f["comp"], f["pres"])
                    for campo in (f["compr"], f["prov"]):
                        t = normalizar(campo)
                        if len(t) >= _LARGO_MIN:
                            votos[t][valor] += 1
    except Exception as exc:  # noqa: BLE001
        log.warning("Fuente %s.%s inaccesible (%s) — se omite",
                    fuente["db"], fuente["tabla"], exc)
        return 0
    log.info("Fuente %s.%s: %d OC procesadas", fuente["db"], fuente["tabla"], n)
    return n


def cargar_cruce_base() -> dict:
    """Devuelve {texto_normalizado: (pactivo, comp, pres)} — la clasificación
    MÁS frecuente para cada texto de comprador/proveedor, sumando los votos
    de TODAS las fuentes históricas."""
    votos: "dict[str, Counter]" = defaultdict(Counter)
    n_total = sum(_cargar_fuente(votos, f) for f in _FUENTES)
    indice = {t: c.most_common(1)[0][0] for t, c in votos.items()}
    log.info("Cruce Base: %d OC reales (todas las fuentes) -> %d textos indexados",
             n_total, len(indice))
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
