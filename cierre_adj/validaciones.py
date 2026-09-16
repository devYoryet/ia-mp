"""Validaciones del cierre (SOLO LECTURA). Cada una devuelve un Resultado.

    V1 días del mes con listado diario descargado (monitoreo_descarga)
    V2 listado completo: lo que la API informa como adjudicado está en listado_api
    V3 actas: cada licitación del listado tiene filas en Licitaciones o una
       razón (desierta / sin ítems); pendientes estado=0
    V4 actas completas: ítems adjudicados según la API == ítems 'Adjudicada'
       descargados (licitaciones que entran a consulta5 + las sin filas)
    V5 Licitaciones del mes: clásico == prime (filas por licitación)
    V6 resumen: 6 tablas, fórmula == clásico == prime
    V7 OC: consulta3 y consulta5 == regla calculada desde el clásico
    V8 prime: consulta5 == regla (incluye pactivo al día)
    V9 prime: fila del mes en test_matias.Fecha

Estados: ok · falla (bloquea el sello) · aviso (informa) · omitida.
"""

from __future__ import annotations

import calendar
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from cierre_adj import bd, mercadopublico as mp, reglas


@dataclass
class Resultado:
    id: str
    titulo: str
    estado: str = "omitida"
    resumen: str = ""
    detalle: dict = field(default_factory=dict)

    def dict(self) -> dict:
        return asdict(self)


def _dias(mes: str, hasta: date | None = None) -> list[str]:
    anio, m = (int(x) for x in mes.split("-"))
    out = []
    for d in range(1, calendar.monthrange(anio, m)[1] + 1):
        dia = date(anio, m, d)
        if hasta and dia > hasta:
            break
        out.append(dia.isoformat())
    return out


# ------------------------------------------------------------------ V1 ---

def v1_dias_descargados(cn_c, mes: str) -> Resultado:
    r = Resultado("V1", "Listado diario descargado todos los días")
    ini, fin = bd.rango_mes(mes)
    hechos = {f.isoformat() for (f,) in bd.todos(
        cn_c, f"SELECT DISTINCT fecha_descarga FROM {bd.DB_ADJ}.monitoreo_descarga WHERE fecha_descarga BETWEEN %s AND %s",
        (ini, fin))}
    faltan = [d for d in _dias(mes, date.today()) if d not in hechos]
    r.estado = "ok" if not faltan else "falla"
    r.resumen = f"{len(hechos)} días con descarga" + (f"; faltan {len(faltan)}: {', '.join(faltan[:10])}" if faltan else "")
    r.detalle = {"dias_faltantes": faltan}
    return r


# ------------------------------------------------------------------ V2 ---

def api_listado_mes(mes: str, cache: Path | None = None, log=print) -> dict:
    """{dia: [códigos]} desde la API. Cachea en disco: 31 llamadas espaciadas
    tardan ~4 min y el panel no debe repetirlas en cada validación."""
    datos = {}
    if cache and cache.exists():
        try:
            datos = json.loads(cache.read_text())
        except (OSError, ValueError):
            datos = {}
    hoy = date.today()
    for dia in _dias(mes, hoy):
        # Días de hace menos de 7 días se vuelven a pedir: la API aún puede sumar.
        reciente = (hoy - date.fromisoformat(dia)).days < 7
        if dia in datos and not reciente:
            continue
        datos[dia] = sorted(mp.listado_adjudicadas(dia))
        log(f"   API {dia}: {len(datos[dia])} adjudicadas")
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(datos))
    return datos


def v2_listado_vs_api(cn_c, mes: str, api_mes: dict | None) -> Resultado:
    r = Resultado("V2", "Listado completo contra la API de Mercado Público")
    if api_mes is None:
        r.resumen = "sin consultar la API (usar 'validar con API')"
        return r
    codigos = {c for cods in api_mes.values() for c in cods}
    en_listado = set()
    for trozo in bd.trozos(sorted(codigos), 1000):
        marcas, vals = bd.en_lista(trozo)
        en_listado |= {x for (x,) in bd.todos(
            cn_c, f"SELECT licitacion FROM {bd.DB_ADJ}.listado_api WHERE licitacion IN ({marcas})", vals)}
    faltan = sorted(codigos - en_listado)
    r.estado = "ok" if not faltan else "falla"
    r.resumen = f"API {len(codigos)} licitaciones; faltan en listado_api: {len(faltan)}"
    r.detalle = {"faltan": faltan}
    return r


