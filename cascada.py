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

import re
from dataclasses import dataclass
from typing import Optional

import clasificador_claude as cc
import cruce_base
import descarte_modelo
import modelo_marcas as mm
import modelo_pactivo as mp
import preclasificador
import reglas
from reglas import PACTIVOS_NO_MATCH_DIRECTO, normalizar
from config import config
from descarte_items import COLUMNA_RUBRO
from taxonomia import Taxonomia

# Sensidiscos / sencidiscos / sensi discos (con o sin tilde, plural opcional) —
# discos de antibiograma. Descarte duro por palabra clave (ver veto en la cascada).
VETO_SENSIDISCOS = re.compile(r"sensi\s*disco|senci\s*disco", re.IGNORECASE)

# VETOS DUROS por glosa — descartan ANTES de cualquier rama (igual molde que
# sensidiscos). Migran reglas largas del prompt (regla 385 consolidada) al
# código, sin costo extra en el prompt cacheable. Cada uno cubre un producto
# que en Pharmatender NO se clasifica nunca. Consolidados 2026-06-05.

# STENT (cualquier tipo: coronario, biliar, carotídeo, intracraneal) y TIRAS
# epsilométricas (=antibiograma en tira). Caso medido 2026-06-04: 83 FP/48h
# de 'Everolimus' por matchear el medicado del stent. NO confundir con TIRAS
# REACTIVAS de glucosa (esas tienen 'reactiv' + 'glucosa' y son válidas).
VETO_STENT_Y_TIRAS = re.compile(
    r"\b(?:stent|tira\s+epsilom[eé]tric|epsilometr[ií]a)\b",
    re.IGNORECASE,
)

# DETERMINACIONES MOLECULARES + MGIT (tubes o cualquier sufijo) + kits
# diagnósticos de microbiología. Insumos de lab clínico, no farma.
# 'mgit' standalone porque las glosas vienen 'MGIT Tbc Identification Test',
# 'MGIT 960', etc. — el sufijo varía.
VETO_LAB_NO_FARMA = re.compile(
    r"\b(?:determinaciones\s+moleculares|mgit|"
    r"kit\s+(?:de\s+)?diagn[oó]stico|kit\s+diagn[oó]stico|"
    r"reactivo\s+(?:de\s+)?microbiolog|cultivo\s+(?:para|de)\s+tbc)\b",
    re.IGNORECASE,
)

# OSTOMÍA: las glosas vienen en varios formatos ('bolsa drenable PARA colostomia',
# 'bolsa abierta colostomia', 'set ostomia'). Más fiable matchear el sustantivo
# colostom/ileostom/urostom directamente (con cualquier sufijo).
VETO_OSTOMIA = re.compile(
    r"\b(?:colostom\w*|ileostom\w*|urostom\w*|"
    r"bolsa\s+drenable\s+(?:de\s+)?ostom|set\s+(?:de\s+)?ostom)",
    re.IGNORECASE,
)

# TRAQUETUBO / tubo endotraqueal / cánula traqueal. Caso medido 2026-06-04:
# 6 falsos Adjunto por glosas "Traquetubo X.0, instrumental quirúrgico,
# Dispositivos médicos estériles, según especificaciones anex" — Claude
# tomó "según especificaciones anex" como Adjunto pero es instrumental.
VETO_TRAQUETUBO = re.compile(
    r"\b(?:traquetubo|tubo\s+(?:traqueal|endotraqueal)|c[aá]nula\s+traqueal|"
    r"c[aá]nula\s+endotraqueal)\b",
    re.IGNORECASE,
)

# AGUA OXIGENADA / peróxido de hidrógeno (cualquier concentración).
VETO_AGUA_OXIGENADA = re.compile(
    r"\b(?:agua\s+oxigenada|per[oó]xido\s+de\s+hidr[oó]geno|h2o2)\b",
    re.IGNORECASE,
)

# TEST DE SCHIRMER (oftalmología — test de lágrima).
VETO_TEST_SCHIRMER = re.compile(
    r"\btest\s+(?:de\s+)?schirmer\b",
    re.IGNORECASE,
)

