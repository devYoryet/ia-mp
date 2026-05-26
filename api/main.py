"""Panel del Clasificador IA — FastAPI.

Vistas:
- /            Resumen
- /comparacion Backtest (IA vs personas) — gasto del backtest Y acumulado
- /revision    Cola de revisión paginada y filtrable. Cada fila marcada como
               INTERÉS / DESCARTE / PACTIVO NUEVO. El pactivo se elige del
               catálogo y la composición/presentación se filtran por pactivo
               (igual que el legacy de gestor_licitaciones). Aprobar la hoja,
               corregir o descartar una línea (con motivo obligatorio).
- /reglas      Reglas de negocio y correcciones (feedback al prompt)
- /legacy      Módulos portados desde gestor_oc Laravel (Subida TD,
               Importaciones, Adjudicaciones, Item Detalle). Cada uno recibe
               un archivo por chunks y lanza un script de bin/ en background.
- /api/catalogo  comp/pres válidas de un pactivo (para los selects dependientes)

Servir detrás de nginx en https://iabot.pharmatender.cl
    uvicorn api.main:app --host 0.0.0.0 --port 8800
"""

from __future__ import annotations

import html
import pathlib
import sys
import time
from datetime import datetime

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from config import config  # noqa: E402
from db import conectar  # noqa: E402
from descarte_modelo import cargar_modelo_descarte, prob_descarte  # noqa: E402
from reglas import normalizar  # noqa: E402

from api.auth import router as auth_router, usuario_actual  # noqa: E402
from api.legacy import router as legacy_router  # noqa: E402
from api.ui import layout as _layout  # noqa: E402

app = FastAPI(title="Clasificador IA — Pharmatender")
app.include_router(auth_router)
app.include_router(legacy_router)


# Rutas que NO requieren login. /salud queda público para el healthcheck del
# container; /api/catalogo se usa por el front del propio panel (ya logueado).
_RUTAS_PUBLICAS = {"/login", "/logout", "/salud"}


@app.middleware("http")
async def proteger(request: Request, call_next):
    """Si no hay sesión, redirige a /login conservando la URL pretendida."""
    ruta = request.url.path
    if ruta in _RUTAS_PUBLICAS:
        return await call_next(request)
    if usuario_actual(request) is None:
        # Conservar el destino solo para GETs (un POST sin sesión queda perdido
        # de todos modos y reintentarlo con next= sería confuso).
        if request.method == "GET":
            destino = ruta + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={destino}", status_code=303)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# SessionMiddleware se agrega ÚLTIMO: en Starlette, el último middleware
# agregado es el más externo. Necesitamos que envuelva a `proteger` para que
# cuando `proteger` lea `request.session`, la cookie ya esté decodificada.
# Cookie firmada (HMAC + itsdangerous), 8h de vida — alcanza para una jornada.
app.add_middleware(
    SessionMiddleware,
    secret_key=config.session_secret,
    session_cookie="iabot_sess",
    max_age=8 * 3600,
    same_site="lax",
    https_only=False,  # nginx termina TLS; la cookie viaja en HTTP interno.
)

TABLAS_VALIDAS = ("compra_agil", "Licitaciones_diarias")
POR_HOJA_DEFAULT = 50  # filas por hoja en la cola de revisión (configurable)
POR_HOJA_OPCIONES = (25, 50, 100, 200)

def _query(sql: str, args=()) -> list:
    conn = conectar()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())
    finally:
        conn.close()


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


# Modelo de descarte entrenado — se carga una vez para mostrar, por fila, la
# etiqueta de "entrenamiento": qué dice el clasificador entrenado (interés o
# descarte), independiente de qué etapa de la cascada resolvió la fila.
_MODELO_DESCARTE = None
_MODELO_CARGADO = False


def _modelo_descarte():
    global _MODELO_DESCARTE, _MODELO_CARGADO
    if not _MODELO_CARGADO:
        try:
            _MODELO_DESCARTE = cargar_modelo_descarte()
        except Exception:  # noqa: BLE001
            _MODELO_DESCARTE = None
        _MODELO_CARGADO = True
    return _MODELO_DESCARTE


# ----------------------------------------------------------------- Catálogo ---
# Catálogo de clasificación EN MEMORIA: {pactivo_normalizado: {nombre, comp, pres}}.
# Mismo origen que el legacy de gestor_licitaciones: 0001_td_oc.Base ∪ la tabla
# `diccionario`. Los selects dependientes pactivo→comp→pres salen de acá sin
# tocar la BD en cada interacción.
#
# El catálogo NO es fijo: CRECE a medida que se ingresan pactivos/composiciones/
# presentaciones nuevos. Por eso se refresca solo cada `_CATALOGO_TTL` — un panel
# de larga vida toma las altas nuevas sin reiniciar.
_CATALOGO_TTL = 8 * 3600  # segundos (8 horas)
_CATALOGO_CACHE = None
_CATALOGO_TS = 0.0


def _catalogo(forzar: bool = False) -> dict:
    global _CATALOGO_CACHE, _CATALOGO_TS
    if (not forzar and _CATALOGO_CACHE is not None
            and time.time() - _CATALOGO_TS < _CATALOGO_TTL):
        return _CATALOGO_CACHE
    nombre, comp, pres = {}, {}, {}
    conn = conectar()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT Pactivo p, Comp c, MedidaPHT m "
                f"FROM `{config.db_catalogo}`.Base "
                f"WHERE Pactivo IS NOT NULL AND Pactivo<>''"
            )
            filas = list(cur.fetchall())
            cur.execute(
                f"SELECT DISTINCT pactivo p, comp c, presentacion m "
                f"FROM `{config.db_diccionario}`.diccionario "
                f"WHERE pactivo IS NOT NULL AND pactivo<>''"
            )
            filas += list(cur.fetchall())
    finally:
        conn.close()
    for r in filas:
        p = (r["p"] or "").strip()
        if not p:
            continue
        k = normalizar(p)
        nombre.setdefault(k, p)
        if r["c"] and r["c"].strip():
            comp.setdefault(k, set()).add(r["c"].strip())
        if r["m"] and r["m"].strip():
            pres.setdefault(k, set()).add(r["m"].strip())
    _CATALOGO_CACHE = {
        k: {
            "nombre": nombre[k],
            "comp": sorted(comp.get(k, set())),
            "pres": sorted(pres.get(k, set())),
        }
        for k in nombre
    }
    _CATALOGO_TS = time.time()
    return _CATALOGO_CACHE


@app.get("/api/catalogo")
def api_catalogo(pactivo: str = ""):
    """Composiciones y presentaciones válidas de un pactivo (selects dependientes)."""
    info = _catalogo().get(normalizar(pactivo))
    if not info:
        return {"comp": [], "pres": [], "encontrado": False}
    return {"comp": info["comp"], "pres": info["pres"], "encontrado": True}


def _select(nombre: str, fila_n: int, clase: str, valor: str, opciones: list) -> str:
    """<select> con un valor actual + 'Sin Cla' + las opciones del catálogo."""
    valor = (valor or "").strip()
    vals: list[str] = []
    for v in [valor, "Sin Cla", *opciones]:
        v = (v or "").strip()
        if v and v not in vals:
            vals.append(v)
    ops = "".join(
        f"<option value=\"{_e(v)}\"{' selected' if v == valor else ''}>{_e(v)}</option>"
        for v in vals
    )
    return (
        f"<select name={nombre} class='{clase}' data-row='{fila_n}' "
        f"data-sug=\"{_e(valor)}\">{ops}</select>"
    )


# ---------------------------------------------------------------- Resumen ---
@app.get("/", response_class=HTMLResponse)
def resumen(request: Request) -> str:
    usuario = usuario_actual(request)
    try:
        log = _query(
            "SELECT COUNT(*) n, SUM(revisado=0) pend, SUM(revisado=1) rev, "
            "SUM(revisado=1 AND feedback_correcto=1) ok FROM clasificador_ia_log"
        )[0]
        # El gasto real (todo lo cobrado por la API) sale del libro de costos,
        # que no se trunca entre backtests — a diferencia del log de clasificación.
        costo = _query(
            "SELECT IFNULL(SUM(costo_usd),0) c FROM clasificador_ia_costos"
        )[0]["c"]
        # Pendientes DE PROCESAR: filas nuevas sin clasificar (estado_gestor NULL
        # y sin clasificador) — lo que el worker tiene por delante.
        pend_proc = {}
        for _t in TABLAS_VALIDAS:
            pend_proc[_t] = _query(
                f"SELECT COUNT(*) n FROM `{_t}` WHERE estado_gestor IS NULL "
                f"AND (nombre_clasificador IS NULL OR nombre_clasificador='')"
            )[0]["n"]
    except Exception as exc:  # noqa: BLE001
        return _layout(
            "Error",
            f"<div class=vacio>No se pudo leer la base.<br><small>{_e(exc)}</small><br><br>"
            "¿Creaste las tablas con <code>schema/auditoria.sql</code>?</div>",
            usuario=usuario,
        )
    rev = log["rev"] or 0
    prec = f"{(log['ok'] or 0) / rev * 100:.1f}%" if rev else "—"
    cuerpo = (
        "<h1>Resumen</h1>"
        "<h2>Pendientes de procesar (filas nuevas sin clasificar)</h2><div class=cards>"
        f"<div class=card><div class=n>{pend_proc['compra_agil']:,}</div>"
        f"<div class=l>Pendientes compra ágil</div></div>"
        f"<div class=card><div class=n>{pend_proc['Licitaciones_diarias']:,}</div>"
        f"<div class=l>Pendientes licitaciones</div></div>"
        "</div>"
        "<h2>Clasificación IA</h2><div class=cards>"
        f"<div class=card><div class=n>{log['n']}</div><div class=l>Clasificadas por IA</div></div>"
        f"<div class=card><div class=n>{log['pend'] or 0}</div><div class=l>Pendientes de revisión</div></div>"
        f"<div class=card><div class=n>{rev}</div><div class=l>Revisadas</div></div>"
        f"<div class=card><div class=n>{prec}</div><div class=l>Precisión (revisadas)</div></div>"
        f"<div class=card><div class=n>${float(costo):.2f}</div>"
        f"<div class=l>Gastado de ${config.budget_usd:.0f} (acumulado)</div></div>"
        "</div>"
        "<div class=aviso>Modo actual del worker: <b>" + _e(config.modo) + "</b>. "
        "La cola de revisión se llena cuando el worker corre en modo producción.</div>"
    )
    return _layout("Resumen", cuerpo, usuario=usuario)


