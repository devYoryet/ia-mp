"""Barrido de la Fase 2: lee las fuentes (sólo lectura) y escribe SÓLO en
clasificador_f2_resultado / _corridas.

Cada corrida toma, por fuente, las filas con id ≥ al menor id publicado en los
últimos `dias` días (los ids crecen con la inserción, así que entran también las
filas insertadas tarde). Las lee por bloques de PK y, para cada una:

  1. estado efectivo = estado_gestor, o lo que sugirió la fase 1 en el log;
  2. si es descarte, la evalúa el motor (palabra + código ONU);
  3. concilia con lo ya guardado (`reconciliar`): inserta, actualiza, anula si
     pasó a farma, y nunca pisa una decisión humana.

Lee las tablas origen, no sólo clasificador_ia_log: el bot del legacy
('Bot Eliminado') elimina ~8.100 filas al mes antes de que pase el worker.

Uso:
    python barrido.py --dias 3                 # corrida manual (escribe)
    python barrido.py --dias 30 --seco         # sólo cuenta, no escribe nada
"""

from __future__ import annotations

import argparse
import logging
import time
from collections import Counter, defaultdict

import motor
from bd import BD
from reconciliar import CAMPOS_AUTO, CAMPOS_INFO, campos_auto, estado_efectivo, reconciliar

log = logging.getLogger("fase2.barrido")

# Columna del código ONU (UNSPSC) por fuente. Copia deliberada de fuentes.py de
# la fase 1: la fase 2 no importa código de la fase 1, así un cambio allá no la
# rompe y un error acá no puede tocar allá. OJO: en Licitaciones_diarias el
# código está en Cod_Onu; en compra_agil y cotizaciones, en Item.
FUENTES = {"compra_agil": "Item", "Licitaciones_diarias": "Cod_Onu", "cotizaciones": "Item"}
# Fuentes cuyo scraper pega el nombre UNSPSC al final de la glosa ("… Guantes
# quirúrgicos"). Si no se quita, ese nombre hace de "palabra" y las dos señales
# dejan de ser independientes (medido 2026-10-08: 4 de 9 cotizaciones con sufijo
# calzaban sólo por él). La glosa original se guarda igual (es la que ve la persona).
CON_SUFIJO_ONU = {"cotizaciones"}
LOTE = 2000

SQL_ONU = """
SELECT Cod_Onu AS cod, Producto_Servicio AS nom, COUNT(*) AS n
FROM Licitaciones_diarias
WHERE Cod_Onu IS NOT NULL AND Cod_Onu <> '' AND Producto_Servicio IS NOT NULL AND Producto_Servicio <> ''
GROUP BY Cod_Onu, Producto_Servicio
"""
SQL_RANGO_LO = "SELECT MIN(id) AS lo FROM `{tabla}` WHERE Fecha_Publicacion >= NOW() - INTERVAL %s DAY"
SQL_RANGO_HI = "SELECT MAX(id) AS hi FROM `{tabla}`"
SQL_FILAS = """
SELECT t.id, t.estado_gestor, t.nombre_clasificador, t.`{rub}` AS rub,
       LEFT(t.Descripcion, 1000) AS descripcion, LEFT(t.Titulo, 300) AS titulo,
       LEFT(t.Licitacion, 255) AS licitacion, LEFT(t.pactivo, 255) AS pactivo,
       t.Fecha_Publicacion AS fecha_publicacion, t.Fecha_Cierre AS fecha_cierre
FROM `{tabla}` t
WHERE t.id >= %s AND t.id <= %s
ORDER BY t.id
LIMIT %s
"""
SQL_LOG = ("SELECT fila_id, interes_sugerido, metodo FROM clasificador_ia_log "
           "WHERE tabla_origen=%s AND fila_id BETWEEN %s AND %s ORDER BY id")