# ------------------------------------------------------------------ V3 ---

def estado_actas(cn_c, mes: str) -> dict:
    ini, fin = bd.rango_mes(mes)
    filas = bd.todos(
        cn_c,
        f"""SELECT l.licitacion, l.estado, COALESCE(l.intentos, 0),
                   EXISTS(SELECT 1 FROM {bd.DB_ADJ}.Licitaciones x WHERE x.ADQUISICION = l.licitacion)
            FROM {bd.DB_ADJ}.listado_api l WHERE l.fecha_descarga BETWEEN %s AND %s""",
        (ini, fin))
    pendientes = [f[0] for f in filas if f[1] == 0 and not f[3]]
    # Último error solo de las pendientes, en una consulta aparte: como subconsulta
    # por fila (ORDER BY id DESC LIMIT 1) no usaba idx_licitacion y con 50.075
    # errores tardaba minutos (medido 2026-09-16).
    ultimo_error: dict = {}
    for trozo in bd.trozos(pendientes, 500):
        marcas, vals = bd.en_lista(trozo)
        for _id, lic, msj in bd.todos(
            cn_c,
            f"""SELECT id, licitacion, mensaje_error FROM {bd.DB_ADJ}.errores_procesamiento
                WHERE licitacion IN ({marcas}) ORDER BY id""", vals):
            ultimo_error[lic] = msj or ""
    sin_acta = [x for x in pendientes if "Sin acta" in ultimo_error.get(x, "")]
    return {
        "total": len(filas),
        "con_filas": [f[0] for f in filas if f[3]],
        # Mercado Público aún no publica el acta: no es un error nuestro, se reintenta.
        "sin_acta_en_mp": sin_acta,
        "pendientes": [x for x in pendientes if x not in sin_acta],
        "marcadas_sin_filas": [f[0] for f in filas if f[1] != 0 and not f[3]],
        "pendientes_con_filas": [f[0] for f in filas if f[1] == 0 and f[3]],
    }


def v3_actas(cn_c, mes: str) -> Resultado:
    r = Resultado("V3", "Acta descargada para cada licitación del listado")
    e = estado_actas(cn_c, mes)
    r.detalle = {k: v for k, v in e.items() if k != "con_filas"}
    r.detalle["con_filas"] = len(e["con_filas"])
    avisos = e["sin_acta_en_mp"] or e["marcadas_sin_filas"]
    r.estado = "falla" if e["pendientes"] else ("aviso" if avisos else "ok")
    r.resumen = (f"{e['total']} en listado; con acta {len(e['con_filas'])}; pendientes por error {len(e['pendientes'])}; "
                 f"sin acta publicada en MP {len(e['sin_acta_en_mp'])}; "
                 f"marcadas sin filas (desierta/sin ítems, se verifican en V4) {len(e['marcadas_sin_filas'])}")
    return r


# ------------------------------------------------------------------ V4 ---

def items_adjudicados_bd(cn_c, codigos) -> dict:
    out = {c: 0 for c in codigos}
    for trozo in bd.trozos(sorted(codigos), 500):
        marcas, vals = bd.en_lista(trozo)
        for adq, n in bd.todos(
            cn_c,
            f"""SELECT ADQUISICION, COUNT(DISTINCT CASE WHEN ESTADO = 'Adjudicada' THEN NUMPROD END)
                FROM {bd.DB_ADJ}.Licitaciones WHERE ADQUISICION IN ({marcas}) GROUP BY ADQUISICION""",
            vals):
            out[adq] = int(n)
    return out


