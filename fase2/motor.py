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
    revision  si NINGUNA categoría calzó en la glosa, pero un término (con su
              contexto) está en el TÍTULO y el ONU calza (Oc) → 'titulo+onu'.
              El título es el paraguas de toda la licitación (lección de la
              fase 1): nunca da verde por sí solo.

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
# Cambia cuando cambia la LÓGICA del motor (no la configuración). Entra en la
# huella de `version`, así el servicio re-evalúa la ventana tras un deploy.
#   1 = v1.1 (2026-10-08)  ·  2 = + señal 'titulo+onu'
#   3 = + 'titulo' sin ONU (categorías con titulo_solo_a_revision) y exclusiones
#       que no aplican cuando el ONU es fuerte (salvo_onu_fuerte)
MOTOR_VERSION = "3"
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
    excluye: tuple[tuple[str, re.Pattern, bool], ...]  # (nombre, regex, salvo_onu_fuerte)
    onu: tuple[tuple[str, int], ...]  # (prefijo, fuerza) — el prefijo más largo primero
    titulo_solo_a_revision: bool = False  # término en el título SIN ONU → revisión


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
                excl.append((e["nombre"], rx, bool(e.get("salvo_onu_fuerte"))))
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
                             bool(c["onu_solo_a_revision"]), tuple(terms), tuple(excl), tuple(onu),
                             bool(c.get("titulo_solo_a_revision"))))
    out.sort(key=lambda c: c.prioridad)
    for e in errores:
        log.warning(e)
    return out, errores


def _fuerza_onu(cat: Categoria, cod: str) -> tuple[int, str | None]:
    for pref, f in cat.onu:
        if cod.startswith(pref):
            return f, pref
    return 0, None


def _excluida(cat: Categoria, textos: tuple[str, ...], f: int) -> str | None:
    """Nombre de la exclusión que calza, o None. Una exclusión `salvo_onu_fuerte`
    no aplica si el código ONU identifica la categoría (p. ej. "resistente a
    químicos" en un guante con código de guante quirúrgico)."""
    for n, rx, salvo in cat.excluye:
        if salvo and f == FUERZA["fuerte"]:
            continue
        if any(rx.search(t) for t in textos):
            return n
    return None


def _evaluar_categoria(cat: Categoria, glosa: str, titulo: str, cod: str):
    f, pref = _fuerza_onu(cat, cod) if cod else (0, None)
    if _excluida(cat, (glosa,), f):
        return None
    fuertes, debiles = [], []
    for t in cat.terminos:
        if not t.regex.search(glosa):
            continue
        if t.contexto is None or t.contexto.search(glosa) or t.contexto.search(titulo):
            fuertes.append(t.nombre)
        else:
            debiles.append(t.nombre)
    if fuertes and f:
        return "verde", "ambas", fuertes, pref
    if fuertes:
        return "revision", "solo_palabra", fuertes, None
    if debiles and f == FUERZA["fuerte"]:
        return "revision", "palabra_debil+onu", debiles, pref
    if f == FUERZA["fuerte"] and cat.onu_solo_a_revision:
        return "revision", "solo_onu", [], pref
    return None


def _evaluar_titulo(cat: Categoria, glosa: str, titulo: str, cod: str):
    """Término (con su contexto) en el título + ONU de la categoría → revisión.
    Sin ONU, sólo en categorías con titulo_solo_a_revision (medido: en
    oftalmología, ~la mitad de las líneas bajo un título "insumos oftalmológicos"
    lo son aunque el código ONU sea genérico)."""
    if not titulo:
        return None
    f, pref = _fuerza_onu(cat, cod) if cod else (0, None)
    if _excluida(cat, (glosa, titulo), f):
        return None
    terms = [t.nombre for t in cat.terminos if t.regex.search(titulo)
             and (t.contexto is None or t.contexto.search(titulo) or t.contexto.search(glosa))]
    if terms and f:
        return "revision", "titulo+onu", terms, pref
    if terms and cat.titulo_solo_a_revision:
        return "revision", "titulo", terms, None
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
    if not hits:  # sólo si la glosa no dio nada: el título es una señal más débil
        for cat in categorias:
            r = _evaluar_titulo(cat, glosa, tit, cod)
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
    canon = [MOTOR_VERSION] + [
        {
            "codigo": c["codigo"], "linea": c["linea"], "nombre": c["nombre"],
            "prioridad": int(c["prioridad"]), "onu_solo": bool(c["onu_solo_a_revision"]),
            "titulo_solo": bool(c.get("titulo_solo_a_revision")),
            "t": [(t["nombre"], t["regex"], t.get("contexto")) for t in _activos(c.get("terminos", []))],
            "e": sorted((e["nombre"], e["regex"], bool(e.get("salvo_onu_fuerte"))) for e in _activos(c.get("excluye", []))),
            "o": sorted((str(o["codigo"]), o["fuerza"]) for o in _activos(c.get("onu", []))),
        }
        for c in sorted(_activos(categorias), key=lambda c: c["codigo"])
    ]
    return hashlib.sha1(json.dumps(canon, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Diagnóstico (sólo auditoría): por qué una fila NO calzó en ninguna categoría
# --------------------------------------------------------------------------
# Raíces de las palabras del cliente, para pillar variantes que las regex no cubren.
RAICES_CLIENTE = re.compile(r"oftal|guant|guat|aposit|deterg|microsc|campim|intraoc|\blio\b|\boct\b"
                            r"|mantenc|mantenim|manuten|preventiv|correctiv")


def diagnosticar(categorias: list[Categoria], descripcion: str | None, titulo: str | None,
                 codigo_onu: str | None) -> list[str]:
    """Para una fila que `evaluar` dejó fuera: motivos de 'casi calza', del más
    al menos concreto. Lista vacía = no se parece a nada de la configuración.
    No decide nada: alimenta la auditoría de lo no rescatado."""
    glosa, tit, cod = normalizar(descripcion), normalizar(titulo), (codigo_onu or "").strip()
    motivos = []
    for cat in categorias:
        f, pref = _fuerza_onu(cat, cod) if cod else (0, None)
        excl = [n for n, rx, salvo in cat.excluye if rx.search(glosa) and not (salvo and f == FUERZA["fuerte"])]
        en_glosa = [t.nombre for t in cat.terminos if t.regex.search(glosa)]
        en_titulo = [t.nombre for t in cat.terminos if t.regex.search(tit)]
        if en_glosa and excl:
            motivos.append(f"{cat.codigo} · excluida por '{excl[0]}'")
        elif en_glosa:
            motivos.append(f"{cat.codigo} · '{en_glosa[0]}' sin contexto ni ONU fuerte")
        elif en_titulo:
            motivos.append(f"{cat.codigo} · '{en_titulo[0]}' sólo en el título, sin ONU")
        elif f and pref and len(pref) >= 8 and cat.codigo != "SRV-MAN":
            motivos.append(f"{cat.codigo} · código ONU sin palabra")
    if not motivos:
        m = RAICES_CLIENTE.search(glosa)
        if m:
            motivos.append(f"raíz '{m.group(0)}' sin calce")
    return motivos
