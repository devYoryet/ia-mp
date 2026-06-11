"""Carga las REGLAS DE NEGOCIO y las CORRECCIONES que el equipo agrega desde el
panel (tabla `clasificador_ia_reglas`) y las arma como bloques para el system
prompt.

Dos niveles, a propósito con distinto PESO:
- 'regla'      — guía general del equipo.
- 'correccion' — un error puntual que la IA ya cometió, con el "por qué". Va en
  una sección aparte, con encabezado enfático y al final del prompt (más cerca
  de la consulta = más peso) para no repetirlo."""

from __future__ import annotations

from db import conexion_worker

# IMPORTANTE: dos consultas separadas, NO un solo SELECT con LIMIT. Las
# correcciones se agregan a diario (miles) y las reglas son pocas y estables;
# un `WHERE activa=1 ORDER BY creado_en DESC LIMIT 120` único dejaba el top-120
# lleno de correcciones recientes y las reglas NUNCA entraban al prompt
# (medido 2026-06-11: 0/14 reglas en el top-120). Reglas = TODAS las activas
# (son pocas); correcciones = las `limite` más recientes.
_SQL_REGLAS = (
    "SELECT texto FROM clasificador_ia_reglas "
    "WHERE activa = 1 AND tipo = 'regla' ORDER BY creado_en DESC"
)
_SQL_CORRECCIONES = (
    "SELECT texto FROM clasificador_ia_reglas "
    "WHERE activa = 1 AND tipo = 'correccion' ORDER BY creado_en DESC LIMIT %s"
)


def cargar_feedback(limite: int = 120) -> str:
    """Bloque de feedback humano para el system prompt. '' si no hay nada."""
    try:
        conn = conexion_worker()
        with conn.cursor() as cur:
            cur.execute(_SQL_REGLAS)
            reglas = [f["texto"] for f in cur.fetchall()]
            cur.execute(_SQL_CORRECCIONES, (limite,))
            correcciones = [f["texto"] for f in cur.fetchall()]
    except Exception:
        return ""

    bloques = []
    if reglas:
        bloques.append(
            "REGLAS DE NEGOCIO — criterio del equipo, aplícalas siempre:\n"
            + "\n".join(f"- {r}" for r in reglas)
        )
    if correcciones:
        # sección aparte, con más peso: errores ya cometidos
        bloques.append(
            "### ERRORES YA COMETIDOS — MÁXIMA PRIORIDAD, NO LOS REPITAS ###\n"
            "Cada línea es un error real que cometiste antes y por qué estuvo mal:\n"
            + "\n".join(f"- {c}" for c in correcciones)
        )
    return "\n\n".join(bloques)