SQL_PREVIO = ("SELECT id, fila_id, vigente, decision, " + ", ".join(CAMPOS_INFO + CAMPOS_AUTO) +
              " FROM clasificador_f2_resultado WHERE tabla_origen=%s AND fila_id BETWEEN %s AND %s")

COLS_INS = ("tabla_origen", "fila_id") + CAMPOS_INFO + CAMPOS_AUTO + ("version_reglas", "vigente", "motivo_no_vigente")
SQL_INS = (f"INSERT INTO clasificador_f2_resultado ({', '.join(COLS_INS)}, creado_en, actualizado_en) "
           f"VALUES ({', '.join(['%s'] * len(COLS_INS))}, NOW(), NOW()) ON DUPLICATE KEY UPDATE id=id")
_COLS_UPD = frozenset(CAMPOS_INFO + CAMPOS_AUTO + ("version_reglas", "vigente", "motivo_no_vigente"))

SQL_CORRIDA = (
    "INSERT INTO clasificador_f2_corridas (tipo, dias, version_reglas, filas_leidas, descartes, pendientes, "
    "calzan, insertadas, actualizadas, anuladas, reactivadas, info, segundos, error, creado_en) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())"
)


# --------------------------------------------------------------------------
# Configuración y diccionario ONU
# --------------------------------------------------------------------------
def cargar_config(bd: BD) -> list[dict]:
    """Configuración ACTIVA desde las tablas, con la misma forma que semilla.CATEGORIAS."""
    cats = bd.todos("SELECT codigo, linea, nombre, prioridad, onu_solo_a_revision, titulo_solo_a_revision "
                    "FROM clasificador_f2_categorias WHERE activa=1")
    por = {c["codigo"]: {**c, "onu_solo_a_revision": bool(c["onu_solo_a_revision"]),
                         "titulo_solo_a_revision": bool(c["titulo_solo_a_revision"]),
                         "terminos": [], "excluye": [], "onu": []} for c in cats}
    for t in bd.todos("SELECT categoria_codigo, tipo, nombre, regex, contexto_regex, salvo_onu_fuerte "
                      "FROM clasificador_f2_terminos WHERE activa=1 ORDER BY categoria_codigo, orden, id"):
        c = por.get(t["categoria_codigo"])
        if c is None:
            continue
        if t["tipo"] == "incluye":
            c["terminos"].append({"nombre": t["nombre"], "regex": t["regex"], "contexto": t["contexto_regex"]})
        elif t["tipo"] == "excluye":
            c["excluye"].append({"nombre": t["nombre"], "regex": t["regex"], "salvo_onu_fuerte": bool(t["salvo_onu_fuerte"])})
    for o in bd.todos("SELECT categoria_codigo, codigo, fuerza FROM clasificador_f2_onu WHERE activa=1"):
        c = por.get(o["categoria_codigo"])
        if c is not None:
            c["onu"].append({"codigo": o["codigo"], "fuerza": o["fuerza"]})
    return list(por.values())


def cargar_onu(bd: BD) -> dict[str, tuple[str, int]]:
    """{código: (nombre UNSPSC dominante, n)} desde Licitaciones_diarias.Producto_Servicio.
    Medido 2026-10-08: 12.948 códigos, un solo nombre por código salvo 2."""
    mejor: dict[str, tuple[str, int]] = {}
    for r in bd.todos(SQL_ONU):
        cod, nom, n = str(r["cod"]).strip()[:20], str(r["nom"]).strip()[:255], int(r["n"])
        if cod and (cod not in mejor or n > mejor[cod][1]):
            mejor[cod] = (nom, n)
    return mejor


def persistir_onu(bd: BD, onu: dict[str, tuple[str, int]]) -> None:
    sql = ("INSERT INTO clasificador_f2_onu_nombre (codigo, nombre, n, actualizado_en) VALUES (%s,%s,%s,NOW()) "
           "ON DUPLICATE KEY UPDATE nombre=VALUES(nombre), n=VALUES(n), actualizado_en=NOW()")
    items = list(onu.items())
    for i in range(0, len(items), 1000):
        bd.transaccion([(sql, (c, nom, n)) for c, (nom, n) in items[i:i + 1000]])


