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


def _pactivos_relevantes_recientes() -> set[str]:
    """Pactivos NORMALIZADOS que Carolina viene usando en feedback (últimos 30d).
    Lo usamos como criterio de relevancia para achicar el bloque de marcas:
    sólo incluir marcas cuyo pactivo Carolina USA hoy, no fósiles del histórico.
    Ver [[leccion-catalogo-historico-vs-vigente]]."""
    try:
        conn = conexion_worker()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT feedback_pactivo p "
                "FROM clasificador_ia_log "
                "WHERE feedback_pactivo IS NOT NULL AND feedback_pactivo <> '' "
                "AND revisado = 1 "
                "AND revisado_en >= NOW() - INTERVAL 30 DAY"
            )
            return {normalizar(r["p"]) for r in cur.fetchall() if r["p"]}
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo cargar feedback_pactivo reciente (%s)", exc)
        return set()


def cargar_marcas_para_prompt(
    pactivos_activos_norm: "set[str] | dict[str, str]",
    max_marcas: int = 200,
) -> str:
    """Bloque de texto que se inyecta como PISTA cacheable en el system prompt
    de Claude. Marca → pactivo INEQUÍVOCA cuyo pactivo está en catálogo activo
    Y que Carolina USA recientemente (feedback últimos 30d).

    Achicado 2026-06-05 de 1500 a 200 marcas: el bloque pesaba 44.5K chars
    (~11K tokens). Cada cache_write costaba 130K tokens. Costo medido +$0.0022/
    llamada = +$130/mes. Top 200 por relevancia reciente cubre los compuestos
    importantes (Acerdil D, Cardioplus Am, Brevex, Bramedil, Micardis Plus)
    manteniendo ~12% del tamaño anterior.

    Cuidado (ver [[fallos-y-lecciones]]): NO es mapeo determinista — el mapeo
    determinista palabra2→pactivo en cascada falló con 38% acierto. Acá Claude
    decide; las marcas son contexto cacheable. Filtros: marca >= 5 chars,
    pactivo en catálogo activo Y reciente, marca distinta de sustantivo
    genérico (_GENERICAS) y distinta de cualquier pactivo activo."""
    activos_set = pactivos_activos_norm if isinstance(pactivos_activos_norm, set) \
        else set(pactivos_activos_norm.keys())
    relevantes_recientes = _pactivos_relevantes_recientes()
    # Si no podemos cargar el reciente, no filtramos por relevancia (fallback)
    if not relevantes_recientes:
        relevantes_recientes = activos_set

    conn = conexion_worker()
    candidatos: "dict[str, set[str]]" = defaultdict(set)
    with conn.cursor() as cur:
        cur.execute(_SQL.format(db=config.db_diccionario))
        for r in cur.fetchall():
            pact = (r["pactivo"] or "").strip()
            pact_n = normalizar(pact)
            if not pact or pact_n not in activos_set:
                continue
            # solo pactivos que Carolina usa hoy (filtro de relevancia)
            if pact_n not in relevantes_recientes:
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
    log.info("Marcas prompt: %d marcas inequívocas%s (de %d candidatos, "
             "filtradas por relevancia reciente)",
             len(pares), " (truncado)" if truncado else "", len(mapa))
    return (
        "MARCAS COMERCIALES CONOCIDAS — PISTA del diccionario interno "
        "(no es veredicto; Claude decide según contexto). Selección: marcas "
        "inequívocas cuyo pactivo está en uso por Carolina los últimos 30d. "
        "Si la glosa contiene una de estas marcas como palabra completa, "
        "preferir el pactivo indicado. Si la glosa indica una variante "
        "compuesta (marca + 'D'/'Plus'/'HCT'/'Hzda'/'/12,5'), considerar el "
        "pactivo COMPUESTO correspondiente del catálogo en vez del simple.\n\n"
        f"{cuerpo}\n"
    )