def v4_actas_vs_api(cn_c, codigos: list[str], cache: Path | None = None, log=print, con_api: bool = True) -> Resultado:
    """Compara ítems adjudicados API vs BD. Las coincidencias se cachean (un acta
    completa no cambia) para no repetir ~900 llamadas en cada validación."""
    r = Resultado("V4", "Actas completas (ítems adjudicados API == BD)")
    if not con_api:
        r.resumen = "sin consultar la API (usar 'validar con API')"
        return r
    ok_previos: dict = {}
    if cache and cache.exists():
        try:
            ok_previos = json.loads(cache.read_text())
        except (OSError, ValueError):
            ok_previos = {}
    bd_items = items_adjudicados_bd(cn_c, codigos)
    incompletas, no_api, verificadas = [], [], 0
    pendientes = [c for c in codigos if ok_previos.get(c) != bd_items.get(c)]
    log(f"   V4: {len(codigos)} licitaciones; {len(codigos) - len(pendientes)} ya verificadas antes; consulto {len(pendientes)} en la API")
    for i, cod in enumerate(pendientes, 1):
        try:
            det = mp.detalle(cod)
        except mp.ErrorAPI as exc:
            no_api.append({"codigo": cod, "error": str(exc)})
            continue
        if det is None:
            no_api.append({"codigo": cod, "error": "la API no la conoce"})
            continue
        if det["items_adjudicados"] == bd_items.get(cod, 0):
            ok_previos[cod] = bd_items.get(cod, 0)
            verificadas += 1
        else:
            incompletas.append({"codigo": cod, "api": det["items_adjudicados"], "bd": bd_items.get(cod, 0),
                                "estado_api": det["estado"], "fecha_adjudicacion": det["fecha_adjudicacion"]})
        if cache and i % 25 == 0:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(ok_previos))
        if i % 100 == 0:
            log(f"   V4: {i}/{len(pendientes)} consultadas, {len(incompletas)} con diferencias")
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(ok_previos))
    r.estado = "falla" if incompletas or no_api else "ok"
    r.resumen = (f"{len(codigos)} verificadas contra la API; con diferencias {len(incompletas)}; "
                 f"sin respuesta de la API {len(no_api)}")
    r.detalle = {"incompletas": incompletas, "sin_api": no_api}
    return r


# ------------------------------------------------------------------ V5 ---

def conteo_por_licitacion(cn, mes: str) -> dict:
    ini, fin = bd.rango_mes(mes)
    return dict(bd.todos(cn, f"""SELECT ADQUISICION, COUNT(*) FROM {bd.DB_ADJ}.Licitaciones
                                 WHERE FECHASQL BETWEEN %s AND %s GROUP BY ADQUISICION""", (ini, fin)))


def v5_prime_licitaciones(cn_c, cn_p, mes: str) -> Resultado:
    r = Resultado("V5", "Licitaciones del mes: clásico == prime")
    c, p = conteo_por_licitacion(cn_c, mes), conteo_por_licitacion(cn_p, mes)
    faltan = {k: (v, p.get(k, 0)) for k, v in c.items() if p.get(k, 0) < v}
    sobran = {k: (c.get(k, 0), v) for k, v in p.items() if v > c.get(k, 0)}
    r.estado = "falla" if faltan else ("aviso" if sobran else "ok")
    r.resumen = (f"clásico {sum(c.values())} filas / {len(c)} licitaciones; prime {sum(p.values())} / {len(p)}; "
                 f"licitaciones con filas de menos en prime {len(faltan)}; con filas de más {len(sobran)}")
    r.detalle = {"faltan_en_prime": faltan, "sobran_en_prime": dict(list(sobran.items())[:50])}
    return r


# ------------------------------------------------------------------ V6 ---

