"""Pre-clasificador local — etapa SIN costo de API.

Las descripciones de mercadopublico.cl son MUY repetitivas: los mismos productos
se licitan una y otra vez. Este módulo memoriza el histórico — si una persona ya
clasificó una descripción idéntica, reutiliza esa etiqueta. Resuelve gratis una
parte grande del volumen y deja a Claude solo lo nuevo o ambiguo.

Usa la conexión compartida del worker (no abre una por fila)."""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Optional

from config import config
from db import conexion_worker
from reglas import normalizar, normalizar_valor

log = logging.getLogger("preclasificador")


@dataclass
class Prediccion:
    interes: int
    pactivo: Optional[str]
    composicion: Optional[str]
    presentacion: Optional[str]
    confianza: float
    soporte: int  # cuántas veces se vio esa descripción ya clasificada


# Clasificación humana más frecuente para una descripción IDÉNTICA.
# Excluye la propia fila (clave para que el backtest no haga trampa) y los bots.
_SQL = """
SELECT pactivo, composicion, presentacion, estado_gestor, COUNT(*) AS n
FROM `{tabla}`
WHERE Descripcion = %s
  AND id <> %s
  AND estado_gestor IS NOT NULL
  AND nombre_clasificador IS NOT NULL
  AND nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_)'
GROUP BY pactivo, composicion, presentacion, estado_gestor
ORDER BY n DESC
LIMIT 1
"""


def buscar_en_historico(
    tabla: str, descripcion: Optional[str], fila_id: int
) -> Optional[Prediccion]:
    """Devuelve la etiqueta humana de una descripción idéntica ya clasificada,
    o None si no hay precedente."""
    if not descripcion or len(descripcion.strip()) < 8:
        return None
    conn = conexion_worker()
    with conn.cursor() as cur:
        cur.execute(_SQL.format(tabla=tabla), (descripcion, fila_id))
        r = cur.fetchone()
    if not r or not r.get("n"):
        return None
    soporte = int(r["n"])
    interes = int(r["estado_gestor"])
    # Asimetría descarte vs interés: un DESCARTE del histórico con soporte bajo
    # NO es confiable — puede ser un error humano puntual reproducido (backtest
    # 2026-05-22: 9 falsos negativos así, todos con soporte 1-2; ej. una fila
    # "ERITROPOYETINA" descartada por error). Si no alcanza el umbral, se
    # devuelve None y la fila sigue a cruce Base / reglas / Claude — que tienen
    # otra oportunidad de reconocerla. El interés sí se devuelve con soporte 1:
    # un falso positivo lo filtra el panel humano; un falso negativo, no.
    if interes == 0 and soporte < config.umbral_descarte_historico:
        return None
    confianza = min(0.99, 0.85 + 0.02 * soporte)
    return Prediccion(
        interes=interes,
        pactivo=r.get("pactivo"),
        composicion=r.get("composicion"),
        presentacion=r.get("presentacion"),
        confianza=confianza,
        soporte=soporte,
    )


# --- composición/presentación dominantes de un pactivo en el histórico --------
# COMODÍN "no se pudo inferir" — forma que usan los humanos. Distinto del valor
# del catálogo "Sin Clas" (con s) que sí es composición real para Polivitamínico,
# Oligoelementos, etc. y se asigna a través de comp_index/pres_index.
SIN_CLA = "Sin cla"

# Cache en memoria: {tabla: {pactivo_normalizado: (comp_dominante, pres_dominante)}}.
# Se llena UNA vez al arrancar el worker con un solo GROUP BY por tabla. Así la
# Etapa 2 (reglas) NO dispara un full-table-scan de compra_agil por cada fila
# que matchea un pactivo — que es lo único de la cascada que estresaba la BD.
_COMP_PRES: dict[str, dict[str, tuple]] = {}

# Cache paralelo con TODAS las opciones (no solo la dominante). Permite, dada una
# descripción y un pactivo, elegir la (comp, pres) que efectivamente EXISTE para
# ese fármaco y que mejor encaja con el texto — en vez de aceptar generación
# libre. Estructura: {tabla: {pactivo_norm: [(comp, pres, n_veces), ...]}}.
_COMP_PRES_OPCIONES: dict[str, dict[str, list[tuple]]] = {}

