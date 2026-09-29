"""Registro central de FUENTES — las tablas de mercadopublico.cl que clasifica la IA.

Cada tipo de oportunidad (compra ágil, licitación, cotización, ...) vive en su
propia tabla del clásico con columnas parecidas pero NO idénticas. Antes los
nombres de tabla estaban repetidos a mano en worker, detector, panel, backtest y
descarte por rubro; agregar una fuente obligaba a encontrarlos todos. Ahora cada
fuente se declara UNA vez acá y el resto del sistema la lee de este registro.

Qué declara cada fuente (ver docs/03-fuentes.md para el detalle de cada tabla):

- `columna_rubro`: el código de rubro UNSPSC/ONU con que `descarte_items` descarta.
  OJO, los nombres engañan: en compra_agil y cotizaciones el rubro está en `Item`;
  en Licitaciones_diarias está en `Cod_Onu` (ahí `Item` es el nº de línea).
- `columna_fecha_muestreo`: con qué fecha el backtest arma la ventana. En
  cotizaciones, desde mar-2026 los DESCARTES humanos quedan con
  `fecha_clasificacion` NULL — muestrear por esa columna vería sólo el interés.
- `limpiar_sufijo_unspsc`: el scraper de cotizaciones arma
  `Descripcion = glosa + ' ' + nombre_UNSPSC`. Ese nombre suele ser OTRO fármaco
  ("SEMAGLUTIDA 4MG/3ML Glucagon", "ALPELISIB 250MG Ciclosporina") y confunde a
  regla_diccionario, a los modelos y a Claude. Ver `preparar_fila`.

Qué fuentes procesa el worker lo decide `FUENTES_WORKER` (.env), NO este registro:
una fuente puede estar registrada (panel, backtest, reportes) sin que el worker
de producción la clasifique todavía.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass

log = logging.getLogger("fuentes")


@dataclass(frozen=True)
class Fuente:
    tabla: str
    etiqueta: str                    # nombre para el panel ("Compras ágiles")
    columna_rubro: str               # código UNSPSC para descarte_items
    columna_fecha_muestreo: str = "fecha_clasificacion"
    limpiar_sufijo_unspsc: bool = False
    # Rama modelo_adjunto (etapa 3). Se apaga por fuente cuando su etiqueta no
    # corresponde a cómo clasifica el equipo esa fuente.
    usar_modelo_adjunto: bool = True
    # Umbral propio de modelo_pactivo (etapa 7). None = el global de config
    # (UMBRAL_MODELO_PACTIVO, calibrado sobre compra_agil).
    umbral_modelo_pactivo: float | None = None


FUENTES: dict[str, Fuente] = {
    f.tabla: f
    for f in (
        Fuente("compra_agil", "Compras ágiles", columna_rubro="Item"),
        Fuente("Licitaciones_diarias", "Licitaciones", columna_rubro="Cod_Onu"),
        Fuente("cotizaciones", "Cotizaciones", columna_rubro="Item",
               columna_fecha_muestreo="Fecha_Publicacion",
               limpiar_sufijo_unspsc=True,
               # Medido 2026-09-29: 0 de 11 aciertos en 12 semanas. En
               # cotizaciones "el detalle está en el adjunto" se etiqueta
               # 'Varios Productos' (246 filas) o se desglosa leyendo el adjunto
               # (Fluoxetina, Sertralina... en filas separadas); 'Adjunto' casi
               # no se usa (20 filas).
               usar_modelo_adjunto=False,
               # Medido 2026-09-29: en la banda 0,30-0,50 acierta 5 de 17
               # (test rápidos, cinta correctora, protector solar).
               umbral_modelo_pactivo=0.50),
    )
}

# Todas las tablas que el sistema conoce (panel, sync de aprobaciones, reportes).
TABLAS_VALIDAS: tuple[str, ...] = tuple(FUENTES)

_WORKER_DEFAULT = "compra_agil,Licitaciones_diarias"


def fuentes_worker() -> list[str]:
    """Tablas que el worker de PRODUCCIÓN clasifica, en orden. Se habilita una
    fuente nueva agregándola a FUENTES_WORKER en el .env del host — sin deploy."""
    crudo = os.getenv("FUENTES_WORKER", _WORKER_DEFAULT)
    tablas = [t.strip() for t in crudo.split(",") if t.strip()]
    desconocidas = [t for t in tablas if t not in FUENTES]
    if desconocidas:
        raise ValueError(f"FUENTES_WORKER tiene tablas no registradas: {desconocidas}")
    return tablas


def usa_modelo_adjunto(tabla: str) -> bool:
    f = FUENTES.get(tabla)
    return f.usar_modelo_adjunto if f else True


def umbral_modelo_pactivo(tabla: str, global_: float) -> float:
    f = FUENTES.get(tabla)
    return f.umbral_modelo_pactivo if f and f.umbral_modelo_pactivo is not None else global_


def etiqueta(tabla: str) -> str:
    f = FUENTES.get(tabla)
    return f.etiqueta if f else tabla


def check(tabla: str) -> None:
    """Toda tabla que se interpola en un SQL pasa por acá (evita inyección)."""
    if tabla not in FUENTES:
        raise ValueError(f"Tabla no permitida: {tabla}")


# --------------------------------------------------------------------------
# Limpieza del sufijo UNSPSC (cotizaciones)
# --------------------------------------------------------------------------
# {tabla: {codigo_rubro: sufijo}}. Se aprende de los DATOS, no se escribe a mano:
# para cada código, las palabras finales que comparten TODAS sus glosas. Con dos
# o más glosas distintas del mismo código, lo único común al final es el nombre
# UNSPSC que pegó el scraper. Medido 2026-09-29 sobre 96.207 cotizaciones: cubre
# el 97,6 % de las filas. La tabla `sys_onu_codigos_analizados` NO sirve de
# fuente: en 1.352 códigos su `nombre` es una glosa, no el nombre UNSPSC.
_SUFIJOS: dict[str, dict[str, str]] = {}

_SQL_GLOSAS = """
SELECT `{col}` AS cod, Descripcion
FROM `{tabla}`
WHERE Descripcion IS NOT NULL AND `{col}` IS NOT NULL
"""


def _aprender_sufijos(filas) -> dict[str, str]:
    por_cod: dict[str, set] = defaultdict(set)
    for r in filas:
        cod = (r["cod"] or "").strip()
        d = (r["Descripcion"] or "").strip()
        if cod and d:
            por_cod[cod].add(d)
    sufijos = {}
    for cod, glosas in por_cod.items():
        if len(glosas) < 2:
            continue  # con una sola glosa no se distingue glosa de sufijo
        partidas = [g.split() for g in glosas]
        k = 0
        while (all(len(p) > k + 1 for p in partidas)  # nunca comerse la glosa entera
               and len({p[-1 - k].lower() for p in partidas}) == 1):
            k += 1
        if k:
            sufijos[cod] = " ".join(partidas[0][-k:])
    return sufijos


def precargar_sufijos(tablas, conn=None) -> None:
    """Aprende los sufijos UNSPSC de las fuentes que los necesitan. Llamar al
    arrancar el worker/backtest y en el refresco diario (códigos nuevos)."""
    from db import conexion_worker
    conn = conn or conexion_worker()
    for tabla in tablas:
        f = FUENTES[tabla]
        if not f.limpiar_sufijo_unspsc:
            continue
        with conn.cursor() as cur:
            cur.execute(_SQL_GLOSAS.format(tabla=tabla, col=f.columna_rubro))
            _SUFIJOS[tabla] = _aprender_sufijos(cur.fetchall())
        log.info("%s: %d sufijos UNSPSC aprendidos", tabla, len(_SUFIJOS[tabla]))


def quitar_sufijo(tabla: str, codigo: str | None, texto: str | None) -> str | None:
    """Devuelve `texto` sin el sufijo UNSPSC de su código, si lo trae al final.
    Si el código no tiene sufijo conocido o el texto no termina con él, lo
    devuelve intacto (nunca recorta a ciegas)."""
    if not texto:
        return texto
    suf = _SUFIJOS.get(tabla, {}).get((codigo or "").strip())
    if not suf:
        return texto
    t = texto.rstrip()
    if t.lower().endswith(suf.lower()) and len(t) > len(suf):
        return t[: -len(suf)].rstrip()
    return texto


def preparar_fila(tabla: str, fila: dict) -> dict:
    """Copia de la fila lista para la cascada. Para fuentes sin limpieza devuelve
    la MISMA fila (compra_agil y Licitaciones_diarias no cambian en nada).

    Para cotizaciones:
      - `Descripcion` sin el sufijo UNSPSC (lo que leen reglas, modelos y Claude);
      - `VINCULOS` = glosa + '.' + descripción de la cotización: se le limpia
        el mismo prefijo;
      - `_desc_cruda` = la glosa ORIGINAL, para las etapas donde el sufijo SUMA:
          · histórico: el propio de la tabla está guardado con sufijo (match exacto);
          · modelo_descarte: el nombre UNSPSC es buena señal de RUBRO. Medido
            2026-09-29 en 13.496 cotizaciones de 2026: con la glosa cruda resuelve
            829 descartes más que con la limpia, con el mismo riesgo (2 vs 3 de
            632 intereses). En cambio, para el PACTIVO el sufijo es veneno: con la
            glosa limpia el diccionario acierta 614 intereses más de 7.203 y la
            mitad de pactivos equivocados (192 vs 396).
    La fila original no se toca: escritor registra en el log lo que ve la persona."""
    f = FUENTES.get(tabla)
    if not f or not f.limpiar_sufijo_unspsc:
        return fila
    if os.getenv("LIMPIAR_SUFIJO_UNSPSC", "1") == "0":  # A/B del backtest
        return fila
    cruda = fila.get("Descripcion")
    limpia = quitar_sufijo(tabla, fila.get(f.columna_rubro), cruda)
    if limpia == cruda:
        return fila
    nueva = dict(fila)
    nueva["Descripcion"] = limpia
    nueva["_desc_cruda"] = cruda
    vinc = fila.get("VINCULOS") or ""
    if cruda and vinc.startswith(cruda):
        nueva["VINCULOS"] = limpia + vinc[len(cruda):]
    return nueva
