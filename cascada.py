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
import modelo_pactivo as mp
import preclasificador
import reglas
from reglas import PACTIVOS_NO_MATCH_DIRECTO, normalizar
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
    metodo: str  # cruce_base|descarte_item|historico|regla_diccionario|conflicto_regla_modelo|modelo_descarte|modelo_pactivo|claude
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
    indice_inverso: "Optional[dict]" = None,
    modelo_pactivo=None,
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
    # IMPORTANTE: tanto match_combinacion como match_diccionario van SOLO sobre
    # la DESCRIPCIÓN del ítem, NO sobre el título del tender. Razón medida en
    # producción 2026-05-26: un tender llamado "BEVACIZUMAB" contiene un ítem
    # cuya descripción es "MAG. POLIDOCANOL 1% AMPOLLA". Si leemos título +
    # descripción, regla_diccionario matchea "Bevacizumab" del título y asigna
    # mal el pactivo. El título es el paraguas del tender (puede listar varios
    # fármacos); la descripción identifica el ÍTEM real.
    pactivo = reglas.match_combinacion(descripcion or "", combinaciones or [])
    por_combinacion = pactivo is not None
    if not pactivo:
        pactivo = reglas.match_diccionario(descripcion or "", pactivos_norm)
    if pactivo:
        # VETO del modelo entrenado sobre el match SIMPLE de diccionario.
        # match_diccionario hace un match de texto contra un catálogo que
        # incluye pactivos NO médicos ("Servicio de Aseo", "Cocina",
        # "Electrodo") — es la señal de interés más débil de la cascada. Si el
        # clasificador de descarte (entrenado con ~1M de decisiones humanas)
        # está MUY seguro de que la fila es descarte, la regla matcheó ruido y
        # gana el modelo. NO se aplica al match COMBINADO (señal fuerte: todos
        # los componentes de un pactivo real del catálogo presentes en la
        # glosa). El resultado lleva método propio para poder auditar el choque.
        if not por_combinacion:
            p_desc = descarte_modelo.prob_descarte(modelo_descarte, descripcion)
            if p_desc >= config.umbral_modelo_descarte:
                return Resultado(
                    interes=0,
                    pactivo=None,
                    composicion=None,
                    presentacion=None,
                    confianza=round(p_desc, 3),
                    metodo="conflicto_regla_modelo",
                    razon=(
                        f"La regla matcheó '{pactivo}', pero el clasificador "
                        f"de descarte entrenado lo descarta "
                        f"(probabilidad {p_desc:.2f})."
                    ),
                )
        comp_g, pres_g = taxonomia.extraer_de_glosa(texto, pactivo)
        # De TODAS las (comp,pres) que existen para este pactivo en el histórico
        # humano, la que mejor encaja con esta descripción. Convierte comp/pres
        # de texto libre a opción dentro de la lista finita REAL del fármaco.
        comp_o, pres_o = preclasificador.elegir_comp_pres_por_descripcion(
            tabla, pactivo, descripcion
        )
        comp_h, pres_h = preclasificador.comp_pres_por_pactivo(tabla, pactivo)
        comp = comp_g or comp_o or comp_h
        pres = pres_g or pres_o or pres_h
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

    # Modelo de pactivo APRENDIDO — antes de Claude, una red más barata. El
    # clasificador multiclase entrenado con ~1.6K pactivos y las glosas
    # históricas de las 4 fuentes captura los casos donde la glosa cambia de
    # forma pero comparte componentes léxicos (lo que hoy iba a Claude).
    # Si está MUY seguro, asigna pactivo + (comp,pres) desde el histórico real
    # del pactivo; si no llega al umbral, la fila sigue a Claude.
    pact_pred, conf = mp.predecir(modelo_pactivo, descripcion)
    # Si el modelo predice un meta-pactivo (Adjunto), lo ignoramos: su
    # asignación es contextual y solo Claude la decide. Igualmente si la
    # clase predicha no está en el catálogo activo de hoy (cliente desactivó).
    if (pact_pred and conf >= config.umbral_modelo_pactivo
            and normalizar(pact_pred) not in {normalizar(p) for p in PACTIVOS_NO_MATCH_DIRECTO}
            and normalizar(pact_pred) in pactivos_norm):
        comp_g, pres_g = taxonomia.extraer_de_glosa(texto, pact_pred)
        comp_o, pres_o = preclasificador.elegir_comp_pres_por_descripcion(
            tabla, pact_pred, descripcion
        )
        comp_h, pres_h = preclasificador.comp_pres_por_pactivo(tabla, pact_pred)
        return Resultado(
            interes=1,
            pactivo=pact_pred,
            composicion=comp_g or comp_o or comp_h,
            presentacion=pres_g or pres_o or pres_h,
            confianza=round(conf, 3),
            metodo="modelo_pactivo",
            razon=f"Clasificador de pactivo entrenado (probabilidad {conf:.2f}).",
        )

    # Claude — texto libre. Su salida se ajusta con snap al valor del catálogo.
    # Top-K pactivos cuyas palabras aparecen en la descripción → PISTA para
    # Claude (no acota el catálogo, solo lo guía). Si el índice no se cargó o
    # config.top_k_pactivos=0, va sin pista (comportamiento original).
    candidatos = (
        reglas.candidatos_top_k(descripcion, indice_inverso, k=config.top_k_pactivos)
        if indice_inverso and config.top_k_pactivos > 0 else []
    )
    c, uso = cc.clasificar(
        descripcion or "", titulo or "", vinculos or "", taxonomia, ejemplos,
        candidatos=candidatos,
    )
    comp, pres = c.composicion, c.presentacion
    if c.interes == 1:
        # Una vez que Claude propone un pactivo, sus comp/pres dejan de ser
        # texto libre: existen, para ese fármaco, opciones REALES en el
        # histórico humano. Preferimos la opción que más encaja con la
        # descripción (medido: comp 31% / pres 15% con generación libre, vs
        # ~85% / ~95% del histórico). Si ninguna opción matchea el texto o el
        # pactivo es NUEVO (sin histórico), se cae al texto de Claude + snap.
        comp_o, pres_o = preclasificador.elegir_comp_pres_por_descripcion(
            tabla, c.pactivo, descripcion
        )
        if comp_o:
            comp = comp_o
        if pres_o:
            pres = pres_o
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