# JABÓN LÍQUIDO en cualquier presentación. EXCEPCIÓN: 'jabón alcohol' o
# 'alcohol gel jabón' SÍ se clasifica (= Alcohol Gel). Por eso el veto solo
# aplica cuando la glosa tiene "jabón líquido"/"jabón de manos"/"jabón
# institucional" y NO menciona "alcohol gel".
VETO_JABON_NO_FARMA = re.compile(
    r"\b(?:jab[oó]n\s+(?:l[ií]quido|de\s+manos|institucional|de\s+glicerina|"
    r"perfumado|para\s+manos|para\s+ba[nñ]o|antibact|antis[eé]ptic|"
    r"neutro|para\s+ducha|infantil|kemikal))\b",
    re.IGNORECASE,
)
# Excepción al jabón: si la glosa también menciona "alcohol gel" o
# "alcohol en gel", ese sí se clasifica como Alcohol Gel.
_JABON_EXCEPCION = re.compile(r"alcohol\s+(?:en\s+)?gel", re.IGNORECASE)

# INSUMOS QUIRÚRGICOS NO FARMA puntuales: stent + set de irrigación + azul
# tripán + Endosolv + agente disolvente endodóntico.
VETO_INSUMOS_PUNTUALES = re.compile(
    r"\b(?:set\s+(?:de\s+)?irrigaci[oó]n|azul\s+trip[aá]n|endosolv|"
    r"agente\s+disolvente\s+endod[oó]nt)\b",
    re.IGNORECASE,
)

# 'Cinta Adhesiva Médica' es el pactivo que más sobre-asigna modelo_pactivo:
# medido 7 FP/9 vía modelo_pactivo en 30d, y 20 FP/35 en 7d post-deploy.
# El 80% del error del modelo viene de este único pactivo. Si el modelo lo
# predice pero la glosa trae señales claras NO-médicas (oficina, ferretería,
# embalaje, uro test, masking, doble cara, fixomul→apósito, ENMASCARAR,
# CINTA DE PAPEL, CINTA METRICA, PASTA ADHESIVA), VETAR la predicción del
# modelo y dejar que la cascada siga (modelo_descarte/Claude). Criterio
# consolidado correcciones Carolina del 25/29-may, 01-jun y 03-jun.
VETO_CINTA_NO_MEDICA = re.compile(
    r"\b(?:embalaj?e|embalar|marbete|escritorio|oficina|enseñanza|ensenanza|"
    r"doble\s*(?:cara|contacto)|uro\s*test|urotest|construccion|construcción|"
    r"ferreter[ií]a|aislante|electric[ao]?|eléctric[ao]?|vulcani[sz]ante|"
    r"americana|duct\s*tape|masking|aseo|fixomul|"
    r"enmascarar|enmascara|enmascarado|cinta\s+de\s+papel|cinta\s+papel|"
    r"cinta\s+m[eé]trica|cinta\s+metro|pasta\s+adhesiva|cinta\s+vidrio|"
    r"cinta\s+(?:plastic|pl[áa]stica)|cinta\s+adhesiva\s+(?:enmascarar|papel|"
    r"de\s+vidrio|de\s+pintor))\b",
    re.IGNORECASE,
)

# 'Lubricante' en Pharmatender = SOLO lubricante íntimo/vaginal/personal/sexual.
# El catálogo activo lo mantiene por farmacia comunitaria. Pero la regla_diccionario
# matchea CUALQUIER 'lubricante' en la glosa, atrapando lubricantes industriales,
# aceites de turbina, DW-40, silicona para instrumental, etc. Veto amplio: si el
# pactivo predicho es 'Lubricante' y la glosa contiene palabras de uso
# industrial/instrumental, anular. Criterio Carolina 2026-06-02: "solo el íntimo".
# Ampliado 2026-06-03 tras 9 FP/11 medidos en 14d: empaquetadura, afloja tuercas,
# desoxidante, adhesivo, base de silicona, alimenticio, lubriclav, autoclave, NSF.
VETO_LUBRICANTE_NO_INTIMO = re.compile(
    r"\b(?:turbina|contra\s*[aá]ngulo|instrumental|"
    r"sint[eé]tico|silicona\s*lubricante|aceite|"
    r"dw-?40|anticorrosivo|ferreter[ií]a|industrial|"
    r"motor|m[áa]quina|maquinaria|mec[áa]nic[oa]|"
    r"penetrante|grasa|esmeril|engranaj?e|sello|"
    r"spray\s+(?:lubricante|sintetico)|"
    r"empaquetadura(?:s)?|afloja\s*tuerca(?:s)?|desoxidante|"
    r"adhesivo|base\s+de\s+silicona|alimenticio|lubriclav|"
    r"autoclave|nsf|equipo|mantenimiento|"
    r"oxida(?:nte|cion|ción)|antioxidante|antifriccion|antifricción)\b",
    re.IGNORECASE,
)