# ------------------------------------------------------------ Comparación ---
# Cross matrix (humano × IA). Las 6 celdas + sus condiciones SQL. Click en cualquiera
# de las cards lleva a la lista de filas que cayeron ahí — para auditar puntualmente.
# Notación: hX_iY = humano=X, ia=Y. _NUEVO es ia.pactivo_nuevo IS NOT NULL.
_CELDAS = {
    # name : (etiqueta, color, SQL condition)
    "h1_i1":   ("✓ Acuerdo INTERÉS",    "ok",   "humano_estado_gestor=1 AND ia_interes=1 AND (ia_pactivo_nuevo IS NULL OR ia_pactivo_nuevo='')"),
    "h1_i0":   ("✗ FALSO NEGATIVO",      "fn",   "humano_estado_gestor=1 AND ia_interes=0"),
    "h1_nuevo":("⚠ Humano interés, IA nuevo", "warn", "humano_estado_gestor=1 AND ia_pactivo_nuevo IS NOT NULL AND ia_pactivo_nuevo<>''"),
    "h0_i1":   ("✗ FALSO POSITIVO",      "fp",   "humano_estado_gestor=0 AND ia_interes=1 AND (ia_pactivo_nuevo IS NULL OR ia_pactivo_nuevo='')"),
    "h0_i0":   ("✓ Acuerdo DESCARTE",    "ok",   "humano_estado_gestor=0 AND ia_interes=0"),
    "h0_nuevo":("⚠ Humano descarte, IA nuevo", "warn", "humano_estado_gestor=0 AND ia_pactivo_nuevo IS NOT NULL AND ia_pactivo_nuevo<>''"),
}


@app.get("/comparacion", response_class=HTMLResponse)
def comparacion(request: Request, cell: str = "", metodo: str = "",
                pactivo_sug: str = "", limit: int = 100) -> str:
    usuario = usuario_actual(request)
    # Si vienen filtros de drill-down, mostrar listado en vez de dashboard.
    if cell or metodo or pactivo_sug:
        return _comparacion_listado(cell, metodo, pactivo_sug, limit, usuario)
    return _comparacion_dashboard(usuario)


def _comparacion_dashboard(usuario: dict | None = None) -> str:
    try:
        tot = _query("SELECT COUNT(*) n FROM clasificador_ia_backtest")[0]["n"]
        if not tot:
            return _layout(
                "Backtest",
                "<h1>Backtest · IA vs personas</h1><div class=vacio>Aún no hay filas "
                "comparadas. El container <code>backtest</code> está procesando 200 "
                "filas cada 15 min — volvé en un rato.</div>",
                usuario=usuario,
            )
        r = _query(
            "SELECT COUNT(*) n, SUM(coincide_interes) ci, "
            "SUM(coincide_pactivo) cp, COUNT(coincide_pactivo) ncp, "
            "SUM(coincide_composicion) cc, SUM(coincide_presentacion) cpr, "
            "IFNULL(SUM(costo_usd),0) costo, IFNULL(AVG(costo_usd),0) prom "
            "FROM clasificador_ia_backtest"
        )[0]
        # Cross matrix: conteos de cada celda en una sola query (CASE WHEN ... THEN 1)
        case_parts = ", ".join(
            f"SUM(CASE WHEN {sql} THEN 1 ELSE 0 END) AS {name}"
            for name, (_, _, sql) in _CELDAS.items()
        )
        matriz = _query(f"SELECT {case_parts} FROM clasificador_ia_backtest")[0]
        # Por método — cuánto resuelve cada etapa, accuracy de pact, costo
        por_metodo = _query(
            "SELECT ia_metodo, COUNT(*) n, "
            "SUM(coincide_interes) ci, "
            "SUM(coincide_pactivo) cp, COUNT(coincide_pactivo) ncp, "
            "IFNULL(SUM(costo_usd),0) costo "
            "FROM clasificador_ia_backtest "
            "GROUP BY ia_metodo ORDER BY n DESC"
        )
        # Top pactivos sospechosos: IA sugirió X, humano descartó. Min N=5 para no
        # pescar ruido de clases con 1-2 filas.
        sospechosos = _query(
            "SELECT ia_pactivo p, "
            "COUNT(*) n, "
            "SUM(humano_estado_gestor=0) descartes, "
            "ROUND(SUM(humano_estado_gestor=0)/COUNT(*)*100,1) pct_desc "
            "FROM clasificador_ia_backtest "
            "WHERE ia_interes=1 AND ia_pactivo IS NOT NULL "
            "GROUP BY ia_pactivo HAVING n >= 5 AND pct_desc >= 50 "
            "ORDER BY pct_desc DESC, n DESC LIMIT 25"
        )
        acum = _query("SELECT IFNULL(SUM(costo_usd),0) c FROM clasificador_ia_costos")[0]["c"]
    except Exception as exc:  # noqa: BLE001
        return _layout("Error", f"<div class=vacio>{_e(exc)}</div>", usuario=usuario)

    def pct(ok, t):
        return f"{(ok or 0) / t * 100:.1f}%" if t else "—"

    proy = float(r["prom"] or 0) * config.filas_mes_estimado
    cards = (
        "<div class=cards>"
        f"<div class=card><div class=n>{r['n']:,}</div><div class=l>Filas comparadas</div></div>"
        f"<div class=card><div class=n>{pct(r['ci'], r['n'])}</div><div class=l>Acierto interés</div></div>"
        f"<div class=card><div class=n>{pct(r['cp'], r['ncp'])}</div><div class=l>Acierto pactivo</div></div>"
        f"<div class=card><div class=n>{pct(r['cc'], r['ncp'])}</div><div class=l>Acierto comp</div></div>"
        f"<div class=card><div class=n>{pct(r['cpr'], r['ncp'])}</div><div class=l>Acierto pres</div></div>"
        f"<div class=card><div class=n>${float(r['costo']):.2f}</div><div class=l>Costo backtest</div></div>"
        f"<div class=card><div class=n>${float(r['prom'] or 0)*1000:.2f}</div><div class=l>$ / 1.000 filas</div></div>"
        f"<div class=card><div class=n>${proy:,.0f}</div><div class=l>Proyección/mes</div></div>"
        "</div>"
    )

    # Cross matrix 2x3 — cards clickeables, color según naturaleza
    matriz_html = ["<h2>Cruz humano vs IA — click en cualquier celda para auditar</h2>",
                   "<div class='matriz'>"]
    for name, (etiq, color, _) in _CELDAS.items():
        n = matriz.get(name, 0) or 0
        matriz_html.append(
            f"<a class='mcell m-{color}' href='/comparacion?cell={name}'>"
            f"<div class=mn>{n:,}</div>"
            f"<div class=ml>{etiq}</div>"
            f"</a>"
        )
    matriz_html.append("</div>")

    # Tabla por método con link
    metodo_rows = []
    for m in por_metodo:
        nm = m["ia_metodo"] or "?"
        etiqueta = _METODOS.get(nm, nm)
        ai = pct(m["ci"], m["n"])
        ap = pct(m["cp"], m["ncp"]) if m["ncp"] else "—"
        costo = float(m["costo"] or 0)
        metodo_rows.append(
            f"<tr><td><a href='/comparacion?metodo={nm}'>{_e(etiqueta)}</a></td>"
            f"<td>{m['n']:,}</td><td>{ai}</td><td>{ap}</td>"
            f"<td>${costo:.4f}</td></tr>"
        )
    metodo_html = (
        "<h2>Por etapa de la cascada</h2><table>"
        "<tr><th>Vía</th><th>Filas</th><th>Acierto interés</th>"
        "<th>Acierto pactivo</th><th>Costo</th></tr>"
        + "".join(metodo_rows) + "</table>"
    )

    # Top pactivos sospechosos — el "Servicio de Aseo, Adjunto, Cocina" de la vida
    if sospechosos:
        sosp_rows = "".join(
            f"<tr><td><a href='/comparacion?pactivo_sug={_e(s['p'])}'>{_e(s['p'])}</a></td>"
            f"<td>{s['n']:,}</td><td>{s['descartes']:,}</td>"
            f"<td><b>{s['pct_desc']}%</b></td></tr>"
            for s in sospechosos
        )
        sosp_html = (
            "<h2>Pactivos sospechosos — IA dijo interés pero humano descarta seguido</h2>"
            "<p style='font-size:13px;color:#6b7689'>Filtro: ≥5 filas con ese pactivo y ≥50% descartadas. "
            "Candidatos a regla automática \"este pactivo → descarte\".</p>"
            "<table><tr><th>Pactivo sugerido por IA</th><th>Filas</th>"
            "<th>Descartes humanos</th><th>% descarte</th></tr>"
            + sosp_rows + "</table>"
        )
    else:
        sosp_html = ("<h2>Pactivos sospechosos</h2><div class=vacio>"
                     "Sin pactivos con ≥50% de descartes humanos (mínimo 5 filas). 🎉</div>")

    coste_acum = (
        f"<p style='font-size:13px;color:#6b7689'>Gasto total acumulado de la cuenta: "
        f"<b>${float(acum):.2f}</b> de ${config.budget_usd:.0f} (mensual)</p>"
    )

    cuerpo = (
        "<h1>Backtest · IA vs personas</h1>"
        + cards + coste_acum
        + "".join(matriz_html)
        + metodo_html
        + sosp_html
    )
    return _layout("Backtest", cuerpo, usuario=usuario)


