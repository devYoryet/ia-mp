"""Etapa 1 — filtro barato sin IA: normalización y match contra el diccionario
controlado. Conservador: solo resuelve casos INEQUÍVOCOS; el resto pasa a Claude."""

from __future__ import annotations

import re
import unicodedata


def normalizar(texto: str | None) -> str:
    """Minúsculas, sin tildes, espacios colapsados — para comparar texto."""
    if not texto:
        return ""
    sin_tildes = (
        unicodedata.normalize("NFKD", texto)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return re.sub(r"\s+", " ", sin_tildes.lower()).strip()


def indexar_pactivos(pactivos: list[str]) -> dict[str, str]:
    """{pactivo_normalizado: pactivo_original} para el match exacto."""
    return {normalizar(p): p for p in pactivos}


def normalizar_valor(texto: str | None) -> str:
    """Como normalizar() pero SIN espacios — para comparar composición/presentación
    (así '500 mg' y '500mg' se consideran el mismo valor)."""
    return normalizar(texto).replace(" ", "")


# 'Sin Cla' (SIN s) — comodín que escribe el clasificador cuando no se puede
# determinar la composición/presentación. NO es un valor del catálogo: es un
# token de match NO ESTRICTO, para que la oportunidad igual le llegue al cliente
# aunque la dosis no figure en mercadopublico.cl. Distinto de 'Sin Clas' (CON s),
# que sí es una categoría real y literal del diccionario (match estricto).
SIN_CLA = "Sin Cla"


def match_diccionario(texto: str, pactivos_norm: dict[str, str]) -> str | None:
    """Devuelve el pactivo SOLO si EXACTAMENTE uno del diccionario calza como
    palabra completa. Si calzan 0 o varios, devuelve None y lo decide Claude.

    Conservador a propósito: el backtest mostró que elegir entre varios produce
    falsos positivos (p. ej. 'salbutamol sol nebulizado' elegía 'Nebulizador')."""
    desc = normalizar(texto)
    if not desc:
        return None
    encontrados: set[str] = set()
    for pnorm, poriginal in pactivos_norm.items():
        if len(pnorm) < 6:  # pactivos muy cortos -> falsos positivos
            continue
        if re.search(rf"\b{re.escape(pnorm)}\b", desc):
            encontrados.add(poriginal)
            if len(encontrados) > 1:
                return None  # ambiguo -> que lo decida Claude
    return next(iter(encontrados)) if encontrados else None


def indexar_combinaciones(pactivos: list[str]) -> list[tuple]:
    """Lista de (pactivo, [tokens]) para los pactivos COMBINADOS del catálogo
    (los que llevan '-'). Cada token es la palabra más distintiva de un
    componente. Sirve para reconocer un combinado aunque la glosa traiga los
    componentes en otro orden o abreviados ('amoxicilina 875mg + ac clavulánico')."""
    combos = []
    for p in pactivos:
        partes = [x.strip() for x in p.split("-") if x.strip()]
        if len(partes) < 2:
            continue
        tokens = []
        for parte in partes:
            palabras = [w for w in normalizar(parte).split() if len(w) >= 5]
            tokens.append(max(palabras, key=len) if palabras else "")
        if all(len(tok) >= 5 for tok in tokens):
            combos.append((p, tokens))
    return combos


def match_combinacion(texto: str, combinaciones: list) -> str | None:
    """Pactivo COMBINADO del catálogo si la glosa contiene TODOS sus componentes
    (como palabras completas), sin importar el orden en que vengan en la glosa.

    El resultado es el pactivo del catálogo VERBATIM — el orden de los
    componentes lo fija el diccionario central, NO la glosa (en el catálogo es
    'Amoxicilina-Acido Clavulanico', nunca al revés). Ambiguo (varios) -> None."""
    desc = normalizar(texto)
    if not desc:
        return None
    encontrados = [
        pactivo for pactivo, tokens in combinaciones
        if all(re.search(rf"\b{re.escape(tok)}\b", desc) for tok in tokens)
    ]
    return encontrados[0] if len(encontrados) == 1 else None