# 'Glicerina': sobre-asignada vía regla_diccionario a productos NO médicos
# (manómetros industriales, dispensadores Ecolab, jabones líquidos comerciales,
# DW-40). Criterio Carolina: solo glicerina/jabón médico. Verificado contra TPs
# para NO romper 'BASE JABÓN GLICERINA' y similares (no usan estas palabras).
VETO_GLICERINA_NO_MEDICA = re.compile(
    r"\b(?:manometro|manómetro|dw-?40|anticorrosivo|ecolab)\b",
    re.IGNORECASE,
)

# Antibióticos en FORMATO NO-MEDICAMENTO: tests, tiras reactivas, kits de
# susceptibilidad, antibiograma, cinta epsilométrica, cemento óseo dental.
# Aplica cuando el match identifica un antibiótico (lista_ANTIBIOTICOS) y la
# glosa trae señales claras de que NO es el fármaco. Generaliza los casos de
# Gentamicina (cemento óseo), Vancomicina (tiras E-test), y futuros.
VETO_ANTIBIOT_NO_MEDICA = re.compile(
    r"\b(?:tiras?|test|reactiv[oa]s?|epsilom\w+|e[\s\-]?test|"
    r"medidora|antibiograma|disco\s*de\s*susceptibilidad|"
    r"sensi\s*discos?|senci\s*discos?|"
    r"cemento\s*(?:[oóáa]seo)?|"
    r"kit\s+(?:de\s+|\d+\s+)?(?:tira|prueba|epsilom|test|sensi))\b",
    re.IGNORECASE,
)
# Lista pragmática de antibióticos del catálogo activo (se compara normalizado).
ANTIBIOTICOS = {
    "gentamicina", "vancomicina", "amikacina", "cefadroxilo",
    "piperacilina-tazobactam", "ampicilina", "ciprofloxacino",
    "ceftriaxona", "cefotaxima", "ceftazidima", "meropenem",
    "imipenem", "ertapenem", "clindamicina", "metronidazol",
    "azitromicina", "claritromicina", "eritromicina",
    "tetraciclina", "doxiciclina", "linezolid", "linezolida",
    "tobramicina", "kanamicina", "estreptomicina",
    "trimetoprima", "sulfametoxazol", "nitrofurantoina",
    "rifampicina", "isoniazida", "etambutol",
}

# Excipientes: lo que viene tras "Excip:" NO es el principio activo (es relleno:
# lactosa, hidróxido de aluminio, estearato de magnesio...). Matchear ahí causaba
# 'Fexofenadina ... Excip: ... aluminio ... magnesio' → 'Aluminio-Magnesio'.
_RE_EXCIPIENTES = re.compile(r"\bexcip\w*\s*[:.\-]", re.IGNORECASE)
# Vasoconstrictor ≈ epinefrina en anestésicos locales: habilita el pactivo
# COMBINADO (Mepivacaina-Epinefrina, Articaina-Epinefrina). Pero "SIN
# vasoconstrictor" es lo contrario → primero se elimina la negación.
_RE_SIN_VASO = re.compile(r"sin\s+(vaso\s*constrictor|epinefrina|adrenalina)", re.IGNORECASE)
_RE_CON_VASO = re.compile(r"(con\s+)?vaso\s*constrictor", re.IGNORECASE)


