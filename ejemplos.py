"""Carga ejemplos ya validados por humanos (feedback) para usarlos como few-shot.

Es el mecanismo de mejora continua: cada corrección en el panel de revisión
alimenta el criterio del clasificador. Se cargan una vez al iniciar el worker."""

from __future__ import annotations

from db import conexion_worker

_SQL = """
SELECT descripcion, interes_sugerido, pactivo_sugerido,
       feedback_correcto, feedback_pactivo
FROM clasificador_ia_log
WHERE revisado = 1 AND descripcion IS NOT NULL AND descripcion <> ''
ORDER BY revisado_en DESC
LIMIT %s
"""


def cargar_ejemplos(limite: int = 40) -> str:
    """Bloque de texto con ejemplos validados para el system prompt. '' si no hay."""
    try:
        conn = conexion_worker()
        with conn.cursor() as cur:
            cur.execute(_SQL, (limite,))
            filas = cur.fetchall()
    except Exception:
        return ""

    if not filas:
        return ""
    lineas = []
    for f in filas:
        pactivo = f.get("feedback_pactivo") or f.get("pactivo_sugerido") or "—"
        desc = (f.get("descripcion") or "")[:160]
        lineas.append(f'- "{desc}" => interes={f.get("interes_sugerido")}, pactivo={pactivo}')
    return (
        "EJEMPLOS DE CLASIFICACIONES YA VALIDADAS POR HUMANOS — sigue este criterio:\n"
        + "\n".join(lineas)
    )