def _comparacion_listado(cell: str, metodo: str, pactivo_sug: str, limit: int,
                         usuario: dict | None = None) -> str:
    """Drill-down: muestra las filas que caen en un filtro específico."""
    limit = max(20, min(500, limit))
    cond = []
    args: list = []
    titulo_bits = []
    if cell in _CELDAS:
        etiq, _, sql = _CELDAS[cell]
        cond.append(sql)
        titulo_bits.append(etiq)
    if metodo in _METODOS:
        cond.append("ia_metodo=%s")
        args.append(metodo)
        titulo_bits.append(f"vía {_METODOS[metodo]}")
    if pactivo_sug:
        cond.append("ia_pactivo=%s")
        args.append(pactivo_sug)
        titulo_bits.append(f"pactivo IA = «{pactivo_sug}»")

    where = " AND ".join(cond) if cond else "1=1"
    try:
        total = _query(
            f"SELECT COUNT(*) n FROM clasificador_ia_backtest WHERE {where}",
            tuple(args),
        )[0]["n"]
        filas = _query(
            "SELECT tabla_origen, fila_id, descripcion, humano_estado_gestor, "
            "humano_pactivo, humano_composicion, humano_presentacion, "
            "ia_interes, ia_pactivo, ia_composicion, ia_presentacion, "
            "ia_metodo, ia_razon, ia_pactivo_nuevo, creado_en "
            f"FROM clasificador_ia_backtest WHERE {where} "
            "ORDER BY creado_en DESC LIMIT %s",
            tuple(args) + (limit,),
        )
    except Exception as exc:  # noqa: BLE001
        return _layout("Error", f"<div class=vacio>{_e(exc)}</div>", usuario=usuario)

    bloques = []
    for f in filas:
        ia_pact = f.get("ia_pactivo") or f.get("ia_pactivo_nuevo") or "—"
        hp = f.get("humano_pactivo") or "—"
        ia_int = "interés" if f.get("ia_interes") == 1 else "descarte"
        hu_int = "interés" if f.get("humano_estado_gestor") == 1 else "descarte"
        bloques.append(
            f"<div class=fila>"
            f"<div class=meta><b>{_e(f['tabla_origen'])} #{f['fila_id']}</b> · "
            f"<span class='badge b-met'>vía {_e(_METODOS.get(f.get('ia_metodo'), f.get('ia_metodo') or '?'))}</span>"
            f"</div>"
            f"<div class=desc>{_e((f.get('descripcion') or '')[:280])}</div>"
            f"<div class=razon>IA: {_e(f.get('ia_razon') or '')}</div>"
            "<div class=lineh>"
            f"<div><b>Humano</b> ({hu_int}): {_e(hp)}"
            f" · {_e(f.get('humano_composicion') or '—')}"
            f" · {_e(f.get('humano_presentacion') or '—')}</div>"
            f"<div><b>IA</b> ({ia_int}): {_e(ia_pact)}"
            f" · {_e(f.get('ia_composicion') or '—')}"
            f" · {_e(f.get('ia_presentacion') or '—')}</div>"
            "</div></div>"
        )

    titulo = " · ".join(titulo_bits) or "(todas)"
    qs_paginar = f"cell={cell}&metodo={metodo}&pactivo_sug={pactivo_sug}"
    cuerpo = (
        f"<h1>Backtest · {_e(titulo)}</h1>"
        f"<p style='font-size:13px'><b>{total:,}</b> filas encuentran este filtro · "
        f"<a href='/comparacion'>← volver al dashboard</a></p>"
        + "".join(bloques)
        + (f"<p style='font-size:13px;color:#6b7689'>Mostrando primeras {limit}. "
           f"Subir LIMIT: <a href='/comparacion?{qs_paginar}&limit=500'>ver 500</a></p>"
           if total > limit else "")
    )
    return _layout("Backtest detalle", cuerpo, usuario=usuario)


# --------------------------------------------------------------- Revisión ---
# Segregación interés vs descarte: un revisor toma `tipo=descarte` (revisar todas
# las descartas) y otro `tipo=interes` (revisar todas las de interés, con su
# pactivo/composición/presentación). `nuevo` es un subconjunto de interés —
# las que Claude propone con un pactivo fuera del catálogo.
_TIPOS = {
    # interés = de interés Y con pactivo del catálogo. Los de pactivo NUEVO
    # tienen su propia categoría aparte (no se mezclan en "interés").
    "interes": "interes_sugerido=1 AND (pactivo_nuevo IS NULL OR pactivo_nuevo='')",
    "descarte": "interes_sugerido=0",
    "nuevo": "pactivo_nuevo IS NOT NULL AND pactivo_nuevo<>''",
}

@app.get("/revision.csv")
def revision_csv(tabla: str = "", tipo: str = "", metodo: str = "", conf: str = "",
                 rango: str = "ayer_hoy", desde: str = "", hasta: str = "",
                 estado: str = "pendientes", busqueda: str = "",
                 licitacion: str = ""):
    """Export Excel-friendly (CSV con BOM, semicolon, UTF-8) de las filas que
    coinciden con el filtro actual de /revision. SIN paginar — exporta todas.

    Usado para análisis offline o auditoría. Las columnas son las mismas que
    el revisor ve en pantalla + N° licitación + fecha — sin metadata interna
    del modelo. Para data cruda usar SQL directo sobre clasificador_ia_log."""
    import csv as _csv, io as _io
    from fastapi.responses import StreamingResponse

    estado = estado if estado in _ESTADOS else "pendientes"
    if rango == "todas":
        rango = ""
    cond = [_ESTADOS[estado]]
    args: list = []
    if tabla in TABLAS_VALIDAS:
        cond.append("tabla_origen=%s"); args.append(tabla)
    if tipo in _TIPOS:
        cond.append(_TIPOS[tipo])
    if metodo in _METODOS:
        cond.append("metodo=%s"); args.append(metodo)
    if conf == "baja":
        cond.append("confianza < 0.7")
    elif conf == "media":
        cond.append("confianza >= 0.7 AND confianza < 0.85")
    elif conf == "alta":
        cond.append("confianza >= 0.85")
    if rango in _RANGOS:
        cond.append(_RANGOS[rango])
    if desde:
        cond.append("creado_en >= %s"); args.append(desde + " 00:00:00")
    if hasta:
        cond.append("creado_en <= %s"); args.append(hasta + " 23:59:59")
    if busqueda:
        cond.append("descripcion LIKE %s"); args.append(f"%{busqueda.strip()}%")
    if licitacion:
        ca_ids, li_ids = _fila_ids_por_licitacion(licitacion.strip())
        partes = []
        if ca_ids:
            ph = ",".join(["%s"] * len(ca_ids))
            partes.append(f"(tabla_origen='compra_agil' AND fila_id IN ({ph}))")
            args.extend(ca_ids)
        if li_ids:
            ph = ",".join(["%s"] * len(li_ids))
            partes.append(f"(tabla_origen='Licitaciones_diarias' AND fila_id IN ({ph}))")
            args.extend(li_ids)
        cond.append("(" + " OR ".join(partes) + ")" if partes else "1=0")
    where = " AND ".join(cond)
    filas = _query(
        "SELECT id, tabla_origen, fila_id, descripcion, interes_sugerido, "
        "pactivo_sugerido, composicion_sugerida, presentacion_sugerida, "
        "confianza, metodo, razon, pactivo_nuevo, creado_en, revisado, "
        "revisado_por, revisado_en, feedback_correcto "
        f"FROM clasificador_ia_log WHERE {where} ORDER BY creado_en DESC LIMIT 50000",
        tuple(args),
    )
    # Trae N° licitación
    num_lic: dict = {}
    por_tabla: dict = {}
    for f in filas:
        por_tabla.setdefault(f["tabla_origen"], []).append(f["fila_id"])
    for t, ids in por_tabla.items():
        if t in TABLAS_VALIDAS and ids:
            ph = ",".join(["%s"] * len(ids))
            try:
                for r in _query(
                    f"SELECT id, Licitacion FROM `{t}` WHERE id IN ({ph})",
                    tuple(ids),
                ):
                    num_lic[(t, r["id"])] = r["Licitacion"]
            except Exception:  # noqa: BLE001
                pass

    buf = _io.StringIO()
    buf.write("﻿")  # BOM para Excel ES
    w = _csv.writer(buf, delimiter=";", quoting=_csv.QUOTE_MINIMAL)
    w.writerow(["N° Licitación", "Tabla", "Fila ID", "Tipo IA", "Pactivo IA",
                "Composición", "Presentación", "Confianza", "Vía", "Razón IA",
                "Pactivo Nuevo", "Descripción", "Fecha clasif.",
                "Revisada", "Revisor", "Fecha revisión", "Acierto humano"])
    for f in filas:
        tipo_ia = "INTERÉS" if f.get("interes_sugerido") == 1 else "descarte"
        if (f.get("pactivo_nuevo") or "").strip():
            tipo_ia = "PACTIVO NUEVO"
        w.writerow([
            num_lic.get((f["tabla_origen"], f["fila_id"])) or "",
            f["tabla_origen"], f["fila_id"], tipo_ia,
            f.get("pactivo_sugerido") or "",
            f.get("composicion_sugerida") or "",
            f.get("presentacion_sugerida") or "",
            f"{float(f.get('confianza') or 0):.2f}",
            _METODOS.get(f.get("metodo"), f.get("metodo") or ""),
            f.get("razon") or "",
            f.get("pactivo_nuevo") or "",
            (f.get("descripcion") or "").replace("\n", " ").replace("\r", " "),
            f.get("creado_en").strftime("%Y-%m-%d %H:%M") if f.get("creado_en") else "",
            "sí" if f.get("revisado") else "no",
            f.get("revisado_por") or "",
            f.get("revisado_en").strftime("%Y-%m-%d %H:%M") if f.get("revisado_en") else "",
            ("sí" if f.get("feedback_correcto") == 1 else "no") if f.get("revisado") else "",
        ])
    buf.seek(0)
    nombre = f"revision-{datetime.now():%Y%m%d-%H%M}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