# Una sola pasada por tabla: comp/pres de TODOS los pactivos de interés.
_SQL_CP_TODOS = """
SELECT pactivo, composicion, presentacion, COUNT(*) AS n
FROM `{tabla}`
WHERE estado_gestor = 1 AND pactivo IS NOT NULL AND pactivo <> ''
  AND nombre_clasificador IS NOT NULL
  AND nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_)'
GROUP BY pactivo, composicion, presentacion
"""

# Fallback: comp/pres de UN pactivo (solo si no se precargó — p. ej. un script
# suelto). El worker siempre precarga, así que nunca llega acá.
_SQL_CP_UNO = """
SELECT composicion, presentacion, COUNT(*) AS n
FROM `{tabla}`
WHERE pactivo = %s AND estado_gestor = 1
  AND nombre_clasificador IS NOT NULL
  AND nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_)'
GROUP BY composicion, presentacion
"""


def _dominante(pares: list, total: int, umbral: float = 0.5, soporte_min: int = 3) -> str:
    """Valor dominante de una distribución, o 'Sin Cla' si ninguno domina."""
    acc: "Counter[str]" = Counter()
    for valor, n in pares:
        v = (valor or "").strip()
        if v:
            acc[v] += n
    if not acc:
        return SIN_CLA
    valor, n = acc.most_common(1)[0]
    return valor if (total >= soporte_min and n / total >= umbral) else SIN_CLA


def _resolver(filas: list) -> tuple:
    """De una lista de (composicion, presentacion, n) saca el par dominante."""
    total = sum(int(n) for _, _, n in filas)
    comp = _dominante([(c, int(n)) for c, _, n in filas], total)
    pres = _dominante([(p, int(n)) for _, p, n in filas], total)
    return (comp, pres)


def precargar_comp_pres(tablas) -> None:
    """Precalcula en memoria la composición/presentación dominante de CADA
    pactivo, con un único GROUP BY por tabla. Llamar UNA vez al arrancar el
    worker — reemplaza el scan por fila de la Etapa 2."""
    conn = conexion_worker()
    for tabla in tablas:
        por_pactivo: "dict[str, list]" = defaultdict(list)
        with conn.cursor() as cur:
            cur.execute(_SQL_CP_TODOS.format(tabla=tabla))
            for f in cur.fetchall():
                por_pactivo[normalizar(f["pactivo"])].append(
                    (f["composicion"], f["presentacion"], int(f["n"]))
                )
        _COMP_PRES[tabla] = {p: _resolver(fs) for p, fs in por_pactivo.items()}
        # Lista cruda de opciones por pactivo (orden estable: más frecuente primero).
        _COMP_PRES_OPCIONES[tabla] = {
            p: sorted(fs, key=lambda x: -x[2]) for p, fs in por_pactivo.items()
        }
        log.info("comp/pres precalculado en memoria: %s -> %d pactivos",
                 tabla, len(_COMP_PRES[tabla]))


def elegir_comp_pres_por_descripcion(
    tabla: str, pactivo: Optional[str], descripcion: Optional[str]
) -> tuple:
    """Una vez fijado el pactivo, comp/pres ya NO son texto libre — son una de
    las opciones reales que existen para ese fármaco en el histórico humano.
    Esta función mira esa lista finita y devuelve la (comp, pres) que mejor
    calza con la DESCRIPCIÓN actual.

    Score por opción:
      +2 si la composición normalizada (sin espacios) aparece en la descripción
         normalizada (capta '500mg' aunque la glosa diga '500 MG' o '500 mg').
      +1 si la presentación aparece como palabra (admite plural) en la glosa.
    Entre las mejores, gana la más frecuente. Si ninguna marca coincidencia,
    devuelve (None, None) y la cascada cae al respaldo de moda histórica.

    Para Claude resuelve el problema medido: comp/pres se equivocan en >2/3 de
    las filas porque genera texto libre que no existe para ese pactivo.
    """
    if not pactivo or not descripcion:
        return (None, None)
    cache = _COMP_PRES_OPCIONES.get(tabla)
    if not cache:
        return (None, None)
    opciones = cache.get(normalizar(pactivo))
    if not opciones:
        return (None, None)

    desc = normalizar(descripcion)
    if not desc:
        return (None, None)
    desc_sin_esp = desc.replace(" ", "")

    mejor: tuple | None = None
    mejor_score = 0
    for comp, pres, n in opciones:
        score = 0
        cn = normalizar_valor(comp or "")  # comp normalizado sin espacios
        if cn and cn in desc_sin_esp:
            score += 2
        pn = normalizar(pres or "")
        if pn and re.search(rf"\b{re.escape(pn)}s?\b", desc):
            score += 1
        if score == 0:
            continue
        # Mejor score gana; con el mismo score, la opción más frecuente.
        if score > mejor_score or (score == mejor_score and (mejor is None or n > mejor[2])):
            mejor = (comp, pres, n)
            mejor_score = score

    if mejor is None:
        return (None, None)
    return (mejor[0], mejor[1])


