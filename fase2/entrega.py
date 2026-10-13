"""Match del gestor 2: entrega lo que clasificó la Fase 2 a las tablas por cliente.

Es el mismo match que hace el gestor 1 (gestor_licitaciones ImportarLicitaciones /
ImportarComprasAgiles), pero leyendo el resultado de la fase 2 en vez de la tabla
de farma. El gestor 1 no puede hacerlo: las líneas de la fase 2 son descartes de
farma (estado_gestor=0, sin pactivo) y escribir la categoría en esas filas
mezclaría device con farma (estadísticas, cola de revisión y datos de
entrenamiento de los modelos de farma).

Cómo decide a quién entregar: por DICCIONARIO, igual que farma. Una unidad de
negocio de cualquier cliente recibe una categoría si su diccionario_unidad tiene
una entrada con pactivo = nombre de la categoría ("Guantes médicos",
"Mantención de equipos médicos", ...). Composición "Sin cla" = toda la categoría;
otra composición = sólo las líneas cuya subcategoría la contenga (p. ej.
"Lentes intraoculares"). Ningún pactivo de farma ni diccionario existente usa
esos nombres (verificado 2026-10-13), así que el gestor 1 y los correos de prime
ignoran esas entradas.

Qué se entrega: lo asignado de la fase 2 (vigente=1 AND (decision='aprobado' OR
(decision IS NULL AND estado_auto IN ('verde','farma')))) que sigue ABIERTO y se
publicó en los últimos F2E_DIAS (30) días, copiado a:
    compra_agil          → licitaciones_diarias_intranet.Licitaciones_diarias_clientes_agil
    Licitaciones_diarias → licitaciones_diarias_intranet.Licitaciones_diarias_clientes
    cotizaciones         → licitaciones_diarias_intranet.Licitaciones_diarias_clientes_cotizaciones
en el clásico Y en prime, como el gestor 1.

Garantías (nada de lo existente se modifica):
  - Sólo INSERTA filas nuevas; nunca actualiza una fila ajena. La fila es idéntica
    a las del legacy (mismos campos; llave queda NULL como en el 100 % de las filas):
    pactivo = categoría, composición = subcategoría, presentación 'Sin cla',
    negocio = nombre de la unidad.
  - La PK (Licitacion, Item, strRutCliente[, Cod_Onu]) impide duplicar: si el
    gestor 1 ya entregó esa línea al cliente, no se toca ('ya_existia').
  - Cuáles filas son de la fase 2 lo dice SÓLO el libro clasificador_f2_entregas.
    Revertir todo = borrar las filas del libro.
  - Retiro: si una línea entregada se anula o se rechaza en la revisión, se borra
    sólo si su contenido es el que puso la fase 2 (pactivo y negocio) y el cliente
    no la tocó (sin estado interno, sin descarte, sin usuario). Si la tocó,
    queda ('retenida'). Lo cerrado nunca se retira.
  - Candado de código (`validar_sql_entrega`) y usuario MySQL propio (ia_f2_entrega).
  - F2E_ACTIVA=0 (por defecto) = en seco: cuenta, no escribe. F2E_RUTS limita a
    esos clientes mientras se pone en marcha (vacío = todos los que tengan entradas).

    python entrega.py --seco --una-vez     # qué entregaría, sin escribir
    python entrega.py                      # servicio (container ia-mp-fase2-entrega)
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from collections import Counter, defaultdict

import motor
from bd import parametros

log = logging.getLogger("fase2.entrega")

ESQUEMA = "licitaciones_diarias_intranet"
DESTINO = {
    "compra_agil": "Licitaciones_diarias_clientes_agil",
    "Licitaciones_diarias": "Licitaciones_diarias_clientes",
    "cotizaciones": "Licitaciones_diarias_clientes_cotizaciones",
}
# Clave primaria de cada tabla por cliente, sin strRutCliente (SHOW CREATE TABLE, 2026-10-13).
CLAVE = {
    "Licitaciones_diarias_clientes": ("Licitacion", "Item"),
    "Licitaciones_diarias_clientes_agil": ("Licitacion", "Item", "Cod_Onu"),
    "Licitaciones_diarias_clientes_cotizaciones": ("Licitacion", "Item", "Cod_Onu"),
}
# Campos que copia el gestor 1, en su orden (ImportarLicitaciones.php / ImportarComprasAgiles.php).
BASE = ("Demandante", "Unidad_Compra", "Region", "Comuna", "Fecha_Publicacion", "Fecha_Cierre", "Cod_Onu",
        "Cantidad", "Unidad_Medida", "Descripcion", "Producto_Servicio", "Licitacion", "Titulo", "VINCULOS",
        "Rut_Cliente", "Duracion_Contrato", "Precio_Ponderacion", "Tiempo_Contrato", "Garantia_Seriedad_Ofertas",
        "Garantia_Seriedad_Contrato", "Fechaadjudicacion", "Fechainiciopregunta", "Fechafinalpregunta", "Item",
        "Estado_activo", "CodigoPostulacion", "CorrelativoInterno", "link_ficha", "link_historial",
        "link_acta_adjudicacion", "link_apertura", "str_fecha_cierre")
CONTACTO = ("nombre_contacto", "telefono_contacto", "mail_contacto", "monto_total")
CON_CONTACTO = {"compra_agil", "cotizaciones"}
COMODIN = {"", "sin cla", "sin clas"}

DIAS = int(os.getenv("F2E_DIAS", "30"))
INTERVALO = int(os.getenv("F2E_INTERVALO_SEGUNDOS", "600"))

SQL_ASIGNADAS = """
SELECT tabla_origen, fila_id, COALESCE(categoria_final, categoria) AS cat, subcategoria
FROM clasificador_f2_resultado
WHERE vigente=1 AND (decision='aprobado' OR (decision IS NULL AND estado_auto IN ('verde','farma')))
  AND fecha_cierre >= NOW() AND fecha_publicacion >= NOW() - INTERVAL %s DAY
