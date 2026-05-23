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

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from config import config  # noqa: E402
from db import conectar  # noqa: E402
from descarte_modelo import cargar_modelo_descarte, prob_descarte  # noqa: E402
from reglas import normalizar  # noqa: E402

from api.legacy import router as legacy_router  # noqa: E402
from api.ui import layout as _layout  # noqa: E402

app = FastAPI(title="Clasificador IA — Pharmatender")
app.include_router(legacy_router)

TABLAS_VALIDAS = ("compra_agil", "Licitaciones_diarias")
POR_HOJA = 20  # filas por hoja en la cola de revisión

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
def resumen() -> str:
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
    return _layout("Resumen", cuerpo)


# ------------------------------------------------------------ Comparación ---
@app.get("/comparacion", response_class=HTMLResponse)
def comparacion() -> str:
    try:
        tot = _query("SELECT COUNT(*) n FROM clasificador_ia_backtest")[0]["n"]
        if not tot:
            return _layout(
                "Backtest",
                "<h1>Backtest · IA vs personas</h1><div class=vacio>Aún no hay filas "
                "comparadas. Corre el worker en modo test.</div>",
            )
        r = _query(
            "SELECT COUNT(*) n, SUM(coincide_interes) ci, "
            "SUM(coincide_pactivo) cp, COUNT(coincide_pactivo) ncp, "
            "SUM(coincide_composicion) cc, SUM(coincide_presentacion) cpr, "
            "IFNULL(SUM(costo_usd),0) costo, IFNULL(AVG(costo_usd),0) prom "
            "FROM clasificador_ia_backtest"
        )[0]
        # Gasto acumulado de la cuenta — del libro de costos, que NO se trunca
        # (a diferencia de clasificador_ia_backtest). Es el mismo número que en /.
        acum = _query(
            "SELECT IFNULL(SUM(costo_usd),0) c FROM clasificador_ia_costos"
        )[0]["c"]
        errores = _query(
            "SELECT tabla_origen, fila_id, descripcion, humano_estado_gestor, humano_pactivo, "
            "ia_interes, ia_pactivo FROM clasificador_ia_backtest "
            "WHERE coincide_interes=0 OR coincide_pactivo=0 ORDER BY creado_en DESC LIMIT 40"
        )
    except Exception as exc:  # noqa: BLE001
        return _layout("Error", f"<div class=vacio>{_e(exc)}</div>")

    def pct(ok, t):
        return f"{(ok or 0) / t * 100:.1f}%" if t else "—"

    proy = float(r["prom"] or 0) * config.filas_mes_estimado
    cards = (
        "<h2>Backtest actual — el contenido vigente de la tabla de comparación</h2>"
        "<div class=cards>"
        f"<div class=card><div class=n>{r['n']}</div><div class=l>Filas en el backtest actual</div></div>"
        f"<div class=card><div class=n>{pct(r['ci'], r['n'])}</div><div class=l>Acierto interés</div></div>"
        f"<div class=card><div class=n>{pct(r['cp'], r['ncp'])}</div><div class=l>Acierto pactivo</div></div>"
        f"<div class=card><div class=n>{pct(r['cc'], r['ncp'])}</div><div class=l>Acierto composición</div></div>"
        f"<div class=card><div class=n>{pct(r['cpr'], r['ncp'])}</div><div class=l>Acierto presentación</div></div>"
        "</div>"
        "<h2>Costo</h2><div class=cards>"
        f"<div class=card><div class=n>${float(r['costo']):.2f}</div>"
        f"<div class=l>Gasto de este backtest</div></div>"
        f"<div class=card><div class=n>${float(r['prom'] or 0)*1000:.2f}</div>"
        f"<div class=l>Costo / 1.000 filas</div></div>"
        f"<div class=card><div class=n>${proy:,.0f}</div><div class=l>Proyección mensual</div></div>"
        f"<div class=card><div class=n>${float(acum):.2f}</div>"
        f"<div class=l>Gasto total acumulado (de ${config.budget_usd:.0f})</div></div>"
        "</div>"
    )
    filas = "".join(
        f"<tr><td>{_e(e['tabla_origen'])} #{e['fila_id']}</td>"
        f"<td>{_e((e.get('descripcion') or '')[:80])}</td>"
        f"<td>estado={_e(e['humano_estado_gestor'])}<br>{_e(e.get('humano_pactivo'))}</td>"
        f"<td>interes={_e(e['ia_interes'])}<br>{_e(e.get('ia_pactivo'))}</td></tr>"
        for e in errores
    )
    cuerpo = (
        "<h1>Backtest · IA vs personas</h1>" + cards
        + "<h2>Discrepancias recientes</h2><table>"
        "<tr><th>Fila</th><th>Descripción</th><th>Persona</th><th>IA</th></tr>"
        + (filas or "<tr><td colspan=4>Sin discrepancias</td></tr>") + "</table>"
    )
    return _layout("Backtest", cuerpo)


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


