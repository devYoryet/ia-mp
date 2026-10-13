"""Tareas de administración de la Fase 2. Las corre UNA PERSONA, a mano, con
credenciales de administrador (F2_ADMIN_MYSQL_*). El servicio nunca las usa.

    python admin.py schema                         # crea las tablas clasificador_f2_* (IF NOT EXISTS)
    python admin.py usuario --host 10.0.0.70 --guardar-en ruta/.env
                                                   # crea ia_fase2 con permisos mínimos
    python admin.py semilla                        # carga semilla.py si la config está vacía
    python admin.py migrar                         # aplica a una BD existente los cambios de schema.sql
    python admin.py pactivos                       # carga la segunda categoría de pactivos farma de semilla.py (las que falten)
    python admin.py usuario-entrega --host 10.0.0.70 --guardar-en ruta/.env.entrega [--rotar]
                                                   # usuario ia_f2_entrega en el clásico Y en prime (match del gestor 2)
    python admin.py suscribir --unidad 79 --categoria "Guantes médicos" [--composicion "Sin cla"]
                                                   # entrada en principal_app.diccionario_unidad (cualquier cliente)
    python admin.py verificar                      # con F2_MYSQL_*: prueba que NO puede escribir en tablas ajenas

Permisos del usuario ia_fase2 (lo único que puede hacer):
    SELECT                   sobre licitaciones_diarias_total_farma.*
    INSERT, UPDATE           sobre clasificador_f2_resultado y clasificador_f2_onu_nombre
    INSERT                   sobre clasificador_f2_corridas
Sin DELETE, sin DDL, sin escritura en ninguna tabla existente, máx. 4 conexiones.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

from bd import BD, parametros

AQUI = Path(__file__).resolve().parent
USUARIO = "ia_fase2"
ESCRITURA = {
    "clasificador_f2_resultado": "INSERT, UPDATE",
    "clasificador_f2_onu_nombre": "INSERT, UPDATE",
    "clasificador_f2_corridas": "INSERT",
}


def _admin():
    import pymysql
    p = parametros("F2_ADMIN_MYSQL")
    return pymysql.connect(host=p["host"], port=p["port"], user=p["user"], password=p["password"],
                           database=p["database"], charset="utf8mb4", autocommit=True,
                           cursorclass=pymysql.cursors.DictCursor), p["database"]


def schema(_a) -> None:
    conn, _db = _admin()
    sql = (AQUI / "schema.sql").read_text(encoding="utf-8")
    # Sin comentarios de línea (incluidos los que van al final de una columna) antes de cortar por ";".
    limpio = "\n".join(l.split("--", 1)[0] for l in sql.splitlines())
    sentencias = [s.strip() for s in limpio.split(";") if s.strip()]
    with conn.cursor() as cur:
        for s in sentencias:
            if not s.upper().startswith("CREATE TABLE IF NOT EXISTS CLASIFICADOR_F2_"):
                raise SystemExit(f"schema.sql sólo puede crear tablas clasificador_f2_*: {s[:60]!r}")
            cur.execute(s)
            print("ok:", s.split("(")[0].strip())


def usuario(a) -> None:
    """Crea (o, con --rotar, re-claviza) ia_fase2 en cada host. La clave se
    escribe al archivo ANTES de tocar MySQL: un error a mitad nunca la pierde."""
    conn, db = _admin()
    p = parametros("F2_ADMIN_MYSQL")
    clave = secrets.token_urlsafe(24)
    destino = Path(a.guardar_en)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        f"F2_MYSQL_HOST={p['host']}\nF2_MYSQL_PORT={p['port']}\nF2_MYSQL_USER={USUARIO}\n"
        f"F2_MYSQL_PASSWORD={clave}\nF2_MYSQL_DB={db}\n", encoding="utf-8")
    os.chmod(destino, 0o600)
    with conn.cursor() as cur:
        for host in a.host:
            cuenta = f"'{USUARIO}'@'{host}'"
            cuenta_fmt = cuenta.replace("%", "%%")  # el host comodín lleva '%' y la sentencia lleva parámetros
            cur.execute("SELECT 1 FROM mysql.user WHERE user=%s AND host=%s", (USUARIO, host))
            if cur.fetchone():
                if not a.rotar:
                    raise SystemExit(f"ya existe {cuenta}: usar --rotar para asignarle la clave de {destino}")
                cur.execute(f"ALTER USER {cuenta_fmt} IDENTIFIED WITH mysql_native_password BY %s "
                            f"WITH MAX_USER_CONNECTIONS 4", (clave,))
                print(f"clave rotada {cuenta}")
            else:
                cur.execute(f"CREATE USER {cuenta_fmt} IDENTIFIED WITH mysql_native_password BY %s "
                            f"WITH MAX_USER_CONNECTIONS 4", (clave,))
                print(f"creado {cuenta}")
            cur.execute(f"GRANT SELECT ON `{db}`.* TO {cuenta}")
            for tabla, priv in ESCRITURA.items():
                cur.execute(f"GRANT {priv} ON `{db}`.`{tabla}` TO {cuenta}")
            cur.execute(f"SHOW GRANTS FOR {cuenta}")
            for r in cur.fetchall():
                print("   ", list(r.values())[0])
    print(f"clave en {destino} (modo 600; no se imprime)")


# Columnas agregadas después de crear las tablas: (tabla, columna, definición).
# MySQL 8 no tiene ADD COLUMN IF NOT EXISTS: se consulta information_schema.
COLUMNAS_NUEVAS = [
    ("clasificador_f2_resultado", "pactivo_f1", "VARCHAR(255) NULL AFTER clasificador_f1"),
    ("clasificador_f2_categorias", "titulo_solo_a_revision", "TINYINT(1) NOT NULL DEFAULT 0 AFTER onu_solo_a_revision"),
    ("clasificador_f2_terminos", "salvo_onu_fuerte", "TINYINT(1) NOT NULL DEFAULT 0 AFTER orden"),
]


def migrar(_a) -> None:
    """Idempotente: crea las tablas que falten (schema.sql) y agrega las columnas
    nuevas a las tablas propias. Sólo toca tablas clasificador_f2_*."""
    schema(_a)
    conn, db = _admin()
    with conn.cursor() as cur:
        for tabla, col, definicion in COLUMNAS_NUEVAS:
            assert tabla.startswith("clasificador_f2_")
            cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
                        "AND column_name=%s", (db, tabla, col))
            if cur.fetchone():
                print(f"ya existe {tabla}.{col}")
                continue
            cur.execute(f"ALTER TABLE `{tabla}` ADD COLUMN `{col}` {definicion}")
            print(f"agregada {tabla}.{col}")


def semilla(a) -> None:
    import semilla as s
    conn, _db = _admin()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM clasificador_f2_categorias")
        hay = cur.fetchone()["n"]
        if hay and not a.reemplazar:
            raise SystemExit("La configuración ya tiene datos. Para pasar a esta versión de la semilla: --reemplazar "
                             "(desactiva términos y códigos actuales, que quedan como historial, y carga los nuevos).")
        quien = f"semilla {s.VERSION_SEMILLA}"
        if hay:
            cur.execute("UPDATE clasificador_f2_terminos SET activa=0, actualizado_en=NOW() WHERE activa=1")
            cur.execute("UPDATE clasificador_f2_onu SET activa=0 WHERE activa=1")
            print(f"configuración anterior desactivada (historial); cargando {quien}")
        for c in s.CATEGORIAS:
            cur.execute("INSERT INTO clasificador_f2_categorias (codigo, linea, nombre, prioridad, onu_solo_a_revision, "
                        "titulo_solo_a_revision, activa, creado_por, creado_en, actualizado_en) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW()) ON DUPLICATE KEY UPDATE linea=VALUES(linea), "
                        "nombre=VALUES(nombre), prioridad=VALUES(prioridad), onu_solo_a_revision=VALUES(onu_solo_a_revision), "
                        "titulo_solo_a_revision=VALUES(titulo_solo_a_revision), activa=VALUES(activa), "
                        "creado_por=VALUES(creado_por), actualizado_en=NOW()",
                        (c["codigo"], c["linea"], c["nombre"], c["prioridad"], int(c["onu_solo_a_revision"]),
                         int(c.get("titulo_solo_a_revision", False)), int(c.get("activa", True)), quien))
            for i, t in enumerate(c["terminos"]):
                cur.execute("INSERT INTO clasificador_f2_terminos (categoria_codigo, tipo, nombre, regex, contexto_regex, "
                            "orden, origen, activa, creado_por, creado_en, actualizado_en) "
                            "VALUES (%s,'incluye',%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())",
                            (c["codigo"], t["nombre"], t["regex"], t.get("contexto"), i, t["origen"],
                             int(t.get("activa", True)), quien))
            for i, e in enumerate(c["excluye"]):
                cur.execute("INSERT INTO clasificador_f2_terminos (categoria_codigo, tipo, nombre, regex, contexto_regex, "
                            "orden, salvo_onu_fuerte, origen, activa, creado_por, creado_en, actualizado_en) "
                            "VALUES (%s,'excluye',%s,%s,NULL,%s,%s,%s,%s,%s,NOW(),NOW())",
                            (c["codigo"], e["nombre"], e["regex"], i, int(bool(e.get("salvo_onu_fuerte"))),
                             e.get("origen", "sugerido"), int(e.get("activa", True)), quien))
            for o in c["onu"]:
                cur.execute("INSERT INTO clasificador_f2_onu (categoria_codigo, codigo, fuerza, nota, activa, creado_por, "
                            "creado_en) VALUES (%s,%s,%s,%s,%s,%s,NOW())",
                            (c["codigo"], o["codigo"], o["fuerza"], o.get("nota"), int(o.get("activa", True)), quien))
            print("ok:", c["codigo"])


def pactivos(_a) -> None:
    """Agrega las filas de semilla.PACTIVO_CATEGORIA que falten (no toca las existentes)."""
    import semilla as s
    import motor
    conn, _db = _admin()
    with conn.cursor() as cur:
        cur.execute("SELECT pactivo, categoria_codigo FROM clasificador_f2_pactivo_categoria")
        hay = {(motor.normalizar(r["pactivo"]), r["categoria_codigo"]) for r in cur.fetchall()}
        n = 0
        for p in s.PACTIVO_CATEGORIA:
            if (motor.normalizar(p["pactivo"]), p["categoria_codigo"]) in hay:
                continue
            cur.execute("INSERT INTO clasificador_f2_pactivo_categoria (pactivo, categoria_codigo, nota, activa, creado_por, "
                        "creado_en) VALUES (%s,%s,%s,1,%s,NOW())",
                        (p["pactivo"], p["categoria_codigo"], p.get("nota"), f"semilla {s.VERSION_SEMILLA}"))
            n += 1
    print(f"pactivos con segunda categoría: {n} nuevos, {len(hay)} ya estaban")


USUARIO_ENTREGA = "ia_f2_entrega"
DESTINOS_ENTREGA = ("Licitaciones_diarias_clientes", "Licitaciones_diarias_clientes_agil",
                    "Licitaciones_diarias_clientes_cotizaciones")


def _admin_prime():
    import pymysql
    p = parametros("F2_ADMIN_PRIME_MYSQL")
    return pymysql.connect(host=p["host"], port=p["port"], user=p["user"], password=p["password"],
                           charset="utf8mb4", autocommit=True, cursorclass=pymysql.cursors.DictCursor)


def usuario_entrega(a) -> None:
    """ia_f2_entrega: lee lo necesario; escribe SÓLO en las 3 tablas por cliente (INSERT/DELETE,
    sin UPDATE de filas ajenas) y en el libro de la fase 2. Mismo usuario y clave en clásico y prime.
    La clave se escribe al archivo ANTES de tocar MySQL."""
    conn, db = _admin()
    p = parametros("F2_ADMIN_MYSQL")
    pp = parametros("F2_ADMIN_PRIME_MYSQL")
    clave = secrets.token_urlsafe(24)
    destino = Path(a.guardar_en)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        f"F2E_MYSQL_HOST={p['host']}\nF2E_MYSQL_PORT={p['port']}\nF2E_MYSQL_USER={USUARIO_ENTREGA}\n"
        f"F2E_MYSQL_PASSWORD={clave}\nF2E_MYSQL_DB={db}\n"
        f"F2E_PRIME_MYSQL_HOST={pp['host']}\nF2E_PRIME_MYSQL_PORT={pp['port']}\nF2E_PRIME_MYSQL_USER={USUARIO_ENTREGA}\n"
        f"F2E_PRIME_MYSQL_PASSWORD={clave}\nF2E_PRIME_MYSQL_DB=licitaciones_diarias_intranet\n"
        f"F2E_ACTIVA=0\nF2E_RUTS=\n", encoding="utf-8")
    os.chmod(destino, 0o600)
    destinos = [f"`licitaciones_diarias_intranet`.`{t}`" for t in DESTINOS_ENTREGA]
    for nombre, c, grants in (
        ("clásico", conn, [f"GRANT SELECT ON `{db}`.* TO {{c}}",
                           f"GRANT INSERT, UPDATE ON `{db}`.`clasificador_f2_entregas` TO {{c}}",
                           f"GRANT INSERT ON `{db}`.`clasificador_f2_corridas` TO {{c}}",
                           "GRANT SELECT ON `principal_app`.`unidad_negocio` TO {c}",
                           "GRANT SELECT ON `principal_app`.`diccionario_unidad` TO {c}"]
                          + [f"GRANT SELECT, INSERT, DELETE ON {t} TO {{c}}" for t in destinos]),
        ("prime", _admin_prime(), [f"GRANT SELECT, INSERT, DELETE ON {t} TO {{c}}" for t in destinos]),
    ):
        with c.cursor() as cur:
            for host in a.host:
                cuenta = f"'{USUARIO_ENTREGA}'@'{host}'"
                cfmt = cuenta.replace("%", "%%")
                cur.execute("SELECT 1 FROM mysql.user WHERE user=%s AND host=%s", (USUARIO_ENTREGA, host))
                if cur.fetchone():
                    if not a.rotar:
                        raise SystemExit(f"{nombre}: ya existe {cuenta}: usar --rotar")
                    cur.execute(f"ALTER USER {cfmt} IDENTIFIED WITH mysql_native_password BY %s WITH MAX_USER_CONNECTIONS 4", (clave,))
                else:
                    cur.execute(f"CREATE USER {cfmt} IDENTIFIED WITH mysql_native_password BY %s WITH MAX_USER_CONNECTIONS 4", (clave,))
                for g in grants:
                    cur.execute(g.format(c=cuenta))
                cur.execute(f"SHOW GRANTS FOR {cuenta}")
                print(f"{nombre}: {cuenta}")
                for r in cur.fetchall():
                    print("    ", list(r.values())[0])
    print(f"clave en {destino} (modo 600; no se imprime). F2E_ACTIVA=0: arranca en seco.")


def suscribir(a) -> None:
    """Agrega a una unidad de negocio una entrada de diccionario con una categoría de la fase 2
    (igual que una entrada de farma: pactivo + composición/presentación, con comodín 'Sin cla')."""
    conn, db = _admin()
    with conn.cursor() as cur:
        cur.execute(f"SELECT codigo, nombre FROM `{db}`.clasificador_f2_categorias WHERE activa=1")
        nombres = {r["nombre"] for r in cur.fetchall()}
        if a.categoria not in nombres:
            raise SystemExit(f"Categoría desconocida: {a.categoria!r}. Válidas: {sorted(nombres)}")
        cur.execute("SELECT id, strNombreUnidadNegocio n, strRutUsuario r FROM principal_app.unidad_negocio WHERE id=%s", (a.unidad,))
        u = cur.fetchone()
        if not u:
            raise SystemExit(f"No existe la unidad {a.unidad}")
        cur.execute("SELECT id FROM principal_app.diccionario_unidad WHERE intIdUnidad_negocio=%s AND pactivo=%s "
                    "AND composicion=%s", (a.unidad, a.categoria, a.composicion))
        if cur.fetchone():
            print(f"ya estaba: unidad {u['id']} {u['n']} ({u['r']}) · {a.categoria} · {a.composicion}")
            return
        cur.execute("INSERT INTO principal_app.diccionario_unidad (intIdUnidad_negocio, pactivo, composicion, presentacion, "
                    "created_at) VALUES (%s,%s,%s,'Sin cla',NOW())", (a.unidad, a.categoria, a.composicion))
        print(f"agregada id {cur.lastrowid}: unidad {u['id']} {u['n']} ({u['r']}) · {a.categoria} · {a.composicion}")


def verificar(_a) -> None:
    """Prueba los tres candados con el usuario de la fase 2. Las sentencias de
    prueba son no-ops (id=-1, WHERE 1=0, IF NOT EXISTS sobre una tabla que ya
    existe, DROP de una tabla inexistente): aunque un permiso fallara, no
    cambiarían nada. Se espera que MySQL las rechace TODAS."""
    import pymysql
    bd = BD()
    print("usuario:", bd.usuario_actual())
    fallos = 0
    conn = pymysql.connect(**{k: v for k, v in bd.p.items()}, charset="utf8mb4", autocommit=True)
    pruebas = [
        ("UPDATE compra_agil", "UPDATE compra_agil SET estado_gestor=estado_gestor WHERE id=-1"),
        ("UPDATE Licitaciones_diarias", "UPDATE Licitaciones_diarias SET pactivo=pactivo WHERE id=-1"),
        ("UPDATE cotizaciones", "UPDATE cotizaciones SET pactivo=pactivo WHERE id=-1"),
        ("UPDATE clasificador_ia_log", "UPDATE clasificador_ia_log SET revisado=revisado WHERE id=-1"),
        ("INSERT clasificador_ia_reglas",
         "INSERT INTO clasificador_ia_reglas (tipo, texto, creado_en) SELECT 'regla','x',NOW() FROM DUAL WHERE 1=0"),
        ("DELETE clasificador_f2_resultado", "DELETE FROM clasificador_f2_resultado WHERE id=-1"),
        ("UPDATE clasificador_f2_terminos", "UPDATE clasificador_f2_terminos SET activa=activa WHERE id=-1"),
        ("CREATE TABLE", "CREATE TABLE IF NOT EXISTS clasificador_f2_corridas (i INT)"),
        ("DROP TABLE", "DROP TABLE IF EXISTS clasificador_f2_no_existe_prueba"),
    ]
    with conn.cursor() as cur:
        for nombre, sql in pruebas:
            try:
                cur.execute(sql)
                print(f"  FALLA  MySQL permitió: {nombre}")
                fallos += 1
            except pymysql.err.MySQLError as exc:
                print(f"  ok     MySQL rechaza {nombre}: {exc.args[0]} {str(exc.args[1])[:60]}")
    # candado de código: BD.transaccion valida ANTES de mandar nada
    try:
        bd.transaccion([("UPDATE compra_agil SET estado_gestor=estado_gestor WHERE id=-1", ())])
        print("  FALLA  el código dejó pasar una escritura ajena")
        fallos += 1
    except PermissionError as exc:
        print("  ok     el código bloquea:", exc)
    # candado de sesión: la conexión de lectura es READ ONLY incluso en tablas propias
    try:
        with bd._lectura().cursor() as cur:
            cur.execute("INSERT INTO clasificador_f2_corridas (tipo, dias, version_reglas, creado_en) VALUES ('x',0,'x',NOW())")
        print("  FALLA  la sesión de lectura escribió")
        fallos += 1
    except pymysql.err.MySQLError as exc:
        print(f"  ok     la sesión de lectura es READ ONLY: {exc.args[0]}")
    print("RESULTADO:", "TODO BLOQUEADO" if not fallos else f"{fallos} FALLAS")
    sys.exit(1 if fallos else 0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema").set_defaults(f=schema)
    u = sub.add_parser("usuario")
    u.add_argument("--host", action="append", required=True)
    u.add_argument("--guardar-en", required=True)
    u.add_argument("--rotar", action="store_true", help="si ya existe, asignarle una clave nueva")
    u.set_defaults(f=usuario)
    s = sub.add_parser("semilla")
    s.add_argument("--reemplazar", action="store_true", help="pasar la config existente a esta versión de la semilla")
    s.set_defaults(f=semilla)
    sub.add_parser("verificar").set_defaults(f=verificar)
    sub.add_parser("migrar").set_defaults(f=migrar)
    sub.add_parser("pactivos").set_defaults(f=pactivos)
    ue = sub.add_parser("usuario-entrega")
    ue.add_argument("--host", action="append", required=True)
    ue.add_argument("--guardar-en", required=True)
    ue.add_argument("--rotar", action="store_true")
    ue.set_defaults(f=usuario_entrega)
    su = sub.add_parser("suscribir")
    su.add_argument("--unidad", type=int, required=True)
    su.add_argument("--categoria", required=True)
    su.add_argument("--composicion", default="Sin cla")
    su.set_defaults(f=suscribir)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