def qs_extras_str(tabla, tipo, metodo, conf, rango, desde, hasta, estado, busqueda, licitacion):
    """Querystring común para preservar filtros al armar links/export."""
    pares = [
        ("tabla", tabla), ("tipo", tipo), ("metodo", metodo), ("conf", conf),
        ("rango", rango), ("desde", desde), ("hasta", hasta),
        ("estado", estado if estado != "pendientes" else ""),
        ("busqueda", busqueda), ("licitacion", licitacion),
    ]
    return "&".join(f"{k}={v}" for k, v in pares if v)


# Etiquetas legibles de cada etapa de la cascada que resolvió la fila.
_METODOS = {
    "cruce_base": "cruce Base",
    "descarte_item": "descarte por rubro",
    "modelo_descarte": "clasif. de descarte",
    "modelo_pactivo": "clasif. de pactivo",
    "conflicto_regla_modelo": "regla vs. modelo",
    "historico": "histórico",
    "regla_diccionario": "reglas",
    "claude": "Claude",
}


_ESTADOS = {"pendientes": "revisado=0", "revisadas": "revisado=1", "todas": "1=1"}
_RANGOS = {
    "ayer_hoy": "creado_en >= CURDATE() - INTERVAL 1 DAY",
    "hoy": "creado_en >= CURDATE()",
    "ayer": "creado_en >= CURDATE() - INTERVAL 1 DAY AND creado_en < CURDATE()",
    "semana": "creado_en >= CURDATE() - INTERVAL 7 DAY",
    "mes": "creado_en >= CURDATE() - INTERVAL 30 DAY",
}


# Supergrupos para ordenar visualmente la cola. El revisor ve primero los
# clusters claros (interés con histórico/cruce que son los más confiables) y
# después los que requieren más atención (Claude, conflictos). Adjuntos y
# pactivos nuevos van como categorías propias.
def _supergrupo(fila: dict) -> tuple:
    """Devuelve (clave_orden, etiqueta_visible) para agrupar la fila."""
    interes = fila.get("interes_sugerido")
    metodo = fila.get("metodo") or ""
    tabla = fila.get("tabla_origen") or ""
    pact = (fila.get("pactivo_sugerido") or "").strip()
    es_nuevo = bool((fila.get("pactivo_nuevo") or "").strip())

    if es_nuevo:
        return ("z1_nuevo", "⚠ PACTIVOS NUEVOS · Claude propuso fuera de catálogo")
    if pact == "Adjunto" and tabla == "compra_agil":
        return ("y1_adj_ca", "📎 ADJUNTOS · COMPRAS ÁGILES")
    if pact == "Adjunto" and tabla == "Licitaciones_diarias":
        return ("y2_adj_li", "📎 ADJUNTOS · LICITACIONES")
    if interes == 1:
        if metodo in ("cruce_base", "historico"):
            return ("a1_int_hist", "🟢 INTERÉS · Cruce histórico (OC reales + descripción ya clasificada)")
        if metodo in ("modelo_pactivo",):
            return ("a2_int_ml", "🟢 INTERÉS · Clasificación ML (modelo entrenado de pactivo)")
        if metodo == "regla_diccionario":
            return ("a3_int_reglas", "🟢 INTERÉS · Reglas (match diccionario)")
        if metodo == "claude":
            return ("a4_int_claude", "🟢 INTERÉS · Claude")
        return ("a9_int_otro", "🟢 INTERÉS · otro")
    if interes == 0:
        if metodo == "descarte_item":
            return ("b1_desc_item", "🔴 DESCARTE · Por rubro (códigos siempre descartados)")
        if metodo == "modelo_descarte":
            return ("b2_desc_ml", "🔴 DESCARTE · Clasificador entrenado")
        if metodo == "conflicto_regla_modelo":
            return ("b3_desc_conf", "🔴 DESCARTE · Conflicto regla vs modelo")
        if metodo == "historico":
            return ("b4_desc_hist", "🔴 DESCARTE · Histórico humano")
        if metodo == "claude":
            return ("b5_desc_claude", "🔴 DESCARTE · Claude")
        return ("b9_desc_otro", "🔴 DESCARTE · otro")
    return ("zz", "otro")


def _fila_ids_por_licitacion(numero: str) -> "tuple[list, list]":
    """Busca el número de licitación/compra ágil en las 2 tablas origen y
    devuelve (compra_agil_ids, licitaciones_ids). El revisor escribe el número
    en el filtro y vemos exactamente esa fila. Match LIKE para tolerar prefijos."""
    if not numero:
        return ([], [])
    out = ([], [])
    for i, t in enumerate(TABLAS_VALIDAS):
        try:
            r = _query(
                f"SELECT id FROM `{t}` WHERE Licitacion LIKE %s LIMIT 200",
                (f"%{numero}%",),
            )
            out[i].extend(x["id"] for x in r if "id" in x)
        except Exception:  # noqa: BLE001
            pass
    return out


