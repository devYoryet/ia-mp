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


# Sustantivos genéricos que aparecen en `palabra2` pero NO son marcas comerciales
# (caen al prompt como ruido y producen FPs). Lista construida por inspección:
# son palabras del lenguaje farma común — formas, presentaciones, insumos —
# que ningún cliente confundiría con un nombre de fantasía.
_GENERICAS = frozenset({
    "tabletas", "comprimidos", "capsulas", "jarabe", "ampolla", "ampollas",
    "vaselina", "alcohol", "agua", "suero", "solucion", "solucao", "spray",
    "gotas", "crema", "ungüento", "unguento", "gel", "polvo", "sobre", "frasco",
    "insulina", "protector", "guante", "guantes", "mascarilla", "mascarillas",
    "vendaje", "vendajes", "aposito", "apositos", "sonda", "sondas",
    "jeringa", "jeringas", "cateter", "cateteres", "tubo", "tubos",
    "torula", "torulas", "gasa", "gasas", "compresa", "compresas",
    "parche", "parches", "tableta", "capsula", "cápsula",
})


def cargar_marcas_para_prompt(
    pactivos_activos_norm: "set[str] | dict[str, str]",
    max_marcas: int = 1500,
) -> str:
    """Bloque de texto que se inyecta como PISTA cacheable en el system prompt
    de Claude. Marca → pactivo INEQUÍVOCA cuyo pactivo está en catálogo activo.

    Cuidado (ver [[fallos-y-lecciones]]): NO es mapeo determinista — el mapeo
    determinista palabra2→pactivo en cascada falló con 38% acierto en mayo. Acá
    Claude decide; las marcas son contexto cacheable (cero costo por fila gracias
    a prompt caching). Filtros: marca >= 5 chars, pactivo en catálogo activo,
    marca distinta de un sustantivo genérico (_GENERICAS) y distinta de cualquier
    pactivo activo (para no llamar "marca" a un genérico)."""
    activos_set = pactivos_activos_norm if isinstance(pactivos_activos_norm, set) \
        else set(pactivos_activos_norm.keys())
    conn = conexion_worker()
    candidatos: "dict[str, set[str]]" = defaultdict(set)
    with conn.cursor() as cur:
        cur.execute(_SQL.format(db=config.db_diccionario))
        for r in cur.fetchall():
            pact = (r["pactivo"] or "").strip()
            if not pact or normalizar(pact) not in activos_set:
                continue
            for tok in (r.get("palabra2") or "").split(","):
                marca = tok.strip()
                k = normalizar(marca)
                if len(k) < _LARGO_MIN:
                    continue
                if k in _GENERICAS or k in activos_set:
                    continue
                candidatos[marca].add(pact)
    # solo marcas inequívocas (1 pactivo)
    mapa = {m: next(iter(v)) for m, v in candidatos.items() if len(v) == 1}
    if not mapa:
        log.warning("Marcas prompt: 0 marcas inequívocas — pista vacía")
        return ""
    pares = sorted(mapa.items())  # estable para prompt caching
    truncado = len(pares) > max_marcas
    if truncado:
        pares = pares[:max_marcas]
    cuerpo = "\n".join(f"- {marca} → {pact}" for marca, pact in pares)
    log.info("Marcas prompt: %d marcas inequívocas%s",
             len(pares), " (truncado)" if truncado else "")
    return (
        "MARCAS COMERCIALES CONOCIDAS — PISTA del diccionario interno (no es "
        "veredicto; Claude decide según contexto):\n"
        "Si la glosa contiene una de estas marcas como palabra completa y el "
        "contexto encaja con el pactivo indicado, preferir ese pactivo. "
        "Si la glosa indica una variante compuesta (marca seguida de 'D', "
        "'Plus', 'HCT', 'Hzda', '/12,5', etc.), considerar el pactivo COMPUESTO "
        "correspondiente en el catálogo activo en vez del simple.\n\n"
        f"{cuerpo}\n"
    )
