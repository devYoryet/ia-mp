"""Cascada de clasificación, de la etapa más barata a la más cara.

  cruce Base   — ¿la descripción coincide con una OC REAL del catálogo?     GRATIS
  descarte     — ¿el código de rubro es uno siempre descartado?            GRATIS
  histórico    — ¿una persona ya clasificó una descripción idéntica?       GRATIS
  reglas       — ¿el texto contiene un pactivo conocido, inequívoco?       GRATIS
  Claude       — lo que queda: lo nuevo o ambiguo                          API

VERIFICACIÓN DE DESCARTES: el cruce Base va PRIMERO, antes del descarte por
rubro. Una coincidencia exacta con una orden de compra real es la señal más
fuerte de que la fila es un producto médico de interés — así un descarte por
rubro NO puede tapar un producto real. Es la red contra falsos negativos.
(Se probó un gate por "matchea un pactivo del catálogo" y se descartó: el
catálogo tiene pactivos NO médicos como "Servicio de Aseo" → ver
[[fallos-y-lecciones]].)

IMPORTANTE: cruce Base, histórico y reglas copian valores que YA están en el
sistema (clasificados por personas / de OC reales) — VERBATIM. Solo la salida de
Claude (texto libre) y la extracción desde la glosa se ajustan con `taxonomia`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import clasificador_claude as cc
import cruce_base
import descarte_modelo
import preclasificador
import reglas
from config import config
from descarte_items import COLUMNA_RUBRO
from taxonomia import Taxonomia


@dataclass
class Resultado:
    interes: Optional[int]
    pactivo: Optional[str]
    composicion: Optional[str]
    presentacion: Optional[str]
    confianza: float
    metodo: str  # 'cruce_base' | 'descarte_item' | 'historico' | 'regla_diccionario' | 'claude'
    razon: str
    pactivo_propuesto: Optional[str] = None  # pactivo nuevo, fuera de la lista
    costo_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0


def clasificar_fila(
    tabla: str,
    fila: dict,
    taxonomia: Taxonomia,
    pactivos_norm: dict,
    descartes: "Optional[dict]" = None,
    cruce: "Optional[dict]" = None,
    combinaciones: "Optional[list]" = None,
    modelo_descarte=None,
    ejemplos: str = "",
) -> Resultado:
    descripcion = fila.get("Descripcion")
    titulo = fila.get("Titulo")
    vinculos = fila.get("VINCULOS")
    texto = f"{titulo or ''} {descripcion or ''}".strip()

    # Cruce Base — descripción idéntica a una OC REAL del catálogo 0001_td_oc.Base.
    # Va PRIMERO: protege a un producto médico real de un descarte por rubro.
    # Valores VERBATIM (de OC reales, no se canonizan).
    hit = cruce_base.buscar(cruce, descripcion)
    if hit:
        pactivo_b, comp_b, pres_b = hit
        return Resultado(
            interes=1,
            pactivo=pactivo_b,
            composicion=comp_b,
            presentacion=pres_b,
            confianza=0.95,
            metodo="cruce_base",
            razon="Descripción idéntica a una OC real del catálogo Base.",
        )

    # Descarte por rubro — Item (compra_agil) / Cod_Onu (Licitaciones_diarias):
    # rubro que el histórico humano descartó SIEMPRE (>= N vistas, 0 de interés).
    rubros = (descartes or {}).get(tabla)
    if rubros:
        cod = (fila.get(COLUMNA_RUBRO[tabla]) or "").strip()
        if cod and cod in rubros:
            return Resultado(
                interes=0,
                pactivo=None,
                composicion=None,
                presentacion=None,
                confianza=0.97,
                metodo="descarte_item",
                razon=(
                    "Rubro que el histórico descartó siempre "
                    f"(>= {config.descarte_item_min_vistas} veces)."
                ),
            )

    # Histórico — descripción idéntica ya clasificada por una persona. Un
    # DESCARTE del histórico con soporte bajo no se confía (ver buscar_en_historico).
    p = preclasificador.buscar_en_historico(tabla, descripcion, fila.get("id", 0))
    if p:
        return Resultado(
            interes=p.interes,
            pactivo=p.pactivo,
            composicion=p.composicion,
            presentacion=p.presentacion,
            confianza=p.confianza,
            metodo="historico",
            razon=f"Descripción idéntica ya clasificada por una persona ({p.soporte}x).",
        )

    # Reglas — pactivo inequívoco. Primero un pactivo COMBINADO del catálogo
    # (todos sus componentes en la descripción, sin importar el orden — el orden
    # lo fija el diccionario central); si no, el nombre simple de un pactivo.
    # comp/pres se leen de la GLOSA; el histórico de ese pactivo es el respaldo.
    # El match de combinación va SOLO sobre la descripción, no el título (el
    # título es el paraguas del tender y lista varios fármacos).
    pactivo = reglas.match_combinacion(descripcion or "", combinaciones or [])
    por_combinacion = pactivo is not None
    if not pactivo:
        pactivo = reglas.match_diccionario(texto, pactivos_norm)
    if pactivo:
        comp_g, pres_g = taxonomia.extraer_de_glosa(texto)
        comp_h, pres_h = preclasificador.comp_pres_por_pactivo(tabla, pactivo)
        comp = comp_g or comp_h
        pres = pres_g or pres_h
        detalle = "combinado" if por_combinacion else "diccionario"
        return Resultado(
            interes=1,
            pactivo=pactivo,
            composicion=comp,
            presentacion=pres,
            confianza=0.90,
            metodo="regla_diccionario",
            razon=f"Pactivo '{pactivo}' ({detalle}); comp/pres de la glosa o histórico.",
        )

    # Descarte aprendido — última red barata antes de Claude. El clasificador
    # entrenado (modelo_descarte.joblib) ya vio que ninguna etapa de interés
    # reclamó esta fila; si está MUY seguro de que es descarte, se resuelve sin
    # gastar una llamada. Se aplica sobre la DESCRIPCIÓN (el texto con que se
    # entrenó). El cruce Base corrió primero — un producto real ya está a salvo.
    p_desc = descarte_modelo.prob_descarte(modelo_descarte, descripcion)
    if p_desc >= config.umbral_modelo_descarte:
        return Resultado(
            interes=0,
            pactivo=None,
            composicion=None,
            presentacion=None,
            confianza=round(p_desc, 3),
            metodo="modelo_descarte",
            razon=f"Clasificador de descarte entrenado (probabilidad {p_desc:.2f}).",
        )

    # Claude — texto libre. Su salida se ajusta con snap al valor del catálogo.
    c, uso = cc.clasificar(
        descripcion or "", titulo or "", vinculos or "", taxonomia, ejemplos
    )
    comp, pres = c.composicion, c.presentacion
    if c.interes == 1:
        comp, pres = taxonomia.snap(c.pactivo, comp, pres)
    return Resultado(
        interes=c.interes,
        pactivo=c.pactivo,
        composicion=comp,
        presentacion=pres,
        confianza=float(c.confianza),
        metodo="claude",
        razon=c.razon,
        pactivo_propuesto=c.pactivo_propuesto if c.pactivo_fuera_de_lista else None,
        costo_usd=uso.costo_usd,
        tokens_in=uso.tokens_in,
        tokens_out=uso.tokens_out,
        cache_read=uso.cache_read,
        cache_write=uso.cache_write,
    )