@app.get("/revision", response_class=HTMLResponse)
def revision(request: Request, hoja: int = 1, msg: str = "", tabla: str = "",
             tipo: str = "", metodo: str = "", conf: str = "", por_hoja: int = 0,
             estado: str = "pendientes", rango: str = "",
             desde: str = "", hasta: str = "",
             busqueda: str = "", licitacion: str = "") -> str:
    usuario = usuario_actual(request)
    hoja = max(1, hoja)
    por_hoja = por_hoja if por_hoja in POR_HOJA_OPCIONES else POR_HOJA_DEFAULT
    estado = estado if estado in _ESTADOS else "pendientes"
    # Default fecha: ayer+hoy si el usuario no especificó nada (lo que está
    # procesando ahora + lo que dejó ayer). Para ver todo: rango='todas'.
    if not rango and not desde and not hasta:
        rango = "ayer_hoy"
    if rango == "todas":
        rango = ""
    cond = [_ESTADOS[estado]]
    args: list = []
    if tabla in TABLAS_VALIDAS:
        cond.append("tabla_origen=%s")
        args.append(tabla)
    if tipo in _TIPOS:
        cond.append(_TIPOS[tipo])
    if metodo in _METODOS:
        cond.append("metodo=%s")
        args.append(metodo)
    # Filtro por banda de confianza — para priorizar los casos dudosos (<0.7)
    # o, al revés, repasar masivamente los seguros (>=0.85) con un solo OK.
    if conf == "baja":
        cond.append("confianza < 0.7")
    elif conf == "media":
        cond.append("confianza >= 0.7 AND confianza < 0.85")
    elif conf == "alta":
        cond.append("confianza >= 0.85")
    # Fecha — preset (rango) o rango personalizado (desde/hasta, formato YYYY-MM-DD).
    if rango in _RANGOS:
        cond.append(_RANGOS[rango])
    if desde:
        cond.append("creado_en >= %s")
        args.append(desde + " 00:00:00")
    if hasta:
        cond.append("creado_en <= %s")
        args.append(hasta + " 23:59:59")
    # Búsqueda de texto en la descripción (LIKE %X%)
    if busqueda:
        cond.append("descripcion LIKE %s")
        args.append(f"%{busqueda.strip()}%")
    # Filtro por número de licitación / compra ágil: pre-resuelve los fila_id
    # en la tabla origen para no JOIN-ear en cada query.
    if licitacion:
        ca_ids, li_ids = _fila_ids_por_licitacion(licitacion.strip())
        partes = []
        if ca_ids:
            ph = ",".join(["%s"] * len(ca_ids))
            partes.append(f"(tabla_origen='compra_agil' AND fila_id IN ({ph}))")
            args.extend(ca_ids)
        if li_ids:
            ph = ",".join(["%s"] * len(li_ids))
            partes.append(f"(tabla_origen='Licitaciones_diarias' AND fila_id IN ({ph}))")
            args.extend(li_ids)
        if not partes:
            cond.append("1=0")  # nada encontrado → no devuelve nada
        else:
            cond.append("(" + " OR ".join(partes) + ")")
    where = " AND ".join(cond)
    # Para REVISADAS ordeno por revisado_en DESC (lo más recientemente cerrado
    # primero — es lo que el revisor quiere ver para auditar). Para pendientes,
    # ordeno PRIMERO por supergrupo (interés cruce histórico va primero,
    # claude/conflictos al final) — el revisor procesa por bloques afines.
    if estado == "revisadas":
        orden = "revisado_en DESC"
    else:
        # Orden compuesto por supergrupo (lógico en SQL: CASE WHEN) — evita
        # re-ordenar en Python después. El orden por confianza adentro de cada
        # grupo sigue siendo válido (lo dudoso primero).
        orden = """
        CASE
          WHEN pactivo_nuevo IS NOT NULL AND pactivo_nuevo<>'' THEN 'z1'
          WHEN pactivo_sugerido='Adjunto' AND tabla_origen='compra_agil' THEN 'y1'
          WHEN pactivo_sugerido='Adjunto' AND tabla_origen='Licitaciones_diarias' THEN 'y2'
          WHEN interes_sugerido=1 AND metodo IN ('cruce_base','historico') THEN 'a1'
          WHEN interes_sugerido=1 AND metodo='modelo_pactivo' THEN 'a2'
          WHEN interes_sugerido=1 AND metodo='regla_diccionario' THEN 'a3'
          WHEN interes_sugerido=1 AND metodo='claude' THEN 'a4'
          WHEN interes_sugerido=0 AND metodo='descarte_item' THEN 'b1'
          WHEN interes_sugerido=0 AND metodo='modelo_descarte' THEN 'b2'
          WHEN interes_sugerido=0 AND metodo='conflicto_regla_modelo' THEN 'b3'
          WHEN interes_sugerido=0 AND metodo='historico' THEN 'b4'
          WHEN interes_sugerido=0 AND metodo='claude' THEN 'b5'
          ELSE 'zz'
        END, confianza ASC, creado_en DESC
        """
    try:
        total = _query(
            f"SELECT COUNT(*) n FROM clasificador_ia_log WHERE {where}", tuple(args)
        )[0]["n"]
        filas = _query(
            "SELECT id, tabla_origen, fila_id, descripcion, interes_sugerido, "
            "pactivo_sugerido, composicion_sugerida, presentacion_sugerida, "
            "confianza, razon, pactivo_nuevo, metodo, creado_en, revisado, "
            "revisado_por, revisado_en, feedback_correcto, feedback_pactivo, "
            "feedback_notas FROM clasificador_ia_log "
            f"WHERE {where} ORDER BY {orden} LIMIT %s OFFSET %s",
            tuple(args) + (por_hoja, (hoja - 1) * por_hoja),
        )
    except Exception as exc:  # noqa: BLE001
        return _layout("Error", f"<div class=vacio>{_e(exc)}</div>", usuario=usuario)

    # número de licitación / compra ágil de cada fila — vive en la tabla origen,
    # no en el log; se trae con una consulta por tabla (no una por fila).
    num_lic: dict = {}
    por_tabla: dict = {}
    for f in filas:
        por_tabla.setdefault(f["tabla_origen"], []).append(f["fila_id"])
    for t, ids in por_tabla.items():
        if t in TABLAS_VALIDAS and ids:
            ph = ",".join(["%s"] * len(ids))
            try:
                for r in _query(
                    f"SELECT id, Licitacion FROM `{t}` WHERE id IN ({ph})", tuple(ids)
                ):
                    num_lic[(t, r["id"])] = r["Licitacion"]
            except Exception:  # noqa: BLE001
                pass

    # barra de filtros (mantiene el estado de los otros filtros en cada enlace).
    # "estado" no usa default-vacío como los otros — siempre tiene valor.
    def filtro_link(clave: str, valor: str, etiqueta: str, activo: bool) -> str:
        cur = {"tabla": tabla, "tipo": tipo, "metodo": metodo,
               "conf": conf, "rango": rango, "desde": desde, "hasta": hasta,
               "busqueda": busqueda, "licitacion": licitacion,
               "estado": estado if estado != "pendientes" else "",
               "por_hoja": str(por_hoja) if por_hoja != POR_HOJA_DEFAULT else ""}
        cur[clave] = valor
        qs = "&".join(f"{k}={v}" for k, v in cur.items() if v)
        cls = " class=on" if activo else ""
        return f"<a href='/revision?{qs}'{cls}>{etiqueta}</a>"

    filtros = (
        "<div class=filtros><b>Estado:</b>"
        + filtro_link("estado", "", "pendientes", estado == "pendientes")
        + filtro_link("estado", "revisadas", "revisadas", estado == "revisadas")
        + filtro_link("estado", "todas", "todas", estado == "todas")
        + " &nbsp; <b>Tabla:</b>"
        + filtro_link("tabla", "", "todas", not tabla)
        + filtro_link("tabla", "compra_agil", "compra ágil", tabla == "compra_agil")
        + filtro_link("tabla", "Licitaciones_diarias", "licitaciones",
                      tabla == "Licitaciones_diarias")
        + " &nbsp; <b>Tipo:</b>"
        + filtro_link("tipo", "", "todos", not tipo)
        + filtro_link("tipo", "interes", "interés", tipo == "interes")
        + filtro_link("tipo", "descarte", "descarte", tipo == "descarte")
        + filtro_link("tipo", "nuevo", "pactivo nuevo", tipo == "nuevo")
        + "</div>"
        + "<div class=filtros><b>Fecha:</b>"
        + filtro_link("rango", "ayer_hoy", "ayer + hoy", rango == "ayer_hoy")
        + filtro_link("rango", "hoy", "solo hoy", rango == "hoy")
        + filtro_link("rango", "ayer", "solo ayer", rango == "ayer")
        + filtro_link("rango", "semana", "última semana", rango == "semana")
        + filtro_link("rango", "mes", "último mes", rango == "mes")
        + filtro_link("rango", "todas", "todas", not rango and not desde and not hasta)
        + (
            f" &nbsp; <form method=get action='/revision' style='display:inline;font-size:13px'>"
            f"<input type=hidden name=estado value='{_e(estado)}'>"
            f"<input type=hidden name=tabla value='{_e(tabla)}'>"
            f"<input type=hidden name=tipo value='{_e(tipo)}'>"
            f"<input type=hidden name=metodo value='{_e(metodo)}'>"
            f"<input type=hidden name=conf value='{_e(conf)}'>"
            f"<input type=hidden name=por_hoja value='{por_hoja}'>"
            f"<input type=hidden name=busqueda value='{_e(busqueda)}'>"
            f"<input type=hidden name=licitacion value='{_e(licitacion)}'>"
            f"<input type=date name=desde value='{_e(desde)}'>"
            f"&nbsp;a&nbsp;<input type=date name=hasta value='{_e(hasta)}'>"
            f"&nbsp;<button type=submit class=sec style='padding:4px 10px'>aplicar</button>"
            f"</form>"
        )
        + "</div>"
        # Búsqueda libre + por número de licitación: forms independientes
        + (
            f"<div class=filtros><b>Buscar:</b>"
            f"<form method=get action='/revision' style='display:inline;font-size:13px'>"
            f"<input type=hidden name=estado value='{_e(estado)}'>"
            f"<input type=hidden name=tabla value='{_e(tabla)}'>"
            f"<input type=hidden name=tipo value='{_e(tipo)}'>"
            f"<input type=hidden name=metodo value='{_e(metodo)}'>"
            f"<input type=hidden name=conf value='{_e(conf)}'>"
            f"<input type=hidden name=rango value='{_e(rango)}'>"
            f"<input type=hidden name=desde value='{_e(desde)}'>"
            f"<input type=hidden name=hasta value='{_e(hasta)}'>"
            f"<input type=hidden name=por_hoja value='{por_hoja}'>"
            f"<input type=hidden name=licitacion value='{_e(licitacion)}'>"
            f"<input type=text name=busqueda value='{_e(busqueda)}' placeholder='palabra en la glosa…' style='width:240px'>"
            f"&nbsp;<button type=submit class=sec style='padding:4px 10px'>buscar</button>"
            f"</form>"
            f"&nbsp;&nbsp;<b>N° licitación / compra:</b>"
            f"<form method=get action='/revision' style='display:inline;font-size:13px'>"
            f"<input type=hidden name=estado value='{_e(estado)}'>"
            f"<input type=hidden name=tabla value='{_e(tabla)}'>"
            f"<input type=hidden name=tipo value='{_e(tipo)}'>"
            f"<input type=hidden name=metodo value='{_e(metodo)}'>"
            f"<input type=hidden name=conf value='{_e(conf)}'>"
            f"<input type=hidden name=rango value='{_e(rango)}'>"
            f"<input type=hidden name=desde value='{_e(desde)}'>"
            f"<input type=hidden name=hasta value='{_e(hasta)}'>"
            f"<input type=hidden name=por_hoja value='{por_hoja}'>"
            f"<input type=hidden name=busqueda value='{_e(busqueda)}'>"
            f"<input type=text name=licitacion value='{_e(licitacion)}' placeholder='ej. 5523-145-L226' style='width:200px'>"
            f"&nbsp;<button type=submit class=sec style='padding:4px 10px'>ir</button>"
            f"</form>"
            f"&nbsp;&nbsp;<a class=sec href='/revision.csv?{_e(qs_extras_str(tabla,tipo,metodo,conf,rango,desde,hasta,estado,busqueda,licitacion))}' style='text-decoration:none;padding:4px 10px;border:1px solid #cdd5e0;border-radius:6px;background:#fff;color:#1d2330;font-size:13px'>📥 Excel (CSV)</a>"
            f"</div>"
        )
        + "<div class=filtros><b>Confianza:</b>"
        + filtro_link("conf", "", "toda", not conf)
        + filtro_link("conf", "baja", "&lt; 0.70 (duda)", conf == "baja")
        + filtro_link("conf", "media", "0.70-0.85", conf == "media")
        + filtro_link("conf", "alta", "≥ 0.85 (segura)", conf == "alta")
        + " &nbsp; <b>Vía:</b>"
        + filtro_link("metodo", "", "todas", not metodo)
        + "".join(filtro_link("metodo", k, v, metodo == k) for k, v in _METODOS.items())
        + "</div>"
        + "<div class=filtros><b>Por hoja:</b>"
        + "".join(filtro_link("por_hoja", str(n) if n != POR_HOJA_DEFAULT else "",
                              str(n), por_hoja == n)
                  for n in POR_HOJA_OPCIONES)
        + "</div>"
    )

    if not total:
        return _layout(
            "Cola de revisión",
            "<h1>Cola de revisión</h1>" + filtros
            + "<div class=vacio>No hay clasificaciones pendientes con ese filtro. 🎉</div>",
            usuario=usuario,
        )

    cat = _catalogo()
    aviso = f"<div class=aviso>{_e(msg)}</div>" if msg else ""

    def _fmt_dt(dt):
        return dt.strftime("%d-%m-%Y %H:%M") if dt else "—"

    # Pre-cuenta filas por supergrupo para inyectar headers con el total.
    # Lo calculamos sobre TODO el conjunto filtrado (no solo la hoja), porque
    # el revisor quiere saber cuántas hay en cada categoría aunque vea una.
    grupo_counts: dict = {}
    if total and estado == "pendientes":
        try:
            for r in _query(
                "SELECT COUNT(*) n, "
                "CASE "
                "  WHEN pactivo_nuevo IS NOT NULL AND pactivo_nuevo<>'' THEN 'z1' "
                "  WHEN pactivo_sugerido='Adjunto' AND tabla_origen='compra_agil' THEN 'y1' "
                "  WHEN pactivo_sugerido='Adjunto' AND tabla_origen='Licitaciones_diarias' THEN 'y2' "
                "  WHEN interes_sugerido=1 AND metodo IN ('cruce_base','historico') THEN 'a1' "
                "  WHEN interes_sugerido=1 AND metodo='modelo_pactivo' THEN 'a2' "
                "  WHEN interes_sugerido=1 AND metodo='regla_diccionario' THEN 'a3' "
                "  WHEN interes_sugerido=1 AND metodo='claude' THEN 'a4' "
                "  WHEN interes_sugerido=0 AND metodo='descarte_item' THEN 'b1' "
                "  WHEN interes_sugerido=0 AND metodo='modelo_descarte' THEN 'b2' "
                "  WHEN interes_sugerido=0 AND metodo='conflicto_regla_modelo' THEN 'b3' "
                "  WHEN interes_sugerido=0 AND metodo='historico' THEN 'b4' "
                "  WHEN interes_sugerido=0 AND metodo='claude' THEN 'b5' "
                "  ELSE 'zz' END k "
                f"FROM clasificador_ia_log WHERE {where} GROUP BY k",
                tuple(args),
            ):
                grupo_counts[r["k"]] = r["n"]
        except Exception:  # noqa: BLE001
            pass

    bloques = []
    grupo_actual = None
    for n, f in enumerate(filas):
        # Encabezado de supergrupo cuando cambia
        sg_key, sg_etiq = _supergrupo(f)
        if sg_key != grupo_actual:
            grupo_actual = sg_key
            short_key = sg_key.split("_")[0] if "_" in sg_key else sg_key
            n_grupo = grupo_counts.get(short_key, "")
            n_html = f" <span class=grupo-n>({n_grupo:,} en total)</span>" if n_grupo else ""
            bloques.append(f"<div class=grupo-hdr>{sg_etiq}{n_html}</div>")
        conf = float(f.get("confianza") or 0)
        interes = f.get("interes_sugerido")
        es_nuevo = bool((f.get("pactivo_nuevo") or "").strip())
        revisada = bool(f.get("revisado"))
        if interes == 0:
            tipo_cls, badge = "t-descarte", "<span class='badge b-desc'>DESCARTE sugerido</span>"
        elif es_nuevo:
            tipo_cls, badge = "t-nuevo", "<span class='badge b-nuevo'>PACTIVO NUEVO</span>"
        else:
            tipo_cls, badge = "", "<span class='badge b-int'>INTERÉS sugerido</span>"
        # vía: qué etapa de la cascada resolvió esta fila.
        _met = f.get("metodo")
        via = f" · <span class='badge b-met'>vía {_e(_METODOS.get(_met, _met or '?'))}</span>"

        # etiqueta de ENTRENAMIENTO: qué dice el clasificador de descarte
        # entrenado sobre esta fila, independiente de la etapa que la resolvió.
        _m = _modelo_descarte()
        ent = ""
        if _m is not None:
            pd = prob_descarte(_m, f.get("descripcion"))
            if pd >= 0.5:
                ent = f" · <span class='badge b-ent-d'>entrenamiento: DESCARTE {pd:.2f}</span>"
            else:
                ent = f" · <span class='badge b-ent-i'>entrenamiento: INTERÉS {1 - pd:.2f}</span>"

        # Encabezado común con timestamps. Para revisadas suma quién y cuándo.
        meta_html = (
            f"<div class=meta>{badge} &nbsp; "
            f"<b>Licitación {_e(num_lic.get((f['tabla_origen'], f['fila_id'])) or '—')}</b>"
            f" · {_e(f['tabla_origen'])} #{f['fila_id']} · "
            f"<span class='badge {'b-baja' if conf < 0.7 else 'b-alta'}'>confianza {conf:.2f}</span>"
            f"{via}{ent}"
            f"<div class=ts>clasificada {_fmt_dt(f.get('creado_en'))}"
            + (
                f" · revisada {_fmt_dt(f.get('revisado_en'))} "
                f"por <b>{_e(f.get('revisado_por') or '—')}</b>"
                if revisada else ""
            )
            + "</div></div>"
        )

        # FILA YA REVISADA: solo lectura, sin checkbox ni form. Muestra el
        # veredicto humano (aprobada / corregida) y sus notas para auditoría.
        if revisada:
            if f.get("feedback_correcto") == 1:
                veredicto = "<span class='badge b-int'>✓ APROBADA</span>"
            else:
                # corregida o descartada; feedback_pactivo solo si corrigió
                fp = (f.get("feedback_pactivo") or "").strip()
                fn = (f.get("feedback_notas") or "").strip()
                etiqueta = "✏ CORREGIDA" if fp else "✗ DESCARTADA"
                detalle = (f" → <b>{_e(fp)}</b>" if fp else "")
                if fn:
                    detalle += f" · motivo: {_e(fn)}"
                veredicto = f"<span class='badge b-desc'>{etiqueta}</span>{detalle}"
            bloques.append(
                f"<div class='fila revisada {tipo_cls}'>"
                f"<div class=fila-head><div style='width:24px'></div>{meta_html}</div>"
                f"<div class=desc>{_e((f.get('descripcion') or '')[:300])}</div>"
                f"<div class=razon>IA: {_e(f.get('razon'))}</div>"
                f"<div class=veredicto>{veredicto}</div>"
                f"</div>"
            )
            continue

        # FILA PENDIENTE: editable, con checkbox para procesarla en el lote.
        info = cat.get(normalizar(f.get("pactivo_sugerido") or ""))
        comps = info["comp"] if info else []
        press = info["pres"] if info else []

        aviso_nuevo = ""
        if es_nuevo:
            aviso_nuevo = (
                "<div class=nuevo-aviso>⚠ Claude no encontró este producto en el "
                f"catálogo y propone un pactivo NUEVO: <b>{_e(f['pactivo_nuevo'])}</b>. "
                "Si corresponde a un pactivo que ya existe, elígelo abajo y corrige; "
                "si de verdad es nuevo, descártalo o anótalo en el motivo para que "
                "se evalúe agregarlo al catálogo.</div>"
            )

        # Para DESCARTES la línea de pactivo/comp/pres se colapsa por default —
        # no hace falta editarla si el descarte está bien; basta con el checkbox.
        linea_oculta = " hidden" if interes == 0 and not es_nuevo else ""
        bloques.append(
            f"<div class='fila {tipo_cls}' data-row='{n}'>"
            f"<div class=fila-head>"
            f"<input type=checkbox class=marcar name=procesar value='{f['id']}' "
            f"data-row='{n}' checked>"
            f"{meta_html}</div>"
            f"<div class=desc>{_e((f.get('descripcion') or '')[:300])}</div>"
            f"<div class=razon>IA: {_e(f.get('razon'))}</div>"
            + aviso_nuevo
            + f"<input type=hidden name=log_id value='{f['id']}'>"
            f"<div class='linea linea-edicion' data-row='{n}'{linea_oculta}>"
            f"<select name=decision class=decision data-row='{n}'>"
            "<option value=aprobar selected>Aprobar</option>"
            "<option value=corregir>Corregir</option>"
            "<option value=descartar>Descartar</option></select>"
            "<label>pactivo</label>"
            f"<input name=pactivo class=f-pactivo list=lista_pactivos data-row='{n}' "
            f"data-sug=\"{_e(f.get('pactivo_sugerido'))}\" "
            f"value=\"{_e(f.get('pactivo_sugerido'))}\">"
            "<label>comp</label>"
            + _select("composicion", n, "f-comp", f.get("composicion_sugerida"), comps)
            + "<label>pres</label>"
            + _select("presentacion", n, "f-pres", f.get("presentacion_sugerida"), press)
            + "<input class=motivo name=motivo "
            "placeholder='motivo (obligatorio si corriges o descartas)'>"
            "</div></div>"
        )

    # datalist único con todos los pactivos del catálogo (autocompletado nativo)
    opts = "".join(
        f"<option value=\"{_e(v['nombre'])}\">"
        for v in sorted(cat.values(), key=lambda x: x["nombre"])
    )
    datalist = f"<datalist id=lista_pactivos>{opts}</datalist>"

    qs_extras = (f"tabla={tabla}" if tabla else "",
                 f"tipo={tipo}" if tipo else "",
                 f"metodo={metodo}" if metodo else "",
                 f"conf={conf}" if conf else "",
                 f"rango={rango}" if rango else "",
                 f"desde={desde}" if desde else "",
                 f"hasta={hasta}" if hasta else "",
                 f"estado={estado}" if estado != "pendientes" else "",
                 f"por_hoja={por_hoja}" if por_hoja != POR_HOJA_DEFAULT else "")
    qs_base = "&".join(p for p in qs_extras if p)
    qs_base = ("&" + qs_base) if qs_base else ""
    n_hojas = (total + por_hoja - 1) // por_hoja
    pag = "<div class=pag>"
    if hoja > 1:
        pag += f"<a href='/revision?hoja={hoja-1}{qs_base}'>« anterior</a>"
    pag += f" hoja {hoja} de {n_hojas} "
    if hoja < n_hojas:
        pag += f"<a href='/revision?hoja={hoja+1}{qs_base}'>siguiente »</a>"
    pag += "</div>"

    # Solo pendientes muestra form + botones de aprobar. Para revisadas/todas
    # la vista es de auditoría: lista de lectura con timestamps y veredicto.
    titulo_h1 = {
        "pendientes": f"Cola de revisión · {total} pendientes",
        "revisadas": f"Revisadas · {total} cerradas",
        "todas": f"Todas las clasificaciones · {total}",
    }[estado]
    if estado == "pendientes":
        # El nombre del logueado va al campo `nombre_clasificador` de cada fila
        # aprobada — antes era un input que el revisor escribía a mano.
        nombre_rev = (usuario or {}).get("name", "—") if usuario else "—"
        cuerpo = (
            f"<h1>{titulo_h1}</h1>{aviso}{filtros}"
            f"<form method=post action='/revisar-hoja' id='formhoja'>"
            f"<input type=hidden name=hoja value='{hoja}'>"
            f"<input type=hidden name=tabla value='{_e(tabla)}'>"
            f"<input type=hidden name=tipo value='{_e(tipo)}'>"
            f"<input type=hidden name=metodo value='{_e(metodo)}'>"
            f"<input type=hidden name=conf value='{_e(conf)}'>"
            f"<input type=hidden name=rango value='{_e(rango)}'>"
            f"<input type=hidden name=desde value='{_e(desde)}'>"
            f"<input type=hidden name=hasta value='{_e(hasta)}'>"
            f"<input type=hidden name=por_hoja value='{por_hoja}'>"
            f"<input type=hidden name=busqueda value='{_e(busqueda)}'>"
            f"<input type=hidden name=licitacion value='{_e(licitacion)}'>"
            "<div class=barra>"
            f"<label>Revisor:</label><b>{_e(nombre_rev)}</b>"
            "<button type=button class=sec onclick='marcarTodos(true)'>Tildar todas</button>"
            "<button type=button class=sec onclick='marcarTodos(false)'>Destildar todas</button>"
            "<button type=submit>Aprobar <span id=cuenta>0</span> marcadas</button>"
            "<span style='font-size:13px;color:#6b7689'>Por default todas vienen "
            "tildadas; destildá solo las que dudás (quedan pendientes para revisar luego). "
            "Editar pactivo/comp/pres marca la fila como \"Corregir\" auto.</span>"
            "</div>"
            + "".join(bloques) + pag + "</form>" + datalist + _JS
        )
    else:
        # Auditoría: sin form ni JS — solo la lista con paginación.
        cuerpo = (
            f"<h1>{titulo_h1}</h1>{aviso}{filtros}"
            + "".join(bloques) + pag
        )
    return _layout("Cola de revisión", cuerpo, usuario=usuario)


