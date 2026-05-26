"""Carga el catálogo de pactivos, composiciones y presentaciones.

Fuente PRIMARIA: `0001_td_oc.Base` (OC reales; columnas `Pactivo`, `Comp`=
composición, `MedidaPHT`=presentación).
Fuente SECUNDARIA: la tabla `diccionario` de clasificación (columnas `pactivo`,
`comp`, `presentacion`) — el MISMO diccionario que usa el legacy de
gestor_licitaciones. Aporta los pactivos que NO están en el catálogo primario.

Usa la conexión compartida del worker."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from catalogo_activo import WHITELIST, construir_filtro_activo
from config import config
from db import conexion_worker
from reglas import normalizar, normalizar_valor

_TABLA_CATALOGO = "Base"

# Dosis dentro de una glosa: número (decimales y rangos) + unidad.
# Ej.: "100mg", "0,12%", "5-10mg", "10 mg/ml", "500 ml". El orden de las
# unidades importa — las compuestas (mg/ml) van antes que las simples (mg).
_RE_DOSIS = re.compile(
    r"\d+(?:[.,]\d+)?(?:\s*-\s*\d+(?:[.,]\d+)?)?\s*"
    r"(?:mg/ml|mg/g|ui/ml|mg|mcg|ug|ui|%|g/l|g|ml|cc)\b",
    re.IGNORECASE,
)
_RE_VOLUMEN = re.compile(r"^\d[\d.,\s-]*(ml|cc)$", re.IGNORECASE)

# Preparado magistral / reactivo: glosa tipo "PQ <droga> <rango> MG" (preparación
# de quimioterapia a medida). El rango de mg es un bracket de CANTIDAD del lote,
# NO una concentración — un preparado a medida no tiene dosis fija. Para esos la
# composición es el comodín «Sin Cla» (match no-estricto). Verificado 2026-05-22.
_RE_MAGISTRAL = re.compile(r"\bpq\b.{0,40}\d+\s*-\s*\d+\s*mg\b", re.IGNORECASE)
_COMODIN = "Sin Cla"


@dataclass
class Taxonomia:
    pactivos: list[str] = field(default_factory=list)
    composiciones: list[str] = field(default_factory=list)
    presentaciones: list[str] = field(default_factory=list)
    # índices globales {valor_normalizado_sin_espacios: valor_canónico}
    comp_index: dict = field(default_factory=dict)
    pres_index: dict = field(default_factory=dict)

    def texto_para_prompt(self) -> str:
        """Bloque estable para el system prompt (se cachea con prompt caching)."""
        return (
            "LISTA CONTROLADA DE PACTIVOS — debes elegir EXACTAMENTE uno de esta "
            "lista, o null si ninguno corresponde:\n"
            + "\n".join(f"- {p}" for p in self.pactivos)
            + "\n\nPRESENTACIONES VÁLIDAS:\n"
            + ", ".join(self.presentaciones)
            + "\n\nLa composición es la dosis o concentración (ej.: 500mg, 0,12%, "
            "40-12,5mg). Extraéla de la descripción."
        )

    def snap(self, pactivo: str | None, comp: str | None, pres: str | None) -> tuple:
        """Canoniza composición/presentación SIN destruir valores válidos:
        - si el valor de la IA es un valor conocido del catálogo, usa su forma canónica;
        - si es uno nuevo (no visto), lo deja tal cual (es válido igual);
        - si viene vacío, usa «Sin Clas» (lo que las personas ponen para 'sin dato')."""
        return (_snap(comp, self.comp_index), _snap(pres, self.pres_index))

    def extraer_de_glosa(self, texto: str) -> tuple:
        """Lee composición (dosis) y presentación (forma) DIRECTAMENTE de la
        glosa y las canoniza con snap. Devuelve (comp|None, pres|None) — None
        cuando la glosa no la indica (ahí la cascada usa el respaldo histórico).

        Sirve para la Etapa 2: cuando el match del diccionario da el pactivo, la
        dosis y la forma suelen estar escritas en el propio texto (ej.
        'KETOPROFENO 100 MG AMPOLLA') — leerlas de ahí es más fiel que el valor
        más frecuente histórico del pactivo."""
        t = normalizar(texto or "")
        if not t:
            return (None, None)
        # presentación: ¿aparece (como palabra, admitiendo plural) alguna del catálogo?
        halladas = sorted(
            pres for pres in self.presentaciones
            if normalizar(pres)
            and re.search(rf"\b{re.escape(normalizar(pres))}s?\b", t)
        )
        # varias coincidencias que son la MISMA forma (singular/plural duplicados
        # en el catálogo) NO son ambiguas; formas distintas sí → respaldo histórico.
        formas = {normalizar(p).rstrip("s") for p in halladas}
        pres = halladas[0] if (halladas and len(formas) == 1) else None
        pres_final = _snap(pres, self.pres_index) if pres else None

        # preparado magistral: el rango de mg NO es una concentración → comodín.
        if _RE_MAGISTRAL.search(t):
            return (_COMODIN, pres_final)

        # composición: la dosis más relevante (se prefiere mg/UI/% sobre el volumen ml)
        dosis = [m.group(0).strip() for m in _RE_DOSIS.finditer(t)]
        comp = None
        if dosis:
            fuertes = [d for d in dosis if not _RE_VOLUMEN.match(d)]
            comp = (fuertes or dosis)[0]
        return (_snap(comp, self.comp_index) if comp else None, pres_final)


def _snap(valor: str | None, indice: dict) -> str:
    if not valor or not valor.strip():
        return "Sin Clas"
    return indice.get(normalizar_valor(valor)) or valor.strip()


def cargar_taxonomia() -> Taxonomia:
    conn = conexion_worker()
    pactivos: set[str] = set()
    comp_index: dict[str, str] = {}
    pres_index: dict[str, str] = {}

    def _registrar(p, c, m):
        if p and p.strip():
            pactivos.add(p.strip())
        if c and c.strip():
            comp_index.setdefault(normalizar_valor(c), c.strip())
        if m and m.strip():
            pres_index.setdefault(normalizar_valor(m), m.strip())

    with conn.cursor() as cur:
        # 1) catálogo PRIMARIO — 0001_td_oc.Base
        cat = config.db_catalogo
        cur.execute(
            f"SELECT DISTINCT Pactivo AS p, Comp AS c, MedidaPHT AS m "
            f"FROM `{cat}`.`{_TABLA_CATALOGO}` WHERE Pactivo IS NOT NULL AND Pactivo <> ''"
        )
        primarios = {normalizar(f["p"]) for f in cur.fetchall() if f["p"]}
        cur.execute(
            f"SELECT DISTINCT Pactivo AS p, Comp AS c, MedidaPHT AS m "
            f"FROM `{cat}`.`{_TABLA_CATALOGO}` WHERE Pactivo IS NOT NULL AND Pactivo <> ''"
        )
        for f in cur.fetchall():
            _registrar(f["p"], f["c"], f["m"])

        # 2) catálogo SECUNDARIO — diccionario de clasificación (el mismo del
        #    legacy); aporta solo los pactivos que NO están en el primario, Y
        #    que aparezcan en al menos un cliente ACTIVO (o estén en la
        #    whitelist de meta-pactivos como "Adjunto"). El cruce con
        #    pharmatender.company (prime) + principal_app.diccionario_unidad
        #    se hace en `catalogo_activo.construir_filtro_activo()`.
        filtro_activo = construir_filtro_activo()
        dic = config.db_diccionario
        cur.execute(
            f"SELECT DISTINCT pactivo AS p, comp AS c, presentacion AS m "
            f"FROM `{dic}`.diccionario WHERE pactivo IS NOT NULL AND pactivo <> ''"
        )
        whitelist_norm = {normalizar(p) for p in WHITELIST}
        n_descartados = 0
        for f in cur.fetchall():
            pn = normalizar(f["p"])
            if pn in primarios:
                # ya está en 0001_td_oc.Base (sagrado) — pero el dicc puede
                # aportar otra combinación de comp/pres, igual la registramos.
                _registrar(f["p"], f["c"], f["m"])
                continue
            # delta: solo si está en filtro_activo o whitelist
            if filtro_activo is not None and pn not in filtro_activo and pn not in whitelist_norm:
                n_descartados += 1
                continue
            _registrar(f["p"], f["c"], f["m"])

    # Construye Taxonomia y reporta el efecto del filtro
    tax = Taxonomia(
        pactivos=sorted(pactivos),
        composiciones=sorted(set(comp_index.values())),
        presentaciones=sorted(set(pres_index.values())),
        comp_index=comp_index,
        pres_index=pres_index,
    )
    if filtro_activo is not None:
        import logging
        logging.getLogger("taxonomia").info(
            "Filtro de clientes activos aplicado: %d pactivos descartados del delta. "
            "Catálogo final: %d pactivos.",
            n_descartados, len(tax.pactivos),
        )
    return tax