def _texto_para_match(descripcion: str) -> str:
    """Prepara la descripción SOLO para match_combinacion/match_diccionario:
    - corta la sección de excipientes (no es el principio activo);
    - normaliza 'con vasoconstrictor' → 'epinefrina' (habilita el combinado),
      respetando 'sin vasoconstrictor' (que NO debe armar el combinado)."""
    if not descripcion:
        return descripcion
    t = descripcion
    m = _RE_EXCIPIENTES.search(t)
    if m:
        t = t[:m.start()]
    t = _RE_SIN_VASO.sub(" ", t)        # "sin vasoconstrictor" → nada (anestésico simple)
    t = _RE_CON_VASO.sub(" epinefrina ", t)  # "con vasoconstrictor" → epinefrina (combinado)
    return t


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
    marcas_texto: str = "",
    modelo_marcas=None,
) -> Resultado:
    """Clasifica y aplica el VALIDADOR FINAL de composición: cualquier rama de la
    cascada (cruce verbatim, regla, modelo, Claude libre) puede dejar una comp que
    NO existe para el pactivo. `preclasificador.canonizar_comp` la confirma contra
    el catálogo del pactivo (corrige decimal 7.5↔7,5; manda a 'Sin cla' lo que no
    existe, p.ej. un volumen del envase). Punto único — vale para TODAS las ramas.

    `marcas_texto` (opcional) llega solo a Claude como bloque cacheable de
    `marcas → pactivo` inequívocas del catálogo activo (ver `marcas.cargar_marcas_para_prompt`)."""
    r = _clasificar_fila_impl(
        tabla, fila, taxonomia, pactivos_norm, descartes, cruce, combinaciones,
        modelo_descarte, ejemplos, indice_inverso, modelo_pactivo, marcas_texto,
        modelo_marcas,
    )
    # FINAL GUARD del PACTIVO: una rama puede haber dejado un pactivo que ya
    # NO está en el catálogo activo (cliente desactivado, decisión de negocio).
    # Caso medido 2026-06-02: 'Polividona Yodada', 'Yodo', 'Yodados', 'Yodo-
    # Potasio' siguen saliendo aunque ya no están en pactivos_norm — vía Claude
    # (no validaba) y vía regla_diccionario con combinaciones antiguas. Las
    # ramas cruce_base, historico y modelo_pactivo YA validan en su propio
    # cuerpo; este guard cierra las restantes. Anular y bajar a descarte; la
    # cola humana lo procesa como FN si corresponde.
    if r.interes == 1 and r.pactivo and normalizar(r.pactivo) not in pactivos_norm:
        r = Resultado(
            interes=0,
            pactivo=None,
            composicion=None,
            presentacion=None,
            confianza=r.confianza,
            metodo=f"{r.metodo}_pact_inactivo",
            razon=(f"Pactivo '{r.pactivo}' propuesto por {r.metodo} no está en "
                   f"el catálogo activo. " + (r.razon or "")),
        )
    if r.interes == 1 and r.pactivo:
        if r.composicion:
            r.composicion = preclasificador.canonizar_comp(
                taxonomia, tabla, r.pactivo, r.composicion
            )
        if r.presentacion:
            r.presentacion = preclasificador.canonizar_pres(
                taxonomia, tabla, r.pactivo, r.presentacion
            )
    return r