# --------------------------------------------------------------------------
# Barrido
# --------------------------------------------------------------------------
def rango(bd: BD, tabla: str, dias: int) -> tuple[int, int] | None:
    lo = bd.uno(SQL_RANGO_LO.format(tabla=tabla), (dias,))
    if not lo or lo["lo"] is None:
        return None
    hi = bd.uno(SQL_RANGO_HI.format(tabla=tabla))
    return int(lo["lo"]), int(hi["hi"])


def _info(f: dict, lg: dict | None, onu: dict) -> dict:
    cod = (f["rub"] or "").strip()[:20]
    nc = f["nombre_clasificador"]
    metodo = (lg or {}).get("metodo")
    return {
        "licitacion": f["licitacion"],
        "fecha_publicacion": f["fecha_publicacion"],
        "fecha_cierre": f["fecha_cierre"],
        "descripcion": f["descripcion"],
        "titulo": f["titulo"],
        "codigo_onu": cod or None,
        "nombre_onu": onu.get(cod, (None, 0))[0] if cod else None,
        "ia_interes": lg["interes_sugerido"] if lg else None,
        "ia_metodo": metodo[:60] if metodo else None,
        "estado_gestor": f["estado_gestor"],
        "clasificador_f1": nc[:80] if nc else None,
        "pactivo_f1": f["pactivo"] or None,
    }


def glosa_sin_sufijo_onu(descripcion: str | None, nombre_onu: str | None) -> str | None:
    """Quita el nombre UNSPSC del final de la glosa, si está. Nunca deja la glosa vacía."""
    if not descripcion or not nombre_onu:
        return descripcion
    d = descripcion.rstrip()
    if len(d) > len(nombre_onu) and d.lower().endswith(nombre_onu.lower()):
        return d[: -len(nombre_onu)].rstrip()
    return descripcion


def barrer_tabla(bd: BD, cats: list, version: str, onu: dict, tabla: str, lo: int, hi: int,
                 st: Counter, escribir: bool = True, leer_previos: bool = True, muestras=None) -> None:
    rub = FUENTES[tabla]
    sql_filas = SQL_FILAS.format(tabla=tabla, rub=rub)
    desde = lo
    while desde <= hi:
        filas = bd.todos(sql_filas, (desde, hi, LOTE))
        if not filas:
            break
        a, b = filas[0]["id"], filas[-1]["id"]
        logs = {r["fila_id"]: r for r in bd.todos(SQL_LOG, (tabla, a, b))}  # el último gana
        previos = {r["fila_id"]: r for r in bd.todos(SQL_PREVIO, (tabla, a, b))} if leer_previos else {}
        sentencias = []
        for f in filas:
            st["filas_leidas"] += 1
            lg = logs.get(f["id"])
            ef = estado_efectivo(f["estado_gestor"], lg["interes_sugerido"] if lg else None)
            if ef is None:
                st["pendientes"] += 1
                continue
            if ef == 0:
                st["descartes"] += 1
            glosa = f["descripcion"]
            if tabla in CON_SUFIJO_ONU:
                glosa = glosa_sin_sufijo_onu(glosa, onu.get((f["rub"] or "").strip(), (None, 0))[0])
            res = motor.evaluar(cats, glosa, f["titulo"], f["rub"])
            # Interés farma: sólo cuenta si es Device con las DOS señales (si no, una
            # gota oftálmica, que es farma, aparecería como Device por la palabra).
            estado_auto = None
            if ef == 1:
                res = res if res and res.estado_auto == "verde" else None
                estado_auto = "farma"
            if res:
                st["calzan"] += 1
                st[("cat", res.categoria, estado_auto or res.estado_auto, res.senal)] += 1
                st[("tabla", tabla, estado_auto or res.estado_auto)] += 1
                if muestras is not None:
                    muestras[(res.categoria, estado_auto or res.senal)].append(
                        (tabla, f["id"], (f["descripcion"] or "")[:110], onu.get((f["rub"] or "").strip(), ("?", 0))[0][:40],
                         ", ".join(res.terminos)))
            info = _info(f, lg, onu)
            accion, cambios = reconciliar(previos.get(f["id"]), ef, campos_auto(res, estado_auto) if res else None,
                                          info, version)
            st[accion] += 1
            if accion == "insertar":
                sentencias.append((SQL_INS, (tabla, f["id"]) + tuple(cambios[c] for c in COLS_INS[2:])))
            elif accion != "nada":
                extra = set(cambios) - _COLS_UPD
                if extra:  # defensa: el barrido nunca escribe columnas de la revisión humana
                    raise RuntimeError(f"columnas no permitidas en el barrido: {extra}")
                cols = sorted(cambios)
                sentencias.append((
                    f"UPDATE clasificador_f2_resultado SET {', '.join(c + '=%s' for c in cols)}, "
                    f"actualizado_en=NOW() WHERE id=%s",
                    tuple(cambios[c] for c in cols) + (previos[f["id"]]["id"],),
                ))
        if escribir and sentencias:
            bd.transaccion(sentencias)
        desde = b + 1