@app.get("/revision", response_class=HTMLResponse)
def revision(hoja: int = 1, msg: str = "", tabla: str = "", tipo: str = "",
             metodo: str = "") -> str:
    hoja = max(1, hoja)
    cond = ["revisado=0"]
    args: list = []
    if tabla in TABLAS_VALIDAS:
        cond.append("tabla_origen=%s")
        args.append(tabla)
    if tipo in _TIPOS:
        cond.append(_TIPOS[tipo])
    if metodo in _METODOS:
        cond.append("metodo=%s")
        args.append(metodo)
    where = " AND ".join(cond)
    try:
        total = _query(
            f"SELECT COUNT(*) n FROM clasificador_ia_log WHERE {where}", tuple(args)
        )[0]["n"]
        filas = _query(
            "SELECT id, tabla_origen, fila_id, descripcion, interes_sugerido, "
            "pactivo_sugerido, composicion_sugerida, presentacion_sugerida, "
            f"confianza, razon, pactivo_nuevo, metodo FROM clasificador_ia_log WHERE {where} "
            "ORDER BY confianza ASC, creado_en DESC LIMIT %s OFFSET %s",
            tuple(args) + (POR_HOJA, (hoja - 1) * POR_HOJA),
        )
    except Exception as exc:  # noqa: BLE001
        return _layout("Error", f"<div class=vacio>{_e(exc)}</div>")

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

    # barra de filtros (mantiene el estado de los otros filtros en cada enlace)
    def filtro_link(clave: str, valor: str, etiqueta: str, activo: bool) -> str:
        estado = {"tabla": tabla, "tipo": tipo, "metodo": metodo}
        estado[clave] = valor
        qs = "&".join(f"{k}={v}" for k, v in estado.items() if v)
        cls = " class=on" if activo else ""
        return f"<a href='/revision?{qs}'{cls}>{etiqueta}</a>"

    filtros = (
        "<div class=filtros><b>Tabla:</b>"
        + filtro_link("tabla", "", "todas", not tabla)
        + filtro_link("tabla", "compra_agil", "compra ágil", tabla == "compra_agil")
        + filtro_link("tabla", "Licitaciones_diarias", "licitaciones",
                      tabla == "Licitaciones_diarias")
        + " &nbsp; <b>Tipo:</b>"
        + filtro_link("tipo", "", "todos", not tipo)
        + filtro_link("tipo", "interes", "interés", tipo == "interes")
        + filtro_link("tipo", "descarte", "descarte", tipo == "descarte")
        + filtro_link("tipo", "nuevo", "pactivo nuevo", tipo == "nuevo")
        + " &nbsp; <b>Vía:</b>"
        + filtro_link("metodo", "", "todas", not metodo)
        + "".join(filtro_link("metodo", k, v, metodo == k) for k, v in _METODOS.items())
        + "</div>"
    )

    if not total:
        return _layout(
            "Cola de revisión",
            "<h1>Cola de revisión</h1>" + filtros
            + "<div class=vacio>No hay clasificaciones pendientes con ese filtro. 🎉</div>",
        )

    cat = _catalogo()
    aviso = f"<div class=aviso>{_e(msg)}</div>" if msg else ""
    bloques = []
    for n, f in enumerate(filas):
        conf = float(f.get("confianza") or 0)
        interes = f.get("interes_sugerido")
        es_nuevo = bool((f.get("pactivo_nuevo") or "").strip())
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

        bloques.append(
            f"<div class='fila {tipo_cls}' data-row='{n}'>"
            f"<div class=meta>{badge} &nbsp; "
            f"<b>Licitación {_e(num_lic.get((f['tabla_origen'], f['fila_id'])) or '—')}</b>"
            f" · {_e(f['tabla_origen'])} #{f['fila_id']} · "
            f"<span class='badge {'b-baja' if conf < 0.7 else 'b-alta'}'>confianza {conf:.2f}</span>"
            f"{via}{ent}</div>"
            f"<div class=desc>{_e((f.get('descripcion') or '')[:300])}</div>"
            f"<div class=razon>Claude: {_e(f.get('razon'))}</div>"
            + aviso_nuevo
            + f"<input type=hidden name=log_id value='{f['id']}'>"
            "<div class=linea>"
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

    qs_base = "&".join(p for p in (f"tabla={tabla}" if tabla else "",
                                   f"tipo={tipo}" if tipo else "",
                                   f"metodo={metodo}" if metodo else "") if p)
    qs_base = ("&" + qs_base) if qs_base else ""
    n_hojas = (total + POR_HOJA - 1) // POR_HOJA
    pag = "<div class=pag>"
    if hoja > 1:
        pag += f"<a href='/revision?hoja={hoja-1}{qs_base}'>« anterior</a>"
    pag += f" hoja {hoja} de {n_hojas} "
    if hoja < n_hojas:
        pag += f"<a href='/revision?hoja={hoja+1}{qs_base}'>siguiente »</a>"
    pag += "</div>"

    cuerpo = (
        f"<h1>Cola de revisión · {total} pendientes</h1>{aviso}{filtros}"
        f"<form method=post action='/revisar-hoja'>"
        f"<input type=hidden name=hoja value='{hoja}'>"
        f"<input type=hidden name=tabla value='{_e(tabla)}'>"
        f"<input type=hidden name=tipo value='{_e(tipo)}'>"
        f"<input type=hidden name=metodo value='{_e(metodo)}'>"
        "<div class=barra><label>Revisor:</label>"
        "<input name=revisor placeholder='tu nombre' required>"
        "<button type=submit>Guardar esta hoja</button>"
        "<span style='font-size:13px;color:#6b7689'>Se guardan solo las "
        f"{POR_HOJA} filas de esta hoja. Editar pactivo/comp/pres marca la fila "
        "como \"Corregir\" automáticamente.</span></div>"
        + "".join(bloques) + pag + "</form>" + datalist + _JS
    )
    return _layout("Cola de revisión", cuerpo)


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
  e.pac.addEventListener('change',function(){alCambiarPactivo(n);});
  e.com.addEventListener('change',function(){marcar(n);});
  e.pre.addEventListener('change',function(){marcar(n);});
});
</script>"""


@app.post("/revisar-hoja")
def revisar_hoja(
    revisor: str = Form(...),
    hoja: int = Form(1),
    tabla: str = Form(""),
    tipo: str = Form(""),
    metodo: str = Form(""),
    log_id: list[str] = Form([]),
    decision: list[str] = Form([]),
    pactivo: list[str] = Form([]),
    composicion: list[str] = Form([]),
    presentacion: list[str] = Form([]),
    motivo: list[str] = Form([]),
):
    revisor = revisor.strip()[:80] or "anónimo"
    ahora = datetime.now()
    aplicadas = 0
    sin_motivo = 0
    conn = conectar()
    try:
        with conn.cursor() as cur:
            for i, lid in enumerate(log_id):
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
    if sin_motivo:
        msg += f" {sin_motivo} quedaron pendientes: falta el motivo obligatorio."
    qs = f"/revision?hoja={hoja}&msg={msg}"
    if tabla:
        qs += f"&tabla={tabla}"
    if tipo:
        qs += f"&tipo={tipo}"
    if metodo:
        qs += f"&metodo={metodo}"
    return RedirectResponse(qs, status_code=303)


# ----------------------------------------------------------------- Reglas ---
@app.get("/reglas", response_class=HTMLResponse)
def reglas(msg: str = "") -> str:
    try:
        items = _query(
            "SELECT tipo, texto, creado_por, creado_en, activa FROM clasificador_ia_reglas "
            "WHERE activa=1 ORDER BY tipo, creado_en DESC LIMIT 200"
        )
    except Exception as exc:  # noqa: BLE001
        return _layout("Error", f"<div class=vacio>{_e(exc)}</div>")

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
    cuerpo = (
        "<h1>Reglas y correcciones — feedback al prompt</h1>" + aviso
        + "<form class=alta method=post action='/reglas'>"
        "<input name=creado_por placeholder='tu nombre' required>"
        "<textarea name=texto placeholder='regla de negocio para la IA' required></textarea>"
        "<button type=submit>Agregar regla</button></form>"
        f"<h2>Reglas de negocio ({len(reglas_)})</h2>" + tabla(reglas_)
        + f"<h2>Errores corregidos — máxima prioridad ({len(corr)})</h2>" + tabla(corr)
    )
    return _layout("Reglas", cuerpo)


@app.post("/reglas")
def agregar_regla(creado_por: str = Form(...), texto: str = Form(...)):
    texto = texto.strip()
    if texto:
        conn = conectar()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO clasificador_ia_reglas "
                    "(tipo, texto, creado_por, creado_en, activa) "
                    "VALUES ('regla',%s,%s,%s,1)",
                    (texto, creado_por.strip()[:80] or "anónimo", datetime.now()),
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