def v6_resumen(cn_c, cn_p, mes: str, mes_cerrado: bool) -> Resultado:
    """mes_cerrado=True si `mes` es el mes que se está cerrando: ahí publicados y
    cerrados también deben calzar. En meses anteriores son foto y solo avisan."""
    r = Resultado("V6", "Resumen (6 tablas): fórmula == clásico == prime")
    fallas, avisos, tablas = [], [], {}
    for tabla, _col, _sin, foto in reglas.TABLAS_RESUMEN:
        esp = reglas.firma(reglas.resumen_esperado(cn_c, mes, tabla))
        cla = reglas.firma(reglas.resumen_actual(cn_c, mes, tabla))
        pri = reglas.firma(reglas.resumen_actual(cn_p, mes, tabla))
        t = {"clasico": reglas.comparar(esp, cla), "prime": reglas.comparar(cla, pri)}
        tablas[tabla] = t
        if not t["prime"]["iguales"]:
            fallas.append(f"{tabla}: prime distinto del clásico")
        if not t["clasico"]["iguales"]:
            (avisos if foto and not mes_cerrado else fallas).append(
                f"{tabla}: clásico ({t['clasico']['reales']}) distinto de la fórmula ({t['clasico']['esperadas']})")
    r.estado = "falla" if fallas else ("aviso" if avisos else "ok")
    r.resumen = "; ".join(fallas + avisos) or "las 6 tablas calzan"
    r.detalle = {"tablas": tablas, "avisos_foto": avisos}
    return r


# ------------------------------------------------------------ V7 / V8 ---

def esperado_consultas(cn_c, mes: str) -> dict:
    c3 = reglas.consulta3_esperado(cn_c, mes)
    c5 = reglas.consulta5_esperado(cn_c, c3)
    return {"c3": c3, "c5": c5, "firma_c3": reglas.firma_c3(c3), "firma_c5": reglas.firma_c5(c5)}


def v7_oc(cn_o, mes: str, esp: dict) -> Resultado:
    r = Resultado("V7", "OC: consulta3 y consulta5 == regla")
    c3 = reglas.comparar(esp["firma_c3"], reglas.firma_c3(reglas.consulta_actual(cn_o, "consulta3", mes)))
    c5 = reglas.comparar(esp["firma_c5"], reglas.firma_c5(reglas.consulta_actual(cn_o, "consulta5", mes)))
    r.estado = "ok" if c3["iguales"] and c5["iguales"] else "falla"
    r.resumen = f"consulta3 {c3['reales']}/{c3['esperadas']} (faltan {c3['faltan']}, sobran {c3['sobran']}); " \
                f"consulta5 {c5['reales']}/{c5['esperadas']} (faltan {c5['faltan']}, sobran {c5['sobran']})"
    r.detalle = {"consulta3": c3, "consulta5": c5}
    return r


def v8_prime_consulta5(cn_p, mes: str, esp: dict) -> Resultado:
    r = Resultado("V8", "Prime: consulta5 == regla (con pactivo al día)")
    c5 = reglas.comparar(esp["firma_c5"], reglas.firma_c5(reglas.consulta_actual(cn_p, "consulta5", mes)))
    r.estado = "ok" if c5["iguales"] else "falla"
    r.resumen = f"{c5['reales']} filas, esperadas {c5['esperadas']} (faltan {c5['faltan']}, sobran {c5['sobran']})"
    r.detalle = {"consulta5": c5}
    return r


# ------------------------------------------------------------------ V9 ---

def v9_fecha(cn_p, mes: str) -> Resultado:
    r = Resultado("V9", "Prime: mes en test_matias.Fecha")
    filas = bd.todos(cn_p, f"SELECT strNombreFecha FROM {bd.DB_TM}.Fecha WHERE datFecha = %s", (mes,))
    esperado = reglas.nombre_mes(mes)
    if not filas:
        r.estado, r.resumen = "falla", f"falta la fila '{esperado}'"
    elif len(filas) > 1:
        r.estado, r.resumen = "aviso", f"{len(filas)} filas para {mes}"
    elif filas[0][0] != esperado:
        r.estado, r.resumen = "aviso", f"nombre '{filas[0][0]}' (se esperaba '{esperado}')"
    else:
        r.estado, r.resumen = "ok", esperado
    return r


def resumen_estados(resultados: list[Resultado]) -> str:
    estados = {r.estado for r in resultados}
    return "falla" if "falla" in estados else ("aviso" if "aviso" in estados else "ok")


def ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")
