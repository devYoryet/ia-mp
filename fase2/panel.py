"""Panel de revisión de la Fase 2 (Device / Servicio técnico).

App APARTE del panel del clasificador (api/main.py no se toca): corre en el
container `ia-mp-fase2-panel`, puerto 8810, con el usuario MySQL restringido.
Lo único que escribe son las columnas de revisión humana de
clasificador_f2_resultado (decision, categoria_final, revisado_por,
revisado_en, motivo). El barrido nunca toca esas columnas.

Login: el mismo padrón del equipo (tabla `users` del legacy, bcrypt de
Laravel), con cookie propia (F2_SESSION_SECRET).

Vistas: /revision (una señal → aprobar / no aprobar), verde (ambas señales,
corregibles), revisadas, anuladas; /resumen (volumen, precisión medida y salud
del barrido); /exportar.xlsx (lo filtrado, para el cliente).
"""

from __future__ import annotations

import html
import io
import re
import os
import threading
from datetime import datetime
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from bd import BD

POR_PAGINA = 50
MAX_EXPORT = 20000
PUBLICAS = {"/login", "/logout", "/salud"}

FUENTES = {"compra_agil": "Compra ágil", "Licitaciones_diarias": "Licitación", "cotizaciones": "Cotización"}
SENALES = {
    "ambas": "Palabra + ONU",
    "solo_palabra": "Sólo palabra",
    "solo_onu": "Sólo código ONU",
    "palabra_debil+onu": "Palabra sin contexto + ONU",
    "titulo+onu": "Palabra en el título + ONU",
}
VISTAS = {
    "revision": ("Revisión", "vigente=1 AND decision IS NULL AND estado_auto='revision'"),
    "verde": ("Verde", "vigente=1 AND decision IS NULL AND estado_auto='verde'"),
    # Interés farma que además es Device con ambas señales (p. ej. apósitos): ya
    # se entrega por el canal farma; aquí sólo se muestra para que la vista Device esté completa.
    "farma": ("En farma", "vigente=1 AND estado_auto='farma'"),
    "revisadas": ("Revisadas", "decision IS NOT NULL"),
    "anuladas": ("Anuladas", "vigente=0"),
}
ORDEN = {
    "revision": "(fecha_cierre IS NULL OR fecha_cierre < NOW()), fecha_cierre ASC, id",
    "verde": "(fecha_cierre IS NULL OR fecha_cierre < NOW()), fecha_cierre ASC, id",
    "farma": "(fecha_cierre IS NULL OR fecha_cierre < NOW()), fecha_cierre ASC, id",
    "revisadas": "revisado_en DESC, id DESC",
    "anuladas": "actualizado_en DESC, id DESC",
}
COLUMNAS = ("id, tabla_origen, fila_id, licitacion, fecha_publicacion, fecha_cierre, descripcion, titulo, "
            "codigo_onu, nombre_onu, ia_interes, ia_metodo, estado_gestor, clasificador_f1, pactivo_f1, categoria, "
            "categoria_nombre, linea, subcategoria, terminos, senal, estado_auto, otras_categorias, vigente, "
            "motivo_no_vigente, decision, categoria_final, revisado_por, revisado_en, motivo, "
            "(fecha_cierre < NOW()) AS cerrada")  # hora del MySQL (Chile): el container está en UTC

# --------------------------------------------------------------------------
# BD: una sola instancia, serializada (pymysql no es thread-safe y el usuario
# tiene un tope de 4 conexiones). El tráfico del panel es de 1-3 personas.
# --------------------------------------------------------------------------
_bd: BD | None = None
_lock = threading.Lock()


def get_bd() -> BD:
    global _bd
    if _bd is None:
        _bd = BD()
    return _bd


def _con_bd(fn):
    with _lock:
        try:
            return fn(get_bd())
        except Exception:
            get_bd().cerrar()  # reconecta en la próxima
            raise


