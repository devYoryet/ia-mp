"""Reglas puras de conciliación del barrido (sin BD, testeables).

Para cada fila origen del rango barrido se compara lo que HAY en
clasificador_f2_resultado (`previo`) con lo que DEBERÍA haber según el estado
efectivo de la fila y la evaluación del motor:

- Estado efectivo = `estado_gestor` si alguien (persona o bot del legacy) ya
  decidió; si no, lo que sugirió la fase 1 en clasificador_ia_log; si no hay
  ninguno, la fila está pendiente y no se toca.
- Entra lo DESCARTADO (estado efectivo 0). Una fila de interés farma (estado
  efectivo 1) entra SÓLO si también es Device con las dos señales (p. ej. un
  apósito que farma clasificó con pactivo 'Apósito'): queda como estado_auto
  'farma', informativa y sin revisión. Si no, su registro se anula: farma manda.
- La decisión humana de la fase 2 (decision / revisado_* / motivo) NUNCA se
  pisa. Una fila revisada sólo cambia su vigencia y sus datos informativos.
"""

from __future__ import annotations

from motor import Resultado

MOTIVO_FARMA = "pasó a interés farma"
MOTIVO_REGLAS = "ya no calza con las reglas vigentes"

CAMPOS_INFO = ("licitacion", "fecha_publicacion", "fecha_cierre", "descripcion", "titulo",
               "codigo_onu", "nombre_onu", "ia_interes", "ia_metodo", "estado_gestor", "clasificador_f1",
               "pactivo_f1")
CAMPOS_AUTO = ("categoria", "linea", "categoria_nombre", "subcategoria", "terminos", "senal",
               "estado_auto", "otras_categorias")


def estado_efectivo(estado_gestor, ia_interes) -> int | None:
    if estado_gestor is not None:
        return int(estado_gestor)
    if ia_interes is not None:
        return int(ia_interes)
    return None


def campos_auto(r: Resultado, estado_auto: str | None = None) -> dict:
    """`estado_auto` fuerza el estado (el barrido pasa 'farma' para filas de interés farma)."""
    return {
        "categoria": r.categoria,
        "linea": r.linea,
        "categoria_nombre": r.categoria_nombre,
        "subcategoria": r.subcategoria[:120] if r.subcategoria else None,
        "terminos": ", ".join(r.terminos)[:500] or None,
        "senal": r.senal,
        "estado_auto": estado_auto or r.estado_auto,
        "otras_categorias": ", ".join(r.otras)[:200] or None,
    }


def _norm(v):
    return None if v == "" else v


def _distinto(a, b) -> bool:
    return _norm(a) != _norm(b)


def reconciliar(previo: dict | None, estado_ef: int | None, auto: dict | None,
                info: dict, version: str) -> tuple[str, dict]:
    """Devuelve (accion, cambios). accion ∈ nada | insertar | actualizar |
    reactivar | anular | info. `cambios` son las columnas a escribir."""
    if estado_ef is None:
        return "nada", {}
    if previo is None:
        if auto:
            return "insertar", {**info, **auto, "version_reglas": version,
                                "vigente": 1, "motivo_no_vigente": None}
        return "nada", {}

    cambios = {k: v for k, v in info.items() if _distinto(previo.get(k), v)}
    revisado = previo.get("decision") is not None
    vigente = bool(previo.get("vigente"))

    if auto is None:
        if estado_ef == 1 and vigente:  # rescatada como farma y no es Device con ambas señales
            cambios.update(vigente=0, motivo_no_vigente=MOTIVO_FARMA)
            return "anular", cambios
        if estado_ef == 0 and vigente and not revisado:  # ya no calza con las reglas
            cambios.update(vigente=0, motivo_no_vigente=MOTIVO_REGLAS)
            return "anular", cambios
        return ("info" if cambios else "nada"), cambios  # la decisión humana se respeta

    if revisado:
        if not vigente:
            cambios.update(vigente=1, motivo_no_vigente=None)
            return "reactivar", cambios
        return ("info" if cambios else "nada"), cambios

    auto_c = {k: v for k, v in auto.items() if _distinto(previo.get(k), v)}
    if auto_c or not vigente:
        cambios.update(auto_c)
        cambios.update(version_reglas=version, vigente=1, motivo_no_vigente=None)
        return ("reactivar" if not vigente else "actualizar"), cambios
    return ("info" if cambios else "nada"), cambios