# JS: selects dependientes (pactivo→comp/pres) + auto-cambio a "Corregir" al editar.
_JS = """<script>
function fila(n){return {
  dec:document.querySelector('select.decision[data-row=\"'+n+'\"]'),
  pac:document.querySelector('input.f-pactivo[data-row=\"'+n+'\"]'),
  com:document.querySelector('select.f-comp[data-row=\"'+n+'\"]'),
  pre:document.querySelector('select.f-pres[data-row=\"'+n+'\"]')};}
function llenar(sel,opciones,actual){
  var vals=[]; actual=(actual||'').trim();
  [actual,'Sin Cla'].concat(opciones||[]).forEach(function(v){
    v=(v||'').trim(); if(v&&vals.indexOf(v)<0)vals.push(v);});
  sel.innerHTML='';
  vals.forEach(function(v){
    var o=document.createElement('option'); o.value=v; o.textContent=v;
    if(v===actual)o.selected=true; sel.appendChild(o);});
}
function marcar(n){
  var e=fila(n); if(e.dec.value==='descartar')return;  // descarte manual: respetar
  var cambio = e.pac.value.trim()!==e.pac.dataset.sug
            || e.com.value!==e.com.dataset.sug
            || e.pre.value!==e.pre.dataset.sug;
  e.dec.value = cambio ? 'corregir' : 'aprobar';
}
function alCambiarPactivo(n){
  var e=fila(n); marcar(n);
  fetch('/api/catalogo?pactivo='+encodeURIComponent(e.pac.value.trim()))
    .then(function(r){return r.json();})
    .then(function(d){ llenar(e.com,d.comp,e.com.value); llenar(e.pre,d.pres,e.pre.value); })
    .catch(function(){});
}
document.querySelectorAll('.fila[data-row]').forEach(function(f){
  var n=f.dataset.row, e=fila(n);
  if(e.pac){e.pac.addEventListener('change',function(){alCambiarPactivo(n);});}
  if(e.com){e.com.addEventListener('change',function(){marcar(n);});}
  if(e.pre){e.pre.addEventListener('change',function(){marcar(n);});}
});

// Checkbox por fila: tildada = se procesa (aprobar/corregir/descartar segun el
// select); destildada = la fila NO se incluye en el batch y queda pendiente
// para revisar luego. Por default vienen tildadas — es el patrón "aprobar
// todas menos las que destildo".
function actualizarCuenta(){
  var marcadas = document.querySelectorAll('.fila input.marcar:checked').length;
  var span = document.getElementById('cuenta');
  if(span){span.textContent = marcadas;}
}
function aplicarEstadoMarca(cb){
  var fila = cb.closest('.fila');
  if(!fila) return;
  if(cb.checked){fila.classList.remove('skip');}
  else{fila.classList.add('skip');}
}
function marcarTodos(estado){
  document.querySelectorAll('.fila input.marcar').forEach(function(cb){
    cb.checked = estado;
    aplicarEstadoMarca(cb);
  });
  actualizarCuenta();
}
document.querySelectorAll('.fila input.marcar').forEach(function(cb){
  aplicarEstadoMarca(cb);
  cb.addEventListener('change', function(){
    aplicarEstadoMarca(cb); actualizarCuenta();
  });
});
// click sobre la descripción / cuerpo del card también togglea el checkbox
// (para destildar más rápido al ojear); excluye clicks sobre inputs/labels.
document.querySelectorAll('.fila').forEach(function(f){
  f.addEventListener('click', function(ev){
    if(ev.target.closest('input, select, label, button, .linea-edicion, .nuevo-aviso')) return;
    var cb = f.querySelector('input.marcar');
    if(!cb) return;
    cb.checked = !cb.checked;
    aplicarEstadoMarca(cb); actualizarCuenta();
  });
});
// expandir la línea de edición de los descartes al hacer doble-click sobre la fila
document.querySelectorAll('.fila').forEach(function(f){
  f.addEventListener('dblclick', function(ev){
    var linea = f.querySelector('.linea-edicion');
    if(linea && linea.hidden){ linea.hidden = false; ev.preventDefault(); }
  });
});
actualizarCuenta();
</script>"""


