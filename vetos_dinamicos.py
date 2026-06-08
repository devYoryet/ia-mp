"""Vetos dinámicos cargados desde `clasificador_ia_reglas` (tipo='veto').

CRUD desde /reglas (panel). NO se inyectan al prompt de Claude — la cascada
los aplica directamente como regex. Cero costo extra de tokens.

Protección:
- Si la BD falla, devuelve None y la cascada usa los hardcoded de seguridad.
- Si una regex no compila (typo del editor), se SALTEA con warning — no
  rompe el resto.
- Cambios en la tabla se detectan por el versionado (MAX(creado_en) + count)
  igual que pactivos_extra.
"""
from __future__ import annotations

import logging
import re

from db import conexion_worker

log = logging.getLogger("vetos_dinamicos")


def cargar_vetos() -> "dict[str, list] | None":
    """Devuelve un dict: {aplica_a: [(nombre, compiled_regex, pactivo_filtro,
    razon), ...]}. Si la BD falla, None — fallback a hardcoded.

    `aplica_a` puede ser un string (una rama) o coma-separado (varias ramas).
    El llamador desempaqueta y pone el mismo veto en cada rama indicada.
    """
    try:
        conn = conexion_worker()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, nombre, regex_pattern, aplica_a, pactivo_filtro, texto "
                "FROM clasificador_ia_reglas "
                "WHERE tipo='veto' AND activa=1"
            )
            filas = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo cargar vetos desde BD (%s) — fallback a hardcoded", exc)
        return None

    por_rama: "dict[str, list]" = {}
    n_total = 0
    n_invalida = 0
    for f in filas:
        nombre = (f["nombre"] or f"veto_{f['id']}").strip()
        patron = (f["regex_pattern"] or "").strip()
        if not patron:
            log.warning("Veto id=%s (%s) sin regex_pattern — salteado", f["id"], nombre)
            continue
        try:
            cre = re.compile(patron, re.IGNORECASE)
        except re.error as exc:
            log.warning("Veto id=%s (%s) regex inválida (%s) — salteado",
                        f["id"], nombre, exc)
            n_invalida += 1
            continue
        ramas = [r.strip() for r in (f["aplica_a"] or "").split(",") if r.strip()]
        if not ramas:
            ramas = ["inicio_cascada"]
        razon = (f["texto"] or "").strip() or f"Veto {nombre}"
        for rama in ramas:
            por_rama.setdefault(rama, []).append((nombre, cre, f["pactivo_filtro"], razon))
            n_total += 1

    log.info("Vetos cargados desde BD: %d aplicaciones en %d ramas (%d inválidos)",
             n_total, len(por_rama), n_invalida)
    return por_rama


def version() -> str:
    """Hash del estado actual de la tabla, para detectar cambios y recargar."""
    try:
        with conexion_worker().cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) n, COALESCE(MAX(creado_en), 0) m, SUM(activa) a "
                "FROM clasificador_ia_reglas WHERE tipo='veto'"
            )
            r = cur.fetchone()
            return f"{r['n']}|{r['m']}|{r['a']}"
    except Exception:
        return ""
