"""Pre-clasificador local — etapa SIN costo de API.

Las descripciones de mercadopublico.cl son MUY repetitivas: los mismos productos
se licitan una y otra vez. Este módulo memoriza el histórico — si una persona ya
clasificó una descripción idéntica, reutiliza esa etiqueta. Resuelve gratis una
parte grande del volumen y deja a Claude solo lo nuevo o ambiguo.

Usa la conexión compartida del worker (no abre una por fila)."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Optional

from config import config
from db import conexion_worker
from reglas import normalizar

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
SIN_CLA = "Sin Cla"

# Cache en memoria: {tabla: {pactivo_normalizado: (comp_dominante, pres_dominante)}}.
# Se llena UNA vez al arrancar el worker con un solo GROUP BY por tabla. Así la
# Etapa 2 (reglas) NO dispara un full-table-scan de compra_agil por cada fila
# que matchea un pactivo — que es lo único de la cascada que estresaba la BD.
_COMP_PRES: dict[str, dict[str, tuple]] = {}

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
        log.info("comp/pres precalculado en memoria: %s -> %d pactivos",
                 tabla, len(_COMP_PRES[tabla]))


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