@app.post("/revisar-hoja")
def revisar_hoja(
    request: Request,
    hoja: int = Form(1),
    tabla: str = Form(""),
    tipo: str = Form(""),
    metodo: str = Form(""),
    conf: str = Form(""),
    rango: str = Form(""),
    desde: str = Form(""),
    hasta: str = Form(""),
    por_hoja: int = Form(POR_HOJA_DEFAULT),
    busqueda: str = Form(""),
    licitacion: str = Form(""),
    log_id: list[str] = Form([]),
    decision: list[str] = Form([]),
    pactivo: list[str] = Form([]),
    composicion: list[str] = Form([]),
    presentacion: list[str] = Form([]),
    motivo: list[str] = Form([]),
    procesar: list[str] = Form([]),
):
    # El revisor sale de la sesión, no del form — así `nombre_clasificador`
    # queda firmado por el usuario logueado (no por un texto libre).
    u = usuario_actual(request)
    revisor = (u["name"] if u else "").strip()[:80] or "anónimo"
    ahora = datetime.now()
    aplicadas = 0
    sin_motivo = 0
    saltadas = 0
    # `procesar` lleva los log_id de las filas QUE el revisor dejó tildadas
    # (la mayoría — el patrón es "aprobar todas menos las que destildo"). Las
    # destildadas se saltan y quedan pendientes para revisar luego.
    set_procesar = set(procesar)
    conn = conectar()
    try:
        with conn.cursor() as cur:
            for i, lid in enumerate(log_id):
                if lid not in set_procesar:
                    saltadas += 1
                    continue
                dec = decision[i] if i < len(decision) else "aprobar"
                pact = (pactivo[i] if i < len(pactivo) else "").strip()
                comp = (composicion[i] if i < len(composicion) else "").strip()
                pres = (presentacion[i] if i < len(presentacion) else "").strip()
                mot = (motivo[i] if i < len(motivo) else "").strip()

                if dec in ("corregir", "descartar") and not mot:
                    sin_motivo += 1  # se deja pendiente: el motivo es obligatorio
                    continue

                cur.execute(
                    "SELECT tabla_origen, fila_id, interes_sugerido, pactivo_sugerido "
                    "FROM clasificador_ia_log WHERE id=%s AND revisado=0",
                    (lid,),
                )
                reg = cur.fetchone()
                if not reg:
                    continue
                tabla_o = reg["tabla_origen"]
                if tabla_o not in TABLAS_VALIDAS:
                    continue

                if dec == "descartar":
                    estado, p, c, pr, correcto = 0, None, None, None, 0
                elif dec == "corregir":
                    estado, p, c, pr, correcto = 1, pact or None, comp or None, pres or None, 0
                else:  # aprobar
                    estado = reg["interes_sugerido"]
                    if estado == 1:
                        p, c, pr = pact or None, comp or None, pres or None
                    else:
                        p, c, pr = None, None, None
                    correcto = 1

                # 1) escribir en la tabla origen (en demo sin esas tablas, no falla el resto)
                try:
                    cur.execute(
                        f"UPDATE `{tabla_o}` SET estado_gestor=%s, pactivo=%s, composicion=%s, "
                        f"presentacion=%s, nombre_clasificador=%s, fecha_clasificacion=%s "
                        f"WHERE id=%s",
                        (estado, p, c, pr, revisor, ahora, reg["fila_id"]),
                    )
                except Exception:  # noqa: BLE001
                    pass
                # 2) cerrar el registro de auditoría
                cur.execute(
                    "UPDATE clasificador_ia_log SET revisado=1, revisado_por=%s, "
                    "revisado_en=%s, feedback_correcto=%s, feedback_pactivo=%s, "
                    "feedback_notas=%s WHERE id=%s",
                    (revisor, ahora, correcto, p if dec == "corregir" else None,
                     mot or None, lid),
                )
                # 3) el "por qué" de una corrección/descarte entra como feedback al prompt
                if dec in ("corregir", "descartar") and mot:
                    cur.execute(
                        "INSERT INTO clasificador_ia_reglas "
                        "(tipo, texto, fila_ref, pactivo_malo, pactivo_bueno, "
                        " creado_por, creado_en, activa) "
                        "VALUES ('correccion',%s,%s,%s,%s,%s,%s,1)",
                        (mot, f"{tabla_o}#{reg['fila_id']}", reg["pactivo_sugerido"],
                         p if dec == "corregir" else None, revisor, ahora),
                    )
                aplicadas += 1
        conn.commit()
    finally:
        conn.close()

    msg = f"{aplicadas} fila(s) revisada(s)."
    if saltadas:
        msg += f" {saltadas} destildada(s) quedaron pendientes."
    if sin_motivo:
        msg += f" {sin_motivo} sin motivo obligatorio."
    qs = f"/revision?hoja={hoja}&msg={msg}"
    for k, v in (("tabla", tabla), ("tipo", tipo), ("metodo", metodo),
                 ("conf", conf), ("rango", rango), ("desde", desde),
                 ("hasta", hasta), ("busqueda", busqueda),
                 ("licitacion", licitacion)):
        if v:
            qs += f"&{k}={v}"
    if por_hoja != POR_HOJA_DEFAULT:
        qs += f"&por_hoja={por_hoja}"
    return RedirectResponse(qs, status_code=303)


