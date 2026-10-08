"""Motor de la Fase 2 (Device / Servicio técnico) — puro, sin base de datos.

Decide si una fila que la fase 1 DESCARTÓ pertenece a una categoría de la fase 2
con dos señales independientes:

  - PALABRA: un término de la categoría calza en la glosa (`Descripcion`).
    Un término puede exigir CONTEXTO, que se busca en la glosa o en el título
    (p. ej. "mantención" sólo cuenta si habla de un equipo médico). Sin su
    contexto, el término es DÉBIL.
  - ONU: el código UNSPSC de la fila está en la lista de la categoría, con
    fuerza 'fuerte' (identifica la categoría por sí solo) o 'confirma' (sólo
    confirma una palabra).

    T  = término con contexto, sin exclusión      Td = término sin su contexto
    Of = ONU fuerte                               Oc = ONU fuerte o confirma
    verde     si T y Oc                             → señal 'ambas'
    revision  si T y no Oc                          → 'solo_palabra'
    revision  si Td y Of                            → 'palabra_debil+onu'
    revision  si Of, sin T ni Td, y la categoría
              tiene onu_solo_a_revision             → 'solo_onu'

Una exclusión que calza anula la categoría completa. Si calzan varias
categorías gana la primera en verde (por prioridad); si ninguna está en verde,
la primera en revisión. Las demás quedan en `otras`.

La configuración llega como datos (dicts), nunca escrita en este archivo: vive
en las tablas clasificador_f2_* y se edita sin deploy. Ver docs/06-fase2-device.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass

log = logging.getLogger("fase2.motor")

FUERZA = {"fuerte": 2, "confirma": 1}
_ESPACIOS = re.compile(r"\s+")


def normalizar(texto: str | None) -> str:
    """Sin tildes, minúsculas y espacios colapsados. Las regex de la
    configuración se escriben contra este texto (sin tildes)."""
    s = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return _ESPACIOS.sub(" ", s.lower()).strip()


@dataclass(frozen=True)
class Termino:
    nombre: str
    regex: re.Pattern
    contexto: re.Pattern | None


@dataclass(frozen=True)
class Categoria:
    codigo: str
    linea: str
    nombre: str
    prioridad: int
    onu_solo_a_revision: bool
    terminos: tuple[Termino, ...]
    excluye: tuple[tuple[str, re.Pattern], ...]
    onu: tuple[tuple[str, int], ...]  # (prefijo, fuerza) — el prefijo más largo primero


@dataclass(frozen=True)
class Resultado:
    categoria: str
    linea: str
    categoria_nombre: str
    estado_auto: str          # 'verde' | 'revision'
    senal: str                # 'ambas' | 'solo_palabra' | 'palabra_debil+onu' | 'solo_onu'
    terminos: tuple[str, ...]
    onu_prefijo: str | None
    otras: tuple[str, ...] = ()

    @property
    def subcategoria(self) -> str | None:
        """El primer término que calzó. Cada categoría declara sus términos del
        más específico al más genérico."""
        return self.terminos[0] if self.terminos else None


def compilar(categorias: list[dict]) -> tuple[list[Categoria], list[str]]:
    """Compila la configuración. Un término con regex inválida se SALTEA y se
    informa (mismo criterio que los vetos dinámicos: un typo no tumba el resto).
    Devuelve (categorías ordenadas por prioridad, errores)."""
    errores: list[str] = []

    def _re(patron: str, donde: str) -> re.Pattern | None:
        try:
            return re.compile(patron)
        except re.error as exc:
            errores.append(f"{donde}: regex inválida ({exc})")
            return None

    out: list[Categoria] = []
    for c in categorias:
        if not c.get("activa", True):
            continue
        cod = c["codigo"]
        terms = []
        for t in c.get("terminos", []):
            if not t.get("activa", True):
                continue
            rx = _re(t["regex"], f"{cod}/{t['nombre']}")
            cx = _re(t["contexto"], f"{cod}/{t['nombre']} (contexto)") if t.get("contexto") else None
            if rx is None or (t.get("contexto") and cx is None):
                continue
            terms.append(Termino(t["nombre"], rx, cx))
        excl = []
        for e in c.get("excluye", []):
            if not e.get("activa", True):
                continue
            rx = _re(e["regex"], f"{cod}/excluye {e['nombre']}")
            if rx is not None:
                excl.append((e["nombre"], rx))
        onu = []
        for o in c.get("onu", []):
            if not o.get("activa", True):
                continue
            if o["fuerza"] not in FUERZA:
                errores.append(f"{cod}/onu {o['codigo']}: fuerza desconocida '{o['fuerza']}'")
                continue
            onu.append((str(o["codigo"]).strip(), FUERZA[o["fuerza"]]))
        onu.sort(key=lambda x: -len(x[0]))
        out.append(Categoria(cod, c["linea"], c["nombre"], int(c["prioridad"]),
                             bool(c["onu_solo_a_revision"]), tuple(terms), tuple(excl), tuple(onu)))
    out.sort(key=lambda c: c.prioridad)
    for e in errores:
        log.warning(e)
    return out, errores


def _fuerza_onu(cat: Categoria, cod: str) -> tuple[int, str | None]:
    for pref, f in cat.onu:
        if cod.startswith(pref):
            return f, pref
    return 0, None


def _evaluar_categoria(cat: Categoria, glosa: str, titulo: str, cod: str):
    for _n, rx in cat.excluye:
        if rx.search(glosa):
            return None
    fuertes, debiles = [], []
    for t in cat.terminos:
        if not t.regex.search(glosa):
            continue
        if t.contexto is None or t.contexto.search(glosa) or t.contexto.search(titulo):
            fuertes.append(t.nombre)
        else:
            debiles.append(t.nombre)
    f, pref = _fuerza_onu(cat, cod) if cod else (0, None)
    if fuertes and f:
        return "verde", "ambas", fuertes, pref
    if fuertes:
        return "revision", "solo_palabra", fuertes, None
    if debiles and f == FUERZA["fuerte"]:
        return "revision", "palabra_debil+onu", debiles, pref
    if f == FUERZA["fuerte"] and cat.onu_solo_a_revision:
        return "revision", "solo_onu", [], pref
    return None


def evaluar(categorias: list[Categoria], descripcion: str | None, titulo: str | None,
            codigo_onu: str | None) -> Resultado | None:
    """Evalúa una fila. `categorias` es la salida de `compilar`."""
    glosa, tit, cod = normalizar(descripcion), normalizar(titulo), (codigo_onu or "").strip()
    hits = []
    for cat in categorias:  # ya vienen por prioridad
        r = _evaluar_categoria(cat, glosa, tit, cod)
        if r:
            hits.append((cat, r))
    if not hits:
        return None
    hits.sort(key=lambda h: 0 if h[1][0] == "verde" else 1)  # estable: respeta prioridad
    cat, (estado, senal, terms, pref) = hits[0]
    return Resultado(cat.codigo, cat.linea, cat.nombre, estado, senal, tuple(terms), pref,
                     tuple(h[0].codigo for h in hits[1:]))


def version(categorias: list[dict]) -> str:
    """Huella de la configuración ACTIVA. Cambia si cambia cualquier regex,
    código ONU, fuerza, prioridad o flag; el servicio la usa para re-evaluar."""
    def _activos(xs):
        return [x for x in xs if x.get("activa", True)]
    canon = [
        {
            "codigo": c["codigo"], "linea": c["linea"], "nombre": c["nombre"],
            "prioridad": int(c["prioridad"]), "onu_solo": bool(c["onu_solo_a_revision"]),
            "t": [(t["nombre"], t["regex"], t.get("contexto")) for t in _activos(c.get("terminos", []))],
            "e": sorted((e["nombre"], e["regex"]) for e in _activos(c.get("excluye", []))),
            "o": sorted((str(o["codigo"]), o["fuerza"]) for o in _activos(c.get("onu", []))),
        }
        for c in sorted(_activos(categorias), key=lambda c: c["codigo"])
    ]
    return hashlib.sha1(json.dumps(canon, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