def _clasificar_fila_impl(
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
    marcas_texto: str = "",
    modelo_marcas=None,
) -> Resultado:
    descripcion = fila.get("Descripcion")
    titulo = fila.get("Titulo")
    vinculos = fila.get("VINCULOS")
    texto = f"{titulo or ''} {descripcion or ''}".strip()

    # Vetos duros por glosa — descartan ANTES de cualquier rama porque las
    # ramas baratas (cruce_base, regla_diccionario) matchearían el medicamento
    # o componente que viene EN el insumo y darían int=1 incorrecto. Migrados
    # 2026-06-05 desde regla 385 del prompt a código (cero costo extra y siempre
    # se aplican). Ver [[huecos-estructurales]] para el principio.
    desc_o = descripcion or ""
    _vetos = [
        (VETO_SENSIDISCOS, "veto_sensidiscos",
         "Sensidiscos (discos de antibiograma) — microbiología, no fármaco."),
        (VETO_STENT_Y_TIRAS, "veto_stent_tiras",
         "Stent o tira epsilométrica — insumo quirúrgico/lab, no fármaco."),
        (VETO_LAB_NO_FARMA, "veto_lab_no_farma",
         "Determinaciones moleculares / MGIT / kit diagnóstico — lab, no fármaco."),
        (VETO_OSTOMIA, "veto_ostomia",
         "Bolsa de ostomía — insumo de ostomía, no fármaco."),
        (VETO_TRAQUETUBO, "veto_traquetubo",
         "Traquetubo / tubo endotraqueal / cánula traqueal — instrumental."),
        (VETO_AGUA_OXIGENADA, "veto_agua_oxigenada",
         "Agua oxigenada / peróxido de hidrógeno — no se clasifica."),
        (VETO_TEST_SCHIRMER, "veto_test_schirmer",
         "Test de Schirmer — test oftalmológico, no fármaco."),
        (VETO_INSUMOS_PUNTUALES, "veto_insumos_puntuales",
         "Insumo quirúrgico/endodóntico puntual — no fármaco."),
    ]
    for regex, met, razon in _vetos:
        if regex.search(desc_o):
            return Resultado(
                interes=0, pactivo=None, composicion=None, presentacion=None,
                confianza=0.97, metodo=met, razon=razon,
            )
    # Jabón líquido: veto con EXCEPCIÓN. Si la glosa dice "jabón líquido" pero
    # también "alcohol gel", NO vetar (es jabón-alcohol = Alcohol Gel, válido).
    if VETO_JABON_NO_FARMA.search(desc_o) and not _JABON_EXCEPCION.search(desc_o):
        return Resultado(
            interes=0, pactivo=None, composicion=None, presentacion=None,
            confianza=0.97, metodo="veto_jabon_no_farma",
            razon="Jabón líquido / institucional / manos — no se clasifica.",
        )

    # Cruce Base — descripción idéntica a una OC REAL del catálogo 0001_td_oc.Base.
    # Va PRIMERO: protege a un producto médico real de un descarte por rubro.
    # Valores VERBATIM (de OC reales, no se canonizan).
    hit = cruce_base.buscar(cruce, descripcion)
    if hit:
        pactivo_b, comp_b, pres_b = hit
        # Validar contra catálogo ACTIVO: el cruce devuelve VERBATIM lo que
        # estaba en una OC histórica, pero ese pactivo puede haber sido REMOVIDO
        # del catálogo (cliente desactivado, decisión de negocio). Caso medido
        # 2026-06-02: 'Medio de Contraste'/'Guante'/'Iohexol' siguen apareciendo
        # vía cruce aunque ya no están en pactivos_norm. Si no está en el
        # catálogo activo, descartamos el hit y la cascada sigue a las siguientes
        # ramas. Igual molde que en la rama 'historico'.
        if pactivo_b and normalizar(pactivo_b) in pactivos_norm:
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
    # Validar contra catálogo ACTIVO: el histórico puede devolver un pactivo que
    # ya fue REMOVIDO del catálogo (caso medido 2026-06-01: 'Guante' no está en
    # Base ni en el diccionario filtrado por clientes activos, pero el histórico
    # humano lo seguía propagando 7 veces/día). Si el pactivo histórico no está
    # en pactivos_norm, lo descartamos y la cascada sigue a las siguientes ramas.
    if p and p.interes == 1 and p.pactivo and normalizar(p.pactivo) not in pactivos_norm:
        p = None
    if p:
        # CONFLICTO REVISAR: histórico de soporte BAJO (1-2 votos) gana como
        # interés, pero el modelo_descarte entrenado está MUY seguro de descarte.
        # Caso real medido 2026-05-29: "GUIA ANGIOPLASTIA" → Jeringas (histórico
        # 1x, descarte 0.99) — el humano de 1 vez se equivocó. En vez de auto-
        # aprobar el interés ciego, se marca como "pactivo nuevo / revisar"
        # (naranja, homologado al concepto pactivo nuevo) para que el revisor lo
        # mire. NO se descarta automático — a veces el histórico acierta y el
        # modelo se equivoca (caso ACIDO FOLINICO → Leucovorina, sinónimos).
        if p.interes == 1 and p.pactivo and p.soporte <= 2:
            p_desc = descarte_modelo.prob_descarte(modelo_descarte, descripcion)
            if p_desc >= config.umbral_modelo_descarte:
                return Resultado(
                    interes=1,
                    pactivo=None,
                    composicion=None,
                    presentacion=None,
                    confianza=round(p_desc, 3),
                    metodo="historico",
                    razon=(
                        f"CONFLICTO REVISAR: histórico ({p.soporte}x) sugiere "
                        f"'{p.pactivo}', pero el modelo de descarte está muy "
                        f"seguro de DESCARTE ({p_desc:.2f}). Revisar."
                    ),
                    pactivo_propuesto=p.pactivo,
                )
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
    desc_match = _texto_para_match(descripcion or "")
    pactivo = reglas.match_combinacion(desc_match, combinaciones or [])
    por_combinacion = pactivo is not None
    if not pactivo:
        pactivo = reglas.match_diccionario(desc_match, pactivos_norm)
    # Vetos puntuales: el match identificó un pactivo medible pero la glosa trae
    # señales claras de que es un PRODUCTO NO médico con ese nombre. Anular el
    # pactivo y dejar que la cascada siga (modelo_descarte → Claude). Igual
    # molde que VETO_CINTA_NO_MEDICA en la rama modelo_pactivo.
    if pactivo:
        pact_n = normalizar(pactivo)
        desc_lower = (descripcion or "")
        if pact_n == normalizar("Glicerina") and VETO_GLICERINA_NO_MEDICA.search(desc_lower):
            pactivo = None
        elif pact_n == normalizar("Lubricante") and VETO_LUBRICANTE_NO_INTIMO.search(desc_lower):
            pactivo = None
        elif pact_n in ANTIBIOTICOS and VETO_ANTIBIOT_NO_MEDICA.search(desc_lower):
            pactivo = None
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
        # Veto puntual: el modelo sobre-asigna 'Cinta Adhesiva Médica' a cintas
        # de oficina/ferretería/embalaje. Si la glosa trae esas señales, se deja
        # que la cascada siga (modelo_descarte → Claude con la regla restrictiva).
        if (normalizar(pact_pred) == normalizar("Cinta Adhesiva Médica")
                and VETO_CINTA_NO_MEDICA.search(descripcion or "")):
            pact_pred = None
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

    # Modelo de MARCAS por contexto (R2 Etapa 2 — 2026-06-05). Complementa al
    # modelo_pactivo: entrenado SOLO con glosas donde el pactivo no aparece
    # literal y se infiere de la marca + título + vínculos. Capa específica
    # para marcas comerciales (Acerdil, Eutirox, Micardis, Cardioplus, etc.).
    # Dos umbrales: alto → asignación normal verde; bajo → "posiblemente de
    # interés" (amarillo) que el revisor debe completar antes de aprobar.
    if modelo_marcas is not None:
        pact_m, conf_m = mm.predecir(modelo_marcas, titulo or "", descripcion or "", vinculos or "")
        if (pact_m
                and normalizar(pact_m) not in {normalizar(p) for p in PACTIVOS_NO_MATCH_DIRECTO}
                and normalizar(pact_m) in pactivos_norm):
            # Mismo veto Cinta Adhesiva que aplica al modelo_pactivo
            if (normalizar(pact_m) == normalizar("Cinta Adhesiva Médica")
                    and VETO_CINTA_NO_MEDICA.search(descripcion or "")):
                pact_m = None
        if (pact_m
                and normalizar(pact_m) not in {normalizar(p) for p in PACTIVOS_NO_MATCH_DIRECTO}
                and normalizar(pact_m) in pactivos_norm):
            if conf_m >= config.umbral_marcas_alto:
                # Verde — asignación normal
                comp_o, pres_o = preclasificador.elegir_comp_pres_por_descripcion(
                    tabla, pact_m, descripcion
                )
                comp_h, pres_h = preclasificador.comp_pres_por_pactivo(tabla, pact_m)
                return Resultado(
                    interes=1,
                    pactivo=pact_m,
                    composicion=comp_o or comp_h,
                    presentacion=pres_o or pres_h,
                    confianza=round(conf_m, 3),
                    metodo="modelo_marcas",
                    razon=f"Clasificador de marcas/contexto (probabilidad {conf_m:.2f}).",
                )
            if conf_m >= config.umbral_marcas_bajo:
                # Amarillo — "posiblemente de interés". El revisor debe
                # completar pact+comp+pres (regla de oro lo obliga). El
                # método '_posible' lo distingue en el filtro del panel.
                return Resultado(
                    interes=1,
                    pactivo=pact_m,
                    composicion=None,
                    presentacion=None,
                    confianza=round(conf_m, 3),
                    metodo="modelo_marcas_posible",
                    razon=(f"Sugerencia por contexto (probabilidad {conf_m:.2f}) — "
                           f"REVISAR antes de aprobar."),
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
        candidatos=candidatos, marcas_texto=marcas_texto,
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