# ----------------------------------------------------------------- Reglas ---
@app.get("/reglas", response_class=HTMLResponse)
def reglas(request: Request, msg: str = "") -> str:
    usuario = usuario_actual(request)
    try:
        items = _query(
            "SELECT tipo, texto, creado_por, creado_en, activa FROM clasificador_ia_reglas "
            "WHERE activa=1 ORDER BY tipo, creado_en DESC LIMIT 200"
        )
    except Exception as exc:  # noqa: BLE001
        return _layout("Error", f"<div class=vacio>{_e(exc)}</div>", usuario=usuario)

    reglas_ = [x for x in items if x["tipo"] == "regla"]
    corr = [x for x in items if x["tipo"] == "correccion"]

    def tabla(rows):
        if not rows:
            return "<div class=vacio>Sin registros.</div>"
        f = "".join(
            f"<tr><td>{_e(r['texto'])}</td><td>{_e(r['creado_por'])}</td>"
            f"<td>{_e(str(r['creado_en'])[:16])}</td></tr>" for r in rows
        )
        return f"<table><tr><th>Texto</th><th>Por</th><th>Fecha</th></tr>{f}</table>"

    aviso = f"<div class=aviso>{_e(msg)}</div>" if msg else ""
    # El campo `creado_por` ahora viene de la sesión, no de un input — la regla
    # queda firmada por el usuario logueado.
    cuerpo = (
        "<h1>Reglas y correcciones — feedback al prompt</h1>" + aviso
        + "<form class=alta method=post action='/reglas'>"
        "<textarea name=texto placeholder='regla de negocio para la IA' required></textarea>"
        "<button type=submit>Agregar regla</button></form>"
        f"<h2>Reglas de negocio ({len(reglas_)})</h2>" + tabla(reglas_)
        + f"<h2>Errores corregidos — máxima prioridad ({len(corr)})</h2>" + tabla(corr)
    )
    return _layout("Reglas", cuerpo, usuario=usuario)


@app.post("/reglas")
def agregar_regla(request: Request, texto: str = Form(...)):
    texto = texto.strip()
    if texto:
        u = usuario_actual(request)
        creado_por = ((u["name"] if u else "") or "").strip()[:80] or "anónimo"
        conn = conectar()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO clasificador_ia_reglas "
                    "(tipo, texto, creado_por, creado_en, activa) "
                    "VALUES ('regla',%s,%s,%s,1)",
                    (texto, creado_por, datetime.now()),
                )
            conn.commit()
        finally:
            conn.close()
    return RedirectResponse("/reglas?msg=Regla agregada.", status_code=303)


@app.get("/salud")
def salud():
    try:
        _query("SELECT 1")
        return {"estado": "ok"}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"estado": "error", "detalle": str(exc)}, status_code=500)