# --------------------------------------------------------------------------
# App y sesión
# --------------------------------------------------------------------------
app = FastAPI(title="Fase 2 · Device / Servicio técnico", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def proteger(request: Request, call_next):
    if request.url.path not in PUBLICAS and not request.session.get("usuario"):
        if request.method == "GET":
            return RedirectResponse("/login?" + urlencode({"next": str(request.url.path) + (
                "?" + request.url.query if request.url.query else "")}), status_code=303)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


def _secret() -> str:
    s = os.getenv("F2_SESSION_SECRET", "")
    if len(s) < 24:
        raise RuntimeError("Falta F2_SESSION_SECRET (mínimo 24 caracteres) en el .env del panel.")
    return s


# Se agrega DESPUÉS del middleware de protección para envolverlo (Starlette: el
# último agregado es el más externo).
app.add_middleware(SessionMiddleware, secret_key=_secret(), max_age=8 * 3600,
                   same_site="lax", https_only=False, session_cookie="f2_sesion")


# --------------------------------------------------------------------------
# Login contra el padrón del legacy
# --------------------------------------------------------------------------
def verificar_password(plano: str, hashed: str | None) -> bool:
    """Laravel guarda bcrypt con prefijo $2y$; la librería espera $2b$."""
    import bcrypt
    h = (hashed or "").strip()
    if len(h) < 50:
        return False
    if h.startswith("$2y$"):
        h = "$2b$" + h[4:]
    try:
        return bcrypt.checkpw(plano.encode("utf-8"), h.encode("utf-8"))
    except ValueError:
        return False


def _buscar_usuario(bd: BD, email: str) -> dict | None:
    return bd.uno("SELECT id, name, email, password FROM users "
                  "WHERE email=%s AND deleted_at IS NULL LIMIT 1", (email.strip(),))


def _next_seguro(n: str | None) -> str:
    return n if n and n.startswith("/") and not n.startswith("//") else "/revision"


@app.get("/login", response_class=HTMLResponse)
def get_login(next: str = "/revision", err: str = ""):
    return _pagina_login(err, _next_seguro(next))


@app.post("/login")
def post_login(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/revision")):
    u = _con_bd(lambda bd: _buscar_usuario(bd, email))
    if not u or not verificar_password(password, u.get("password")):
        return HTMLResponse(_pagina_login("Correo o contraseña incorrectos.", _next_seguro(next), email),
                            status_code=401)
    request.session["usuario"] = {"id": u["id"], "name": u["name"], "email": u["email"]}
    return RedirectResponse(_next_seguro(next), status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------------------
# Consultas
# --------------------------------------------------------------------------
def _filtros(vista: str, cat: str, fuente: str, senal: str, dias: int, q: str) -> tuple[str, list]:
    where, params = [VISTAS[vista][1], "fecha_publicacion >= NOW() - INTERVAL %s DAY"], [dias]
    if cat:
        where.append("COALESCE(categoria_final, categoria)=%s")
        params.append(cat)
    if fuente in FUENTES:
        where.append("tabla_origen=%s")
        params.append(fuente)
    if senal in SENALES:
        where.append("senal=%s")
        params.append(senal)
    if q.strip():
        where.append("(descripcion LIKE %s OR titulo LIKE %s OR licitacion LIKE %s)")
        params += [f"%{q.strip()}%"] * 3
    return " AND ".join(where), params


def _categorias(bd: BD) -> list[dict]:
    return bd.todos("SELECT codigo, linea, nombre FROM clasificador_f2_categorias WHERE activa=1 ORDER BY prioridad")


def _conteos(bd: BD, dias: int) -> dict:
    r = bd.uno(
        "SELECT SUM(vigente=1 AND decision IS NULL AND estado_auto='revision') AS revision, "
        "SUM(vigente=1 AND decision IS NULL AND estado_auto='verde') AS verde, "
        "SUM(vigente=1 AND estado_auto='farma') AS farma, "
        "SUM(decision IS NOT NULL) AS revisadas, SUM(vigente=0) AS anuladas, "
        "SUM(decision IS NOT NULL AND DATE(revisado_en)=CURDATE()) AS hoy "
        "FROM clasificador_f2_resultado WHERE fecha_publicacion >= NOW() - INTERVAL %s DAY", (dias,))
    return {k: int(v or 0) for k, v in (r or {}).items()}


def _ultima_corrida(bd: BD) -> dict | None:
    return bd.uno("SELECT tipo, creado_en, segundos, error, TIMESTAMPDIFF(MINUTE, creado_en, NOW()) AS hace "
                  "FROM clasificador_f2_corridas ORDER BY id DESC LIMIT 1")


# --------------------------------------------------------------------------
# Vistas
# --------------------------------------------------------------------------
@app.get("/salud")
def salud():
    def _q(bd):
        u = _ultima_corrida(bd)
        return {"ok": bool(u and not u["error"] and (u["hace"] or 0) <= 30),
                "ultima_corrida": u and {"tipo": u["tipo"], "creado_en": str(u["creado_en"]),
                                         "hace_min": u["hace"], "error": u["error"]}}
    try:
        return JSONResponse(_con_bd(_q))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": type(exc).__name__}, status_code=503)


@app.get("/")
def raiz():
    return RedirectResponse("/revision", status_code=303)


@app.get("/revision", response_class=HTMLResponse)
def revision(request: Request, vista: str = "revision", cat: str = "", fuente: str = "", senal: str = "",
             dias: int = 60, q: str = "", p: int = 1):
    vista = vista if vista in VISTAS else "revision"
    dias = max(1, min(dias, 365))
    p = max(1, p)
    where, params = _filtros(vista, cat, fuente, senal, dias, q)
    where_sc, params_sc = _filtros(vista, "", fuente, senal, dias, q)  # sin la categoría: conteo por categoría

    def _q(bd):
        filas = bd.todos(f"SELECT {COLUMNAS} FROM clasificador_f2_resultado WHERE {where} "
                         f"ORDER BY {ORDEN[vista]} LIMIT %s OFFSET %s", tuple(params) + (POR_PAGINA, (p - 1) * POR_PAGINA))
        total = bd.uno(f"SELECT COUNT(*) AS n FROM clasificador_f2_resultado WHERE {where}", tuple(params))["n"]
        por_cat = bd.todos(f"SELECT COALESCE(categoria_final, categoria) AS c, COUNT(*) AS n FROM clasificador_f2_resultado "
                           f"WHERE {where_sc} GROUP BY 1", tuple(params_sc))
        return filas, int(total), {r["c"]: r["n"] for r in por_cat}, _categorias(bd), _conteos(bd, dias), _ultima_corrida(bd)

    filas, total, por_cat, cats, conteos, corrida = _con_bd(_q)
    filtros = {"vista": vista, "cat": cat, "fuente": fuente, "senal": senal, "dias": dias, "q": q}
    aqui = "/revision?" + urlencode({**filtros, "p": p})
    cuerpo = [_tabs(filtros, conteos), _form_filtros(filtros, cats, por_cat)]
    paginas = max(1, -(-total // POR_PAGINA))
    cuerpo.append(f'<p class="meta">{total} filas · página {p} de {paginas} · '
                  f'<a href="/exportar.xlsx?{urlencode(filtros)}">Exportar a Excel</a></p>')
    if not filas:
        cuerpo.append('<p class="vacio">No hay filas con estos filtros.</p>')
    nombres = {c["codigo"]: c for c in cats}
    for f in filas:
        cuerpo.append(_tarjeta(f, cats, nombres, aqui))
    cuerpo.append(_paginador(filtros, p, paginas))
    return _layout(request, VISTAS[vista][0], "".join(cuerpo), corrida)


@app.post("/decidir")
def decidir(request: Request, id: int = Form(...), decision: str = Form(...), categoria: str = Form(""),
            motivo: str = Form(""), next: str = Form("/revision")):
    quiere_json = "application/json" in request.headers.get("accept", "")
    usuario = request.session["usuario"]["name"][:80]

    def _err(msg, code=400):
        if quiere_json:
            return JSONResponse({"ok": False, "error": msg}, status_code=code)
        return HTMLResponse(f"<p>{html.escape(msg)}</p><p><a href='{html.escape(_next_seguro(next))}'>Volver</a></p>",
                            status_code=code)

    if decision not in ("aprobado", "rechazado", "deshacer"):
        return _err("Decisión inválida.")

    def _q(bd):
        fila = bd.uno("SELECT id, categoria, vigente FROM clasificador_f2_resultado WHERE id=%s", (id,))
        if not fila:
            return "no existe"
        if decision == "deshacer":
            bd.transaccion([("UPDATE clasificador_f2_resultado SET decision=NULL, categoria_final=NULL, "
                             "revisado_por=NULL, revisado_en=NULL, motivo=NULL WHERE id=%s", (id,))])
            return None
        if not fila["vigente"]:
            return "La fila está anulada (pasó a farma o ya no calza); no se puede decidir."
        cat_final = categoria.strip() or None
        if cat_final == fila["categoria"]:
            cat_final = None
        if cat_final and cat_final not in {c["codigo"] for c in _categorias(bd)}:
            return "Categoría desconocida."
        bd.transaccion([("UPDATE clasificador_f2_resultado SET decision=%s, categoria_final=%s, revisado_por=%s, "
                         "revisado_en=NOW(), motivo=%s WHERE id=%s",
                         (decision, cat_final, usuario, motivo.strip()[:2000] or None, id))])
        return None

    problema = _con_bd(_q)
    if problema:
        return _err(problema, 404 if problema == "no existe" else 400)
    if quiere_json:
        return JSONResponse({"ok": True, "decision": None if decision == "deshacer" else decision, "por": usuario})
    return RedirectResponse(_next_seguro(next), status_code=303)


@app.get("/resumen", response_class=HTMLResponse)
def resumen(request: Request, dias: int = 30):
    dias = max(1, min(dias, 365))

    def _q(bd):
        por = bd.todos(
            "SELECT COALESCE(categoria_final, categoria) AS c, "
            "SUM(vigente=1 AND estado_auto='verde') AS verde, SUM(vigente=1 AND estado_auto='revision') AS revision, "
            "SUM(vigente=1 AND estado_auto='farma') AS farma, "
            "SUM(decision='aprobado') AS aprob, SUM(decision='rechazado') AS rech "
            "FROM clasificador_f2_resultado WHERE fecha_publicacion >= NOW() - INTERVAL %s DAY GROUP BY 1", (dias,))
        prec = bd.todos(
            "SELECT categoria, estado_auto, senal, SUM(decision='aprobado') AS a, SUM(decision='rechazado') AS r "
            "FROM clasificador_f2_resultado WHERE decision IS NOT NULL GROUP BY 1,2,3 ORDER BY 1,2,3")
        fuentes = bd.todos(
            "SELECT tabla_origen AS t, SUM(estado_auto='verde') AS verde, SUM(estado_auto='revision') AS revision "
            "FROM clasificador_f2_resultado WHERE vigente=1 AND fecha_publicacion >= NOW() - INTERVAL %s DAY GROUP BY 1", (dias,))
        corridas = bd.todos("SELECT tipo, dias, filas_leidas, calzan, insertadas, actualizadas, anuladas, segundos, "
                            "error, creado_en FROM clasificador_f2_corridas ORDER BY id DESC LIMIT 12")
        return por, prec, fuentes, corridas, _categorias(bd), _ultima_corrida(bd)

    por, prec, fuentes, corridas, cats, corrida = _con_bd(_q)
    nom = {c["codigo"]: f'{c["linea"]} · {c["nombre"]}' for c in cats}
    e = html.escape
    filas = "".join(
        f"<tr><td>{e(nom.get(r['c'], r['c']))}</td><td>{int(r['verde'] or 0)}</td><td>{int(r['verde'] or 0) / dias:.1f}</td>"
        f"<td>{int(r['revision'] or 0)}</td><td>{int(r.get('farma') or 0)}</td><td>{int(r['aprob'] or 0)}</td>"
        f"<td>{int(r['rech'] or 0)}</td></tr>"
        for r in sorted(por, key=lambda r: -int(r["verde"] or 0)))
    tprec = "".join(
        f"<tr><td>{e(nom.get(r['categoria'], r['categoria']))}</td><td>{e(r['estado_auto'])}</td>"
        f"<td>{e(SENALES.get(r['senal'], r['senal']))}</td><td>{int(r['a'])}</td><td>{int(r['r'])}</td>"
        f"<td><b>{100 * int(r['a']) / max(1, int(r['a']) + int(r['r'])):.0f}%</b></td></tr>" for r in prec
    ) or '<tr><td colspan="6">Todavía no hay revisiones.</td></tr>'
    tfu = " · ".join(f"{e(FUENTES.get(r['t'], r['t']))}: {int(r['verde'] or 0)} verde / {int(r['revision'] or 0)} revisión"
                     for r in fuentes)
    tco = "".join(
        f"<tr><td>{e(str(r['creado_en']))}</td><td>{e(r['tipo'])}</td><td>{r['dias']}</td><td>{r['filas_leidas']}</td>"
        f"<td>{r['calzan']}</td><td>+{r['insertadas']} / {r['actualizadas']} / {r['anuladas']}</td><td>{r['segundos']} s</td>"
        f"<td>{e(r['error'] or 'ok')}</td></tr>" for r in corridas)
    cuerpo = f"""
<form class="linea" method="get" action="/resumen"><label>Días</label>
<input type="number" name="dias" value="{dias}" min="1" max="365"><button>Ver</button></form>
<h2>Volumen por categoría (últimos {dias} días de publicación)</h2>
<table><tr><th>Categoría</th><th>Verde</th><th>Verde/día</th><th>Revisión</th><th>En farma</th><th>Aprobadas</th><th>No aprobadas</th></tr>{filas}</table>
<p class="meta">{tfu}</p>
<h2>Precisión medida (lo que el equipo ya revisó)</h2>
<table><tr><th>Categoría</th><th>Estado auto</th><th>Señal</th><th>Aprob.</th><th>No aprob.</th><th>% aprobación</th></tr>{tprec}</table>
<h2>Últimos barridos</h2>
<table><tr><th>Fecha (Chile)</th><th>Tipo</th><th>Días</th><th>Filas leídas</th><th>Calzan</th><th>+ins / act / anul</th><th>Tiempo</th><th>Estado</th></tr>{tco}</table>
"""
    return _layout(request, "Resumen", cuerpo, corrida)


@app.get("/exportar.xlsx")
def exportar(vista: str = "verde", cat: str = "", fuente: str = "", senal: str = "", dias: int = 60, q: str = ""):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    vista = vista if vista in VISTAS else "verde"
    dias = max(1, min(dias, 365))
    where, params = _filtros(vista, cat, fuente, senal, dias, q)

    def _q(bd):
        return (bd.todos(f"SELECT {COLUMNAS} FROM clasificador_f2_resultado WHERE {where} ORDER BY {ORDEN[vista]} LIMIT %s",
                         tuple(params) + (MAX_EXPORT,)), _categorias(bd))

    filas, cats = _con_bd(_q)
    nom = {c["codigo"]: c for c in cats}
    wb = Workbook()
    ws = wb.active
    ws.title = VISTAS[vista][0]
    cab = ["Fuente", "Licitación / código", "Publicación", "Cierre", "Línea", "Categoría", "Subcategoría", "Estado",
           "Señal", "Términos", "Código ONU", "Nombre ONU", "Glosa", "Título", "Revisado por", "Revisado en", "Motivo"]
    ws.append(cab)
    for c in ws[1]:
        c.font = Font(bold=True)
    for f in filas:
        cat = nom.get(f["categoria_final"] or f["categoria"], {})
        ws.append([_xl(v) for v in [FUENTES.get(f["tabla_origen"], f["tabla_origen"]), f["licitacion"], f["fecha_publicacion"],
                   f["fecha_cierre"], cat.get("linea", f["linea"]), cat.get("nombre", f["categoria_nombre"]),
                   f["subcategoria"], _estado_txt(f), SENALES.get(f["senal"], f["senal"]), f["terminos"],
                   f["codigo_onu"], f["nombre_onu"], f["descripcion"], f["titulo"], f["revisado_por"],
                   f["revisado_en"], f["motivo"]]])
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    nombre = f"fase2_{vista}_{datetime.now():%Y%m%d_%H%M}.xlsx"
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
_ILEGALES_XL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xl(v):
    """openpyxl rechaza caracteres de control (vienen en algunas glosas)."""
    return _ILEGALES_XL.sub(" ", v) if isinstance(v, str) else v


def _estado_txt(f: dict) -> str:
    if not f["vigente"]:
        return f"Anulada ({f['motivo_no_vigente'] or ''})"
    if f["decision"] == "aprobado":
        return "Aprobada"
    if f["decision"] == "rechazado":
        return "No aprobada"
    if f["estado_auto"] == "farma":
        return f"En farma ({f.get('pactivo_f1') or 'sin pactivo'})"
    return "Verde" if f["estado_auto"] == "verde" else "Revisión"


def _via(f: dict) -> str:
    quien, metodo = f["clasificador_f1"], f["ia_metodo"]
    if quien == "Bot Eliminado":
        return "eliminada por el bot del legacy (no pasó por la IA)"
    if f["ia_interes"] == 1 and f["estado_gestor"] == 0:
        return f"la IA dijo interés; {quien or 'una persona'} la eliminó después"
    if f["estado_gestor"] is None:
        return f"descartada por la IA ({metodo or '?'}), sin confirmar"
    return f"descartada por la IA ({metodo or '?'}); confirmó {quien or '?'}"


def _fmt(d) -> str:
    return d.strftime("%d-%m-%Y %H:%M") if isinstance(d, datetime) else ""


def _tarjeta(f: dict, cats: list, nombres: dict, aqui: str) -> str:
    e = html.escape
    cat_act = f["categoria_final"] or f["categoria"]
    c = nombres.get(cat_act, {"linea": f["linea"], "nombre": f["categoria_nombre"]})
    if not f["vigente"]:
        clase = "t-anulada"
    elif f["decision"]:
        clase = "t-aprobada" if f["decision"] == "aprobado" else "t-rechazada"
    else:
        clase = {"verde": "t-verde", "farma": "t-farma"}.get(f["estado_auto"], "t-revision")
    cierre = f["fecha_cierre"]
    cerrada = bool(f.get("cerrada"))
    opts = "".join(f'<option value="{e(x["codigo"])}"{" selected" if x["codigo"] == cat_act else ""}>'
                   f'{e(x["linea"])} · {e(x["nombre"])}</option>' for x in cats)
    titulo = f'<div class="ap-titulo"><span class="ap-tag">Título</span>{e(f["titulo"] or "")}</div>' if f["titulo"] else ""
    otras = f' · también calza: {e(f["otras_categorias"])}' if f["otras_categorias"] else ""
    if f["decision"]:
        cambio = f' → {e(nombres.get(f["categoria_final"], {}).get("nombre", f["categoria_final"]))}' if f["categoria_final"] else ""
        estado = (f'<div class="hu-dec"><b>{e(_estado_txt(f))}</b>{cambio} por {e(f["revisado_por"] or "")} '
                  f'el {_fmt(f["revisado_en"])}{" · " + e(f["motivo"]) if f["motivo"] else ""}</div>')
    elif not f["vigente"]:
        estado = f'<div class="hu-dec">Anulada: {e(f["motivo_no_vigente"] or "")}</div>'
    elif f["estado_auto"] == "farma":
        estado = (f'<div class="hu-dec">Ya es interés en farma, pactivo <b>{e(f.get("pactivo_f1") or "—")}</b>: '
                  f'se entrega por el canal farma. Aquí es sólo informativa.</div>')
    else:
        estado = ""
    acciones = ""
    if f["vigente"] and not f["decision"] and f["estado_auto"] != "farma":
        acciones = f"""
<form class="linea decidir" method="post" action="/decidir">
  <input type="hidden" name="id" value="{f['id']}"><input type="hidden" name="next" value="{e(aqui)}">
  <select name="categoria" title="Categoría">{opts}</select>
  <input class="motivo" name="motivo" placeholder="Motivo (opcional)">
  <button name="decision" value="aprobado" class="b-ok">Aprobar</button>
  <button name="decision" value="rechazado" class="b-no">No aprobar</button>
</form>"""
    elif f["decision"]:
        acciones = f"""
<form class="linea decidir" method="post" action="/decidir">
  <input type="hidden" name="id" value="{f['id']}"><input type="hidden" name="next" value="{e(aqui)}">
  <button name="decision" value="deshacer" class="b-neutro">Deshacer revisión</button>
</form>"""
    return f"""
<div class="fila-aprob {clase}" id="f{f['id']}">
  <div class="meta-aprob">
    <span class="badge b-cat">{e(c['linea'])} · {e(c['nombre'])}</span>
    <span class="badge b-sen">{e(SENALES.get(f['senal'], f['senal']))}</span>
    <span class="ap-lic">{e(FUENTES.get(f['tabla_origen'], f['tabla_origen']))} <b>{e(f['licitacion'] or '')}</b></span>
    <span class="ap-pub">publicada {_fmt(f['fecha_publicacion'])}</span>
    <span class="{'ap-pub' if cerrada else 'ap-cierre'}">{'cerró' if cerrada else 'cierra'} {_fmt(cierre)}</span>
  </div>
  <div class="desc-aprob">{e(f['descripcion'] or '')}</div>
  {titulo}
  <div class="meta">Términos: <b>{e(f['terminos'] or '—')}</b> · ONU {e(f['codigo_onu'] or '—')} {e(f['nombre_onu'] or '')}{otras}</div>
  <div class="meta">Fase 1: {e(_via(f))}</div>
  {estado}
  {acciones}
  <div class="resultado"></div>
</div>"""


def _tabs(filtros: dict, conteos: dict) -> str:
    out = []
    for v, (nombre, _w) in VISTAS.items():
        url = "/revision?" + urlencode({**filtros, "vista": v, "p": 1})
        out.append(f'<a class="tab{" on" if v == filtros["vista"] else ""}" href="{html.escape(url)}">'
                   f'{nombre} <span>{conteos.get(v, 0)}</span></a>')
    out.append(f'<span class="tab-meta">revisadas hoy: {conteos.get("hoy", 0)}</span>')
    return '<div class="tabs">' + "".join(out) + "</div>"


def _form_filtros(filtros: dict, cats: list, por_cat: dict) -> str:
    e = html.escape

    def sel(nombre, opciones, actual):
        return (f'<select name="{nombre}">' + "".join(
            f'<option value="{e(k)}"{" selected" if k == actual else ""}>{e(v)}</option>' for k, v in opciones) + "</select>")
    ocat = [("", "Todas las categorías")] + [(c["codigo"], f'{c["linea"]} · {c["nombre"]} ({por_cat.get(c["codigo"], 0)})')
                                            for c in cats]
    ofu = [("", "Todas las fuentes")] + list(FUENTES.items())
    ose = [("", "Todas las señales")] + list(SENALES.items())
    return f"""
<form class="linea filtros" method="get" action="/revision">
  <input type="hidden" name="vista" value="{e(filtros['vista'])}">
  {sel('cat', ocat, filtros['cat'])}{sel('fuente', ofu, filtros['fuente'])}{sel('senal', ose, filtros['senal'])}
  <label>Días</label><input type="number" name="dias" value="{filtros['dias']}" min="1" max="365" style="width:70px">
  <input name="q" value="{e(filtros['q'])}" placeholder="Buscar en glosa, título o código">
  <button>Filtrar</button>
</form>"""


def _paginador(filtros: dict, p: int, paginas: int) -> str:
    if paginas <= 1:
        return ""
    links = []
    for n in sorted({1, max(1, p - 1), p, min(paginas, p + 1), paginas}):
        url = "/revision?" + urlencode({**filtros, "p": n})
        links.append(f'<a class="{"on" if n == p else ""}" href="{html.escape(url)}">{n}</a>')
    return '<div class="paginas">' + " ".join(links) + "</div>"


def _salud_html(corrida: dict | None) -> str:
    if not corrida:
        return '<span class="salud bad">sin barridos</span>'
    if corrida["error"]:
        return f'<span class="salud bad" title="{html.escape(corrida["error"])}">barrido con error</span>'
    hace = corrida["hace"] or 0
    clase = "ok" if hace <= 25 else ("warn" if hace <= 60 else "bad")
    return f'<span class="salud {clase}">barrido hace {hace} min</span>'


def _layout(request: Request, titulo: str, cuerpo: str, corrida: dict | None) -> str:
    u = request.session.get("usuario") or {}
    nombre = html.escape(u.get("name", ""))
    inicial = html.escape((u.get("name") or "?")[:1].upper())
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fase 2 · {html.escape(titulo)}</title><style>{CSS}</style></head><body>
<header><b>Fase 2 · Device / Servicio técnico</b>
<nav><a href="/revision">Revisión</a><a href="/resumen">Resumen</a>{_salud_html(corrida)}
<span class="usuario"><span class="avatar">{inicial}</span>{nombre}<a href="/logout">salir</a></span></nav></header>
<main><h1>{html.escape(titulo)}</h1>{cuerpo}</main>
<script>{JS}</script></body></html>"""


def _pagina_login(error: str = "", next_url: str = "/revision", email: str = "") -> str:
    e = html.escape
    msg = f'<p class="error">{e(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Fase 2 · Ingreso</title>
<style>{CSS}</style></head><body class="login"><form class="tarjeta" method="post" action="/login">
<h1>Fase 2 · Device / Servicio técnico</h1><p class="meta">Ingresa con tu cuenta del gestor.</p>{msg}
<input type="hidden" name="next" value="{e(next_url)}">
<input name="email" type="email" placeholder="Correo" value="{e(email)}" required autofocus>
<input name="password" type="password" placeholder="Contraseña" required>
<button>Ingresar</button></form></body></html>"""


# Estilo copiado del panel del clasificador (api/ui.py), sin importarlo: la
# fase 2 no depende de archivos de la fase 1.
CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background: #eef1f4; color: #1d2330; line-height: 1.5; }
header { background: #16263d; color: #fff; padding: 14px 28px; display: flex; align-items: center;
         justify-content: space-between; flex-wrap: wrap; gap: 12px; }
header b { font-size: 18px; }
header nav { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }
header nav a { color: #b9c6d6; text-decoration: none; margin-left: 20px; font-size: 14px; }
header nav a:hover { color: #fff; }
header .usuario { display: inline-flex; align-items: center; gap: 8px; margin-left: 22px; padding: 5px 10px;
                  background: rgba(255,255,255,.08); border-radius: 999px; font-size: 13px; color: #d6e0ec; }
header .usuario .avatar { width: 22px; height: 22px; border-radius: 50%; background: linear-gradient(135deg,#6cf,#7df0a8);
                          color: #07101f; font-weight: 700; font-size: 11px; display: grid; place-items: center; }
header .usuario a { margin-left: 4px; color: #93a4c0; font-size: 12px; }
.salud { margin-left: 18px; font-size: 12px; padding: 3px 9px; border-radius: 999px; }
.salud.ok { background: #1b6b3a; } .salud.warn { background: #b07a12; } .salud.bad { background: #a8322a; }
main { max-width: 1120px; margin: 24px auto; padding: 0 16px; }
h1 { font-size: 20px; margin-bottom: 14px; }
h2 { font-size: 15px; color: #6b7689; margin: 22px 0 10px; }
table { width: 100%; background: #fff; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08);
        border-collapse: collapse; margin-bottom: 8px; }
th, td { text-align: left; padding: 8px 12px; font-size: 13.5px; }
th { background: #f3f6fa; color: #6b7689; }
tr + tr td { border-top: 1px solid #eef1f4; }
.tabs { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
.tab { padding: 7px 14px; border-radius: 8px; background: #fff; color: #1d2330; text-decoration: none; font-size: 14px;
       box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.tab span { background: #eef1f4; border-radius: 999px; padding: 0 8px; margin-left: 4px; font-size: 12px; }
.tab.on { background: #16263d; color: #fff; } .tab.on span { background: rgba(255,255,255,.18); }
.tab-meta { margin-left: auto; font-size: 13px; color: #6b7689; }
.linea { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 8px 0; }
.linea label { font-size: 12px; color: #6b7689; }
.linea input, .linea select { padding: 6px 8px; border: 1px solid #cdd5e0; border-radius: 6px; font-size: 13px; max-width: 100%; }
.linea input.motivo, .filtros input[name=q] { flex: 1; min-width: 180px; }
button { padding: 6px 14px; border: 0; border-radius: 6px; background: #2f6fb0; color: #fff; font-size: 13px; cursor: pointer; }
button.b-ok { background: #1b6b3a; } button.b-no { background: #a8322a; } button.b-neutro { background: #6b7689; }
.meta { font-size: 12.5px; color: #6b7689; margin: 4px 0; }
.vacio { padding: 30px; text-align: center; color: #6b7689; background: #fff; border-radius: 10px; }
.fila-aprob { border: 2px solid #c5a72b; border-left-width: 7px; background: #fff8d7; border-radius: 9px;
              padding: 12px 16px; margin-bottom: 10px; }
.fila-aprob.t-verde { border-color: #1b6b3a; background: #e6f5ec; }
.fila-aprob.t-aprobada { border-color: #144a26; background: #d8efe0; }
.fila-aprob.t-rechazada { border-color: #c0392b; background: #fbe4e1; }
.fila-aprob.t-anulada { border-color: #95a5b8; background: #eef0f3; }
.fila-aprob.t-farma { border-color: #2f6fb0; background: #e8f0fa; }
.fila-aprob .desc-aprob { font-size: 16px; font-weight: 600; line-height: 1.35; margin: 6px 0; word-break: break-word; }
.fila-aprob .meta-aprob { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; font-size: 13px; }
.fila-aprob .ap-pub { color: #6b7689; } .fila-aprob .ap-cierre { color: #c0392b; font-weight: 600; }
.fila-aprob .ap-titulo { font-size: 12.5px; color: #44506a; background: rgba(255,255,255,.55); border-radius: 6px;
                         padding: 5px 10px; margin: 4px 0; word-break: break-word; }
.fila-aprob .ap-tag { font-weight: 700; color: #2f6fb0; margin-right: 6px; text-transform: uppercase; font-size: 11px; }
.hu-dec { background: rgba(255,255,255,.6); border-left: 3px solid #1b6b3a; padding: 5px 10px; margin: 6px 0; font-size: 13px; }
.resultado { font-size: 13px; font-weight: 600; }
.badge { display: inline-block; padding: 2px 9px; border-radius: 20px; font-size: 12px; font-weight: 600; }
.b-cat { background: #16263d; color: #fff; } .b-sen { background: #fff; color: #44506a; border: 1px solid #cdd5e0; }
.paginas { display: flex; gap: 6px; margin: 14px 0; } .paginas a { padding: 4px 10px; background: #fff; border-radius: 6px;
  text-decoration: none; color: #1d2330; } .paginas a.on { background: #16263d; color: #fff; }
body.login { display: grid; place-items: center; min-height: 100vh; padding: 16px; }
.tarjeta { background: #fff; padding: 28px; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,.1); width: 100%;
           max-width: 380px; display: grid; gap: 12px; }
.tarjeta input { padding: 10px; border: 1px solid #cdd5e0; border-radius: 7px; font-size: 14px; }
.tarjeta .error { color: #a8322a; font-size: 13px; }
@media (max-width: 640px) { header { padding: 12px 16px; } header nav a { margin-left: 12px; } }
"""

# Envía la decisión sin recargar la página; sin JS el formulario funciona igual
# (POST normal + redirect). Sin saltos de línea dentro de strings JS.
JS = """
document.querySelectorAll('form.decidir').forEach(function (f) {
  f.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var btn = ev.submitter, fd = new FormData(f);
    if (btn) { fd.append(btn.name, btn.value); }
    var card = f.closest('.fila-aprob'), out = card.querySelector('.resultado');
    f.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    fetch('/decidir', {method: 'POST', body: fd, headers: {'Accept': 'application/json'}})
      .then(function (r) { return r.json().then(function (j) { return [r.ok, j]; }); })
      .then(function (res) {
        var ok = res[0], j = res[1];
        if (!ok || !j.ok) { throw new Error(j.error || 'Error al guardar'); }
        card.classList.remove('t-revision', 't-verde', 't-aprobada', 't-rechazada');
        if (j.decision === 'aprobado') { card.classList.add('t-aprobada'); out.textContent = 'Aprobada por ' + j.por; }
        else if (j.decision === 'rechazado') { card.classList.add('t-rechazada'); out.textContent = 'No aprobada por ' + j.por; }
        else { out.textContent = 'Revisión deshecha. Recarga para verla de nuevo.'; }
        f.remove();
      })
      .catch(function (e) {
        out.textContent = e.message;
        f.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
      });
  });
});
"""
