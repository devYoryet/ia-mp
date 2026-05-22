"""Etapa 2 (sub-capa) — match por NOMBRE DE MARCA comercial.

La tabla `diccionario` de clasificación trae, además del pactivo, la columna
`palabra2` con la MARCA comercial limpia (ej. "Bramedil" → pactivo `Pargeverina`).

Una glosa de mercadopublico.cl suele venir con la MARCA del producto, no con su
principio activo. Sin este mapa, Claude tiene que adivinar qué contiene la marca
y alucina (dijo "betametasona + clorfenamina" para Bramedil).

IMPORTANTE — solo se usa `palabra2`. La columna `palabra1` (marca + sus variantes
mal escritas) se probó y FALLÓ: sus fragmentos de typos colisionan como falsos
positivos → 17% de acierto en backtest. Ver [[fallos-y-lecciones]]. Cualquier
reintento con `palabra1` está prohibido sin medir primero."""

from __future__ import annotations

import logging
import re
from collections import defaultdict

from config import config
from db import conexion_worker
from reglas import normalizar

log = logging.getLogger("marcas")

# Keywords más cortas que esto se descartan: producen falsos positivos.
_LARGO_MIN = 5
_TOKEN = re.compile(rf"[a-z0-9]{{{_LARGO_MIN},}}")

_SQL = """
SELECT pactivo, palabra2
FROM `{db}`.diccionario
WHERE pactivo IS NOT NULL AND pactivo <> ''
"""


def cargar_marcas() -> dict:
    """Devuelve {marca_normalizada: pactivo} usando SOLO `palabra2`. Una marca
    que apunta a más de un pactivo (ambigua) se descarta — solo las inequívocas."""
    conn = conexion_worker()
    candidatos: "dict[str, set]" = defaultdict(set)
    with conn.cursor() as cur:
        cur.execute(_SQL.format(db=config.db_diccionario))
        for r in cur.fetchall():
            pact = (r["pactivo"] or "").strip()
            if not pact:
                continue
            for tok in (r.get("palabra2") or "").split(","):
                k = normalizar(tok)
                if len(k) >= _LARGO_MIN:
                    candidatos[k].add(pact)
    mapa = {k: next(iter(v)) for k, v in candidatos.items() if len(v) == 1}
    log.info(
        "Marcas: %d keywords→pactivo (%d ambiguas descartadas)",
        len(mapa), len(candidatos) - len(mapa),
    )
    return mapa


def buscar_marca(texto: str, mapa: dict) -> str | None:
    """Pactivo si la glosa contiene EXACTAMENTE una marca conocida (como palabra
    completa). Si no hay ninguna, o hay varias que apuntan a pactivos distintos,
    devuelve None — que lo resuelva otra etapa o Claude."""
    if not texto or not mapa:
        return None
    palabras = set(_TOKEN.findall(normalizar(texto)))
    encontrados = {mapa[w] for w in palabras if w in mapa}
    return next(iter(encontrados)) if len(encontrados) == 1 else None