def comp_pres_por_pactivo(tabla: str, pactivo: Optional[str]) -> tuple:
    """Composición y presentación más frecuentes de un pactivo en el histórico
    humano. Cada una es su valor dominante, o 'Sin Cla' si no hay uno claro
    (caso en que el comodín «Sin Cla» es justamente la respuesta correcta).

    Lee del cache en memoria si el worker lo precargó; si no, consulta puntual."""
    if not pactivo:
        return (SIN_CLA, SIN_CLA)
    cache = _COMP_PRES.get(tabla)
    if cache is not None:
        return cache.get(normalizar(pactivo), (SIN_CLA, SIN_CLA))
    # Fallback sin precarga — solo scripts sueltos; el worker nunca cae acá.
    conn = conexion_worker()
    with conn.cursor() as cur:
        cur.execute(_SQL_CP_UNO.format(tabla=tabla), (pactivo,))
        filas = cur.fetchall()
    if not filas:
        return (SIN_CLA, SIN_CLA)
    return _resolver([(f["composicion"], f["presentacion"], int(f["n"])) for f in filas])


def _unif_decimal(s: str) -> str:
    """Unifica el separador decimal para COMPARAR: '7,5'->'7.5'. La coma chilena
    y el punto son la misma dosis ('7,5mg' == '7.5 mg')."""
    return (s or "").replace(",", ".")


def canonizar_comp(tax, tabla: str, pactivo: Optional[str], comp: Optional[str]) -> Optional[str]:
    """VALIDADOR FINAL de composición — confirma que la comp asignada EXISTE para
    el pactivo en el catálogo (Base + diccionario + histórico humano). Resuelve:
      - formato decimal: '7.5 mg' -> '7,5mg' (la forma canónica del catálogo);
      - dosis/volumen inventado: 'Clorhexidina 50ml' (no es comp de Clorhexidina,
        que son '0,2%','2%'...) -> 'Sin cla', porque la IA NO inventa.
    Si el pactivo no tiene comps conocidas (pactivo nuevo sin histórico ni Base),
    deja la comp tal cual — no rompe lo que no puede verificar. NO toca el comodín
    'Sin cla' / valor 'Sin Clas'."""
    if not comp or not comp.strip():
        return comp
    nv = normalizar_valor(comp)
    if nv in ("sincla", "sinclas"):
        return comp
    pact_n = normalizar(pactivo or "")
    if not pact_n:
        return comp
    # {decimal_unificado(normalizado): forma_canónica}. Base/diccionario PRIMERO
    # (forma oficial del catálogo, p.ej. '7,5mg' con coma chilena), luego el
    # histórico humano ordenado por frecuencia — setdefault da prioridad a Base.
    mapa: dict = {}
    hay = False
    for c in sorted(getattr(tax, "comp_por_pactivo", {}).get(pact_n, set())):
        if c and c.strip():
            hay = True
            mapa.setdefault(_unif_decimal(normalizar_valor(c)), c.strip())
    for c, _p, _n in _COMP_PRES_OPCIONES.get(tabla, {}).get(pact_n, []):
        if c and c.strip():
            hay = True
            mapa.setdefault(_unif_decimal(normalizar_valor(c)), c.strip())
    if not hay:
        return comp  # pactivo sin comps conocidas — no validar (no romper)
    return mapa.get(_unif_decimal(nv), "Sin cla")