"""
SQL_LIBRO = (
    "INSERT INTO clasificador_f2_entregas (tabla_origen, fila_id, rut_cliente, unidad_id, negocio, destino, licitacion, "
    "item, cod_onu, categoria, estado, en_clasico, en_prime, motivo, entregado_en, actualizado_en) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW()) "
    "ON DUPLICATE KEY UPDATE estado=VALUES(estado), en_clasico=VALUES(en_clasico), en_prime=VALUES(en_prime), "
    "motivo=VALUES(motivo), actualizado_en=NOW()"
)
# El único DELETE posible: por PK, sólo si el contenido es el que puso la fase 2 y el cliente no la tocó.
_RETIRO_COND = ("`pactivo`=%s AND `negocio`=%s AND COALESCE(intEstadoInterno,0)=0 "
                "AND COALESCE(intEstadoDescarte,0)=0 AND rut_usuario IS NULL")


# --------------------------------------------------------------------------
# Candado de escritura
# --------------------------------------------------------------------------
_TABLAS_DESTINO = {f"{ESQUEMA}.{t}" for t in DESTINO.values()}
_LIBRO = {"clasificador_f2_entregas", "clasificador_f2_corridas"}
_SQL_RX = re.compile(r"^\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+`?(\w+)`?(?:\.`?(\w+)`?)?", re.IGNORECASE)


def validar_sql_entrega(sql: str) -> None:
    """INSERT en las 3 tablas por cliente o en el libro; UPDATE sólo del libro;
    DELETE sólo en las tablas por cliente y sólo con la condición de retiro completa."""
    m = _SQL_RX.match(sql)
    if not m:
        raise PermissionError(f"Entrega: sentencia no permitida: {sql[:80]!r}")
    verbo = m.group(1).split()[0].upper()
    tabla = f"{m.group(2)}.{m.group(3)}" if m.group(3) else m.group(2)
    if verbo == "INSERT" and (tabla in _TABLAS_DESTINO or tabla in _LIBRO):
        return
    if verbo == "UPDATE" and tabla == "clasificador_f2_entregas":
        return
    if verbo == "DELETE" and tabla in _TABLAS_DESTINO and sql.rstrip().endswith(_RETIRO_COND):
        return
    raise PermissionError(f"Entrega: {verbo} en {tabla} bloqueado")


class Conexion:
    def __init__(self, p: dict, solo_lectura: bool = False):
        self.p, self.solo_lectura, self._c = p, solo_lectura, None

    def _con(self):
        import pymysql
        if self._c is None:
            self._c = pymysql.connect(host=self.p["host"], port=self.p["port"], user=self.p["user"],
                                      password=self.p["password"], database=self.p["database"], charset="utf8mb4",
                                      autocommit=True, cursorclass=pymysql.cursors.DictCursor,
                                      connect_timeout=15, read_timeout=180, write_timeout=180)
            with self._c.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
                if self.solo_lectura:
                    cur.execute("SET SESSION TRANSACTION READ ONLY")
        else:
            self._c.ping(reconnect=True)
        return self._c

    def todos(self, sql: str, params=()) -> list[dict]:
        if not sql.lstrip().upper().startswith("SELECT"):
            raise PermissionError("Entrega: todos() sólo hace SELECT")
        with self._con().cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def uno(self, sql: str, params=()) -> dict | None:
        r = self.todos(sql, params)
        return r[0] if r else None

    def ejecutar(self, sql: str, params=()) -> int:
        if self.solo_lectura:
            raise PermissionError("Entrega: conexión de lectura")
        validar_sql_entrega(sql)
        with self._con().cursor() as cur:
            return cur.execute(sql, params)

    def cerrar(self):
        try:
            if self._c is not None:
                self._c.close()
        finally:
            self._c = None


# --------------------------------------------------------------------------
# Piezas puras (testeables)
# --------------------------------------------------------------------------
def suscripciones(diccionario: list[dict], cats: dict) -> dict[int, list[tuple[str, str | None]]]:
    """{unidad_id: [(codigo_categoria, filtro_subcategoria|None), ...]} desde las entradas
    de diccionario_unidad cuyo pactivo es el nombre (o el código) de una categoría."""
    por_nombre = {}
    for cod, c in cats.items():
        por_nombre[motor.normalizar(c["nombre"])] = cod
        por_nombre[motor.normalizar(cod)] = cod
    out = defaultdict(list)
    for d in diccionario:
        cod = por_nombre.get(motor.normalizar(d["pactivo"]))
        if not cod:
            continue
        comp = motor.normalizar(d.get("composicion"))
        out[d["intIdUnidad_negocio"]].append((cod, None if comp in COMODIN else comp))
    return dict(out)


def corresponde(subs: list[tuple[str, str | None]], cat: str, subcategoria: str | None) -> bool:
    sub = motor.normalizar(subcategoria)
    return any(c == cat and (f is None or f in sub) for c, f in subs)


def construir_fila(tabla_origen: str, src: dict, unidad: dict, categoria: dict, subcategoria: str | None,
                   hoy) -> dict:
    """La fila que arma el gestor 1, con la categoría de la fase 2 en pactivo/composición."""
    campos = BASE + (CONTACTO if tabla_origen in CON_CONTACTO else ())
    fila = {c: src.get(c) for c in campos}
    fila.update({
        "Campo21": "Campo21",
        "strRutCliente": unidad["rut"],
        "datFechaPublicacion": src.get("Fecha_Publicacion"),
        "datFechaCierre": src.get("Fecha_Cierre"),
        "pactivo": categoria["nombre"],
        "composicion": (subcategoria or "Sin cla")[:255],
        "presentacion": "Sin cla",
        "datFechaSubida": hoy,
        "negocio": unidad["nombre"],
    })
    return fila


def clave_fila(destino: str, fila: dict) -> tuple:
    return tuple(fila[c] for c in CLAVE[destino]) + (fila["strRutCliente"],)


def _where_pk(destino: str) -> str:
    return " AND ".join(f"`{c}`=%s" for c in CLAVE[destino] + ("strRutCliente",))


def sql_retiro(destino: str) -> str:
    return f"DELETE FROM {ESQUEMA}.{destino} WHERE {_where_pk(destino)} AND {_RETIRO_COND}"


# --------------------------------------------------------------------------
# Escritura en una copia (clásico o prime)
# --------------------------------------------------------------------------
def _insertar(con: Conexion, destino: str, fila: dict) -> str:
    """'nueva' | 'nuestra' (ya estaba con el contenido de la fase 2) | 'ajena' (la entregó el gestor 1)."""
    import pymysql
    cols = list(fila)
    try:
        con.ejecutar(f"INSERT INTO {ESQUEMA}.{destino} ({', '.join('`' + c + '`' for c in cols)}) "
                     f"VALUES ({', '.join(['%s'] * len(cols))})", tuple(fila[c] for c in cols))
        return "nueva"
    except pymysql.err.IntegrityError as exc:
        if exc.args[0] != 1062:
            raise
        r = con.uno(f"SELECT pactivo, negocio FROM {ESQUEMA}.{destino} WHERE {_where_pk(destino)}", clave_fila(destino, fila))
        return "nuestra" if r and r["pactivo"] == fila["pactivo"] and r["negocio"] == fila["negocio"] else "ajena"


def _existe(con: Conexion, destino: str, clave: tuple, pactivo: str, negocio: str) -> bool:
    return con.uno(f"SELECT 1 AS x FROM {ESQUEMA}.{destino} WHERE {_where_pk(destino)} AND pactivo=%s AND negocio=%s",
                   clave + (pactivo, negocio)) is not None


# --------------------------------------------------------------------------
# Ciclo
# --------------------------------------------------------------------------
def ciclo(lec: Conexion, cl: Conexion | None, pr: Conexion | None, ruts: list[str], escribir: bool) -> Counter:
    st: Counter = Counter()
    hoy = lec.uno("SELECT CURDATE() AS d")["d"]  # fecha del MySQL (Chile), no del container
    cats = {c["codigo"]: c for c in lec.todos("SELECT codigo, linea, nombre FROM clasificador_f2_categorias WHERE activa=1")}
    nombres = sorted({c["nombre"] for c in cats.values()} | set(cats))
    dic = lec.todos(f"SELECT intIdUnidad_negocio, pactivo, composicion FROM principal_app.diccionario_unidad "
                    f"WHERE pactivo IN ({','.join(['%s'] * len(nombres))})", tuple(nombres))
    subs = suscripciones(dic, cats)
    if not subs:
        log.info("Ninguna unidad tiene categorías de la fase 2 en su diccionario")
        return st
    unidades = {u["id"]: {"id": u["id"], "nombre": u["n"], "rut": u["r"]}
                for u in lec.todos(f"SELECT id, strNombreUnidadNegocio AS n, strRutUsuario AS r FROM principal_app.unidad_negocio "
                                   f"WHERE id IN ({','.join(['%s'] * len(subs))})", tuple(subs))
                if not ruts or u["r"] in ruts}
    if not unidades:
        return st
    asignadas = lec.todos(SQL_ASIGNADAS, (DIAS,))
    st["candidatas"] = len(asignadas)
    ruts_u = sorted({u["rut"] for u in unidades.values()})
    libro = {(r["tabla_origen"], r["fila_id"], r["rut_cliente"]): r
             for r in lec.todos(f"SELECT * FROM clasificador_f2_entregas WHERE rut_cliente IN ({','.join(['%s'] * len(ruts_u))})",
                                tuple(ruts_u))}

    por_tabla = defaultdict(set)
    for a in asignadas:
        por_tabla[a["tabla_origen"]].add(a["fila_id"])
    origen = {}
    for t, ids in por_tabla.items():
        ids = sorted(ids)
        for i in range(0, len(ids), 500):
            parte = ids[i:i + 500]
            for r in lec.todos(f"SELECT * FROM `{t}` WHERE id IN ({','.join(['%s'] * len(parte))})", tuple(parte)):
                origen[(t, r["id"])] = r

    # 1) entregar lo nuevo (o completar la copia que haya fallado)
    entregadas_ahora = set()
    for u in unidades.values():
        for a in asignadas:
            if a["cat"] not in cats or not corresponde(subs[u["id"]], a["cat"], a["subcategoria"]):
                continue
            k = (a["tabla_origen"], a["fila_id"], u["rut"])
            if k in entregadas_ahora:
                continue  # el cliente tiene dos unidades que la piden: una sola fila (la PK es por cliente)
            entregadas_ahora.add(k)
            prev = libro.get(k)
            if prev and prev["estado"] in ("ya_existia", "retenida"):
                continue
            if prev and prev["estado"] == "entregada" and prev["en_clasico"] and prev["en_prime"]:
                continue
            src = origen.get((a["tabla_origen"], a["fila_id"]))
            if not src:
                continue
            destino = DESTINO[a["tabla_origen"]]
            fila = construir_fila(a["tabla_origen"], src, u, cats[a["cat"]], a["subcategoria"], hoy)
            st[("entregaria", u["rut"], u["nombre"], a["tabla_origen"])] += 1
            if not escribir:
                continue
            r_cl = _insertar(cl, destino, fila)
            r_pr = _insertar(pr, destino, fila)
            propia = {"nueva", "nuestra"}
            estado = "ya_existia" if r_cl not in propia and r_pr not in propia else "entregada"
            st[estado] += 1
            cl.ejecutar(SQL_LIBRO, (a["tabla_origen"], a["fila_id"], u["rut"], u["id"], u["nombre"], destino,
                                    fila["Licitacion"], str(fila["Item"]), fila.get("Cod_Onu"), a["cat"], estado,
                                    int(r_cl in propia), int(r_pr in propia),
                                    None if estado == "entregada" else "el gestor 1 ya había entregado esa línea"))

    # 2) retirar lo entregado que se anuló o se rechazó (nunca lo cerrado: queda como historial)
    entregadas = [r for r in libro.values() if r["estado"] == "entregada"]
    actuales = {}
    for t in {r["tabla_origen"] for r in entregadas}:
        ids = sorted({r["fila_id"] for r in entregadas if r["tabla_origen"] == t})
        for i in range(0, len(ids), 500):
            parte = ids[i:i + 500]
            for r in lec.todos("SELECT tabla_origen, fila_id, vigente, decision, subcategoria, "
                               "COALESCE(categoria_final, categoria) AS cat FROM clasificador_f2_resultado "
                               f"WHERE tabla_origen=%s AND fila_id IN ({','.join(['%s'] * len(parte))})", (t,) + tuple(parte)):
                actuales[(r["tabla_origen"], r["fila_id"])] = r
    for r in entregadas:
        act = actuales.get((r["tabla_origen"], r["fila_id"]))
        u = unidades.get(r["unidad_id"])
        if act and act["vigente"] and act["decision"] != "rechazado" and u \
                and corresponde(subs.get(u["id"], []), act["cat"], act["subcategoria"]):
            continue
        st["retiraria"] += 1
        if not escribir:
            continue
        destino = r["destino"]
        clave = (r["licitacion"], r["item"]) + ((r["cod_onu"],) if len(CLAVE[destino]) == 3 else ()) + (r["rut_cliente"],)
        nombre_cat = cats.get(r["categoria"], {}).get("nombre", r["categoria"])
        for con in (cl, pr):
            con.ejecutar(sql_retiro(destino), clave + (nombre_cat, r["negocio"]))
        en_cl = _existe(cl, destino, clave, nombre_cat, r["negocio"])
        en_pr = _existe(pr, destino, clave, nombre_cat, r["negocio"])
        estado = "retenida" if en_cl or en_pr else "retirada"
        st[estado] += 1
        cl.ejecutar(SQL_LIBRO, (r["tabla_origen"], r["fila_id"], r["rut_cliente"], r["unidad_id"], r["negocio"], destino,
                                r["licitacion"], r["item"], r["cod_onu"], r["categoria"], estado, int(en_cl), int(en_pr),
                                "el cliente ya la había trabajado" if estado == "retenida"
                                else "anulada o rechazada en la fase 2"))
    return st


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seco", action="store_true", help="no escribe nada aunque F2E_ACTIVA=1")
    ap.add_argument("--una-vez", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    activa = os.getenv("F2E_ACTIVA", "0") == "1" and not a.seco
    ruts = [x.strip() for x in os.getenv("F2E_RUTS", "").split(",") if x.strip()]
    lec = Conexion(parametros("F2E_MYSQL"), solo_lectura=True)
    cl = Conexion(parametros("F2E_MYSQL")) if activa else None
    pr = Conexion(parametros("F2E_PRIME_MYSQL")) if activa else None
    log.info("=== Match gestor 2 · %s · clientes: %s · ventana %d días ===",
             "ACTIVO" if activa else "EN SECO", ", ".join(ruts) or "todos los que tengan entradas", DIAS)
    while True:
        t0, error, st = time.time(), None, Counter()
        try:
            st = ciclo(lec, cl, pr, ruts, escribir=activa)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"[:2000]
            log.exception("Ciclo de entrega falló")
            for c in (lec, cl, pr):
                if c:
                    c.cerrar()
        log.info("Entrega: %s (%.1f s)", {(k if isinstance(k, str) else " · ".join(map(str, k))): v
                                         for k, v in st.items()}, time.time() - t0)
        if activa:
            try:
                cl.ejecutar(
                    "INSERT INTO clasificador_f2_corridas (tipo, dias, version_reglas, filas_leidas, calzan, insertadas, "
                    "anuladas, info, segundos, error, creado_en) VALUES ('entrega',%s,'entrega-1',%s,%s,%s,%s,%s,%s,%s,NOW())",
                    (DIAS, st["candidatas"], sum(v for k, v in st.items() if isinstance(k, tuple)), st["entregada"],
                     st["retirada"], st["ya_existia"], round(time.time() - t0, 1), error))
            except Exception:  # noqa: BLE001
                log.exception("No se pudo registrar la corrida de entrega")
        if a.una_vez:
            break
        time.sleep(INTERVALO)


if __name__ == "__main__":
    main()