def correr(bd: BD, tipo: str, dias: int, onu: dict, config: list[dict], escribir: bool = True,
           leer_previos: bool = True, tablas=None, muestras=None) -> Counter | None:
    """Una corrida completa sobre todas las fuentes. Devuelve los contadores, o
    None si otra corrida tiene el lock."""
    t0 = time.time()
    cats, _errores = motor.compilar(config)
    if not cats:
        # Sin categorías activas el barrido anularía todo lo no revisado: se niega.
        raise RuntimeError("Fase 2: no hay categorías activas; el barrido no corre.")
    version = motor.version(config)
    st: Counter = Counter()
    if escribir and not bd.lock():
        log.warning("Otra corrida tiene el lock; se salta esta.")
        return None
    error = None
    try:
        for tabla in (tablas or FUENTES):
            r = rango(bd, tabla, dias)
            if not r:
                log.info("%s: sin filas en los últimos %d días", tabla, dias)
                continue
            barrer_tabla(bd, cats, version, onu, tabla, r[0], r[1], st,
                         escribir=escribir, leer_previos=leer_previos, muestras=muestras)
            log.info("%s: ids %d-%d listos", tabla, r[0], r[1])
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"[:2000]
        raise
    finally:
        st["segundos"] = round(time.time() - t0, 1)
        st["version"] = version
        if escribir:
            try:
                bd.transaccion([(SQL_CORRIDA, (
                    tipo, dias, version, st["filas_leidas"], st["descartes"], st["pendientes"], st["calzan"],
                    st["insertar"], st["actualizar"], st["anular"], st["reactivar"], st["info"],
                    st["segundos"], error))])
            finally:
                bd.unlock()
    log.info("Corrida %s (%d días, reglas %s): %d filas, %d descartes, %d calzan, +%d ins, %d act, "
             "%d anul, %d react, %d info, %.1f s", tipo, dias, version, st["filas_leidas"], st["descartes"],
             st["calzan"], st["insertar"], st["actualizar"], st["anular"], st["reactivar"], st["info"],
             st["segundos"])
    return st


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dias", type=int, default=3)
    ap.add_argument("--seco", action="store_true", help="no escribe nada (sólo cuenta)")
    ap.add_argument("--tabla", choices=list(FUENTES), action="append")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bd = BD()
    bd.exigir_usuario_restringido()
    st = correr(bd, "manual", a.dias, cargar_onu(bd), cargar_config(bd), escribir=not a.seco,
                leer_previos=True, tablas=a.tabla)
    if st is None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
