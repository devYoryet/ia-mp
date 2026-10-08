"""Tareas de administración de la Fase 2. Las corre UNA PERSONA, a mano, con
credenciales de administrador (F2_ADMIN_MYSQL_*). El servicio nunca las usa.

    python admin.py schema                         # crea las tablas clasificador_f2_* (IF NOT EXISTS)
    python admin.py usuario --host 10.0.0.70 --guardar-en ruta/.env
                                                   # crea ia_fase2 con permisos mínimos
    python admin.py semilla                        # carga semilla.py si la config está vacía
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
    sentencias = [s.strip() for s in "\n".join(
        l for l in sql.splitlines() if not l.strip().startswith("--")).split(";") if s.strip()]
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


def semilla(a) -> None:
    import semilla as s
    conn, _db = _admin()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM clasificador_f2_categorias")
        if cur.fetchone()["n"] and not a.forzar:
            raise SystemExit("La configuración ya tiene datos: no se carga la semilla encima (usar --forzar sólo si está vacía de verdad).")
        quien = f"semilla {s.VERSION_SEMILLA}"
        for c in s.CATEGORIAS:
            cur.execute("INSERT INTO clasificador_f2_categorias (codigo, linea, nombre, prioridad, onu_solo_a_revision, "
                        "activa, creado_por, creado_en, actualizado_en) VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())",
                        (c["codigo"], c["linea"], c["nombre"], c["prioridad"], int(c["onu_solo_a_revision"]),
                         int(c.get("activa", True)), quien))
            for i, t in enumerate(c["terminos"]):
                cur.execute("INSERT INTO clasificador_f2_terminos (categoria_codigo, tipo, nombre, regex, contexto_regex, "
                            "orden, origen, activa, creado_por, creado_en, actualizado_en) "
                            "VALUES (%s,'incluye',%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())",
                            (c["codigo"], t["nombre"], t["regex"], t.get("contexto"), i, t["origen"],
                             int(t.get("activa", True)), quien))
            for i, e in enumerate(c["excluye"]):
                cur.execute("INSERT INTO clasificador_f2_terminos (categoria_codigo, tipo, nombre, regex, contexto_regex, "
                            "orden, origen, activa, creado_por, creado_en, actualizado_en) "
                            "VALUES (%s,'excluye',%s,%s,NULL,%s,%s,%s,%s,NOW(),NOW())",
                            (c["codigo"], e["nombre"], e["regex"], i, e.get("origen", "sugerido"),
                             int(e.get("activa", True)), quien))
            for o in c["onu"]:
                cur.execute("INSERT INTO clasificador_f2_onu (categoria_codigo, codigo, fuerza, nota, activa, creado_por, "
                            "creado_en) VALUES (%s,%s,%s,%s,%s,%s,NOW())",
                            (c["codigo"], o["codigo"], o["fuerza"], o.get("nota"), int(o.get("activa", True)), quien))
            print("ok:", c["codigo"])


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
    s.add_argument("--forzar", action="store_true")
    s.set_defaults(f=semilla)
    sub.add_parser("verificar").set_defaults(f=verificar)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
