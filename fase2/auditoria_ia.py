"""Auditoría con IA de lo que la Fase 2 NO rescató — ¿se quedó fuera algo que debía entrar?

La corre una PERSONA, a mano y con tope de gasto (nunca el servicio): gasta API.

1. Selección ($0). Recorre los descartes de los últimos `--dias` que el motor
   deja fuera y los separa en estratos:
     - cercano:   casi calzan (`motor.diagnosticar`): palabra excluida, palabra
                  sin contexto, palabra sólo en el título, código ONU sin palabra,
                  raíz de una palabra del cliente sin calce. Un estrato por motivo.
     - onu_salud: código ONU de salud (segmento 42, laboratorio 4110-4112,
                  servicios de salud 85, limpieza 47131) sin ningún parecido.
     - resto:     todo lo demás.
   De cada estrato toma una muestra al azar (reproducible, semilla fija).
2. Revisión: la muestra va a Claude por la Message Batches API (50 % del precio),
   40 líneas por pedido, salida JSON estructurada: categoría o NINGUNA, confianza
   y motivo. Antes de enviar estima el costo y aborta si pasa `--tope-usd`.
3. Resultado: tabla clasificador_f2_auditoria (no cambia ninguna clasificación),
   el gasto en clasificador_ia_costos (contexto 'ajuste', como manda el control de
   presupuesto) y un Excel: resumen por estrato con el ESTIMADO de faltantes
   (población × tasa de la muestra) y la lista de posibles faltantes.

    python auditoria_ia.py --dias 30 --tope-usd 10 --excel auditoria.xlsx
    python auditoria_ia.py --reanudar estado.json --excel auditoria.xlsx   # si se cortó la espera

Requiere ANTHROPIC_API_KEY, F2_MYSQL_* (lectura) y F2_ADMIN_MYSQL_* (escritura de la
tabla de auditoría y del libro de costos).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import barrido
import motor
from bd import BD, parametros
from reconciliar import estado_efectivo

log = logging.getLogger("fase2.auditoria")

MODELO = os.getenv("F2_AUDITORIA_MODELO", "claude-opus-5-5")
# USD por millón de tokens EN BATCH (50 % del precio estándar): entrada, salida, lectura y escritura de caché.
PRECIO_BATCH = {
    "claude-opus-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-haiku-5-5": (0.05, 0.25, 0.005, 0.0625),
}
POR_PEDIDO = 40
SALIDA_ESTIMADA_POR_FILA = 45      # tokens de JSON por línea
SALIDA_ESTIMADA_POR_PEDIDO = 2000  # razonamiento (esfuerzo bajo) + margen
ONU_SALUD = ("42", "4110", "4111", "4112", "85", "47131")

DESCRIPCIONES = {
    "SRV-MAN": "mantención preventiva o correctiva, reparación, calibración o servicio técnico de equipos médicos, "
               "clínicos, dentales, de laboratorio, de esterilización u oftalmológicos (autoclaves, rayos X, "
               "monitores, ecógrafos, microscopios, sillones dentales, centrífugas...). NO: vehículos, edificios, "
               "climatización, ascensores, extintores, generadores, informática.",
    "DEV-OFT": "equipos, instrumental, insumos o implantes de oftalmología: lentes intraoculares, campímetros, "
               "OCT (tomografía de coherencia óptica), instrumental oftalmológico, insumos de cirugía de cataratas, "
               "retina o glaucoma, equipos de diagnóstico ocular. NO: colirios ni otros medicamentos, ni lentes "
               "ópticos o anteojos.",
    "DEV-MIC": "microscopios (de laboratorio, quirúrgicos, oftalmológicos, digitales) y sus partes principales "
               "(objetivos, cabezales). NO: portaobjetos, cubreobjetos, colorantes ni insumos menores de laboratorio.",
    "DEV-GUA": "guantes de uso médico o clínico: de examen, procedimiento o quirúrgicos (nitrilo, látex, vinilo, "
               "poliisopreno). NO: guantes de trabajo, de aseo doméstico, de cocina, de jardinería ni deportivos.",
    "DEV-DET": "detergentes clínicos: enzimáticos o para lavado de instrumental quirúrgico, endoscopios o "
               "material de laboratorio. NO: detergentes de ropa, de loza o de pisos.",
    "DEV-APO": "apósitos para curación de heridas (transparentes, hidrocoloides, de espuma, alginato, plata, "
               "gasa...).",
}


# --------------------------------------------------------------------------
# 1. Selección de la muestra ($0)
# --------------------------------------------------------------------------
def _estrato(cats, f: dict, glosa: str) -> tuple[str, str]:
    motivos = motor.diagnosticar(cats, glosa, f["titulo"], f["rub"])
    if motivos:
        return "cercano · " + motivos[0], "; ".join(motivos)[:160]
    cod = (f["rub"] or "").strip()
    if cod.startswith(ONU_SALUD):
        return "onu_salud", "código ONU de salud sin parecido con las categorías"
    return "resto", "sin parecido con las categorías"


def seleccionar(bd: BD, dias: int, por_estrato: int, n_onu: int, n_resto: int, semilla: int = 7) -> dict:
    cats, _ = motor.compilar(barrido.cargar_config(bd))
    onu = barrido.cargar_onu(bd)
    rnd = random.Random(semilla)
    poblacion: Counter = Counter()
    muestra: dict[str, list] = defaultdict(list)
    tope = lambda e: n_onu if e == "onu_salud" else (n_resto if e == "resto" else por_estrato)
    capturadas = 0
    for tabla, rub in barrido.FUENTES.items():
        r = barrido.rango(bd, tabla, dias)
        if not r:
            continue
        lo, hi = r
        sql = barrido.SQL_FILAS.format(tabla=tabla, rub=rub)
        while lo <= hi:
            filas = bd.todos(sql, (lo, hi, barrido.LOTE))
            if not filas:
                break
            a, b = filas[0]["id"], filas[-1]["id"]
            logs = {x["fila_id"]: x for x in bd.todos(barrido.SQL_LOG, (tabla, a, b))}
            for f in filas:
                lg = logs.get(f["id"])
                if estado_efectivo(f["estado_gestor"], lg["interes_sugerido"] if lg else None) != 0:
                    continue  # sólo lo descartado
                cod = (f["rub"] or "").strip()
                glosa = f["descripcion"]
                if tabla in barrido.CON_SUFIJO_ONU:
                    glosa = barrido.glosa_sin_sufijo_onu(glosa, onu.get(cod, (None, 0))[0])
                if motor.evaluar(cats, glosa, f["titulo"], f["rub"]):
                    capturadas += 1
                    continue  # ya la rescató la fase 2
                estrato, motivo = _estrato(cats, f, glosa)
                poblacion[estrato] += 1
                fila = {"tabla_origen": tabla, "fila_id": f["id"], "licitacion": f["licitacion"],
                        "fecha_publicacion": str(f["fecha_publicacion"] or ""), "descripcion": (f["descripcion"] or "")[:1000],
                        "titulo": (f["titulo"] or "")[:300], "codigo_onu": cod or None,
                        "nombre_onu": onu.get(cod, (None, 0))[0], "ia_metodo": (lg or {}).get("metodo"),
                        "clasificador_f1": f["nombre_clasificador"], "estrato": estrato, "motivo_grupo": motivo}
                # muestreo de reservorio: muestra uniforme sin guardar toda la población
                m, k, n = muestra[estrato], tope(estrato), poblacion[estrato]
                if len(m) < k:
                    m.append(fila)
                else:
                    j = rnd.randrange(n)
                    if j < k:
                        m[j] = fila
            lo = b + 1
        log.info("%s listo", tabla)
    return {"dias": dias, "creado": datetime.now().isoformat(timespec="seconds"), "modelo": MODELO,
            "capturadas": capturadas, "poblacion": dict(poblacion),
            "muestra": [x for v in muestra.values() for x in v]}


# --------------------------------------------------------------------------
# 2. Revisión con Claude (Message Batches API)
# --------------------------------------------------------------------------
def _sistema(config: list[dict]) -> str:
    lineas = [f"- {c['codigo']} · {c['linea']} · {c['nombre']}: {DESCRIPCIONES.get(c['codigo'], c['nombre'])}"
              for c in sorted(config, key=lambda c: c["prioridad"])]
    return (
        "Auditas la clasificación de compras públicas de Chile (mercadopublico.cl) para Pharmatender, una empresa "
        "que vende esa información categorizada a sus clientes. El cliente Megalabs quiere recibir las líneas de "
        "compra de estas categorías:\n" + "\n".join(lineas) + "\n\n"
        "Cada línea que recibes fue DESCARTADA por el sistema. Para cada una decide si en realidad corresponde a "
        "una de las categorías (la más específica) o a NINGUNA. Sé estricto: marca una categoría sólo si la glosa, "
        "el título o el código ONU muestran claramente ese producto o servicio. Si la glosa no dice qué es (por "
        "ejemplo, 'según adjunto') y sólo el título o el código lo sugieren, usa confianza baja. El título es el "
        "nombre de toda la compra y puede abarcar productos distintos al de la línea. Responde en español; el "
        "motivo, en 15 palabras como máximo."
    )


def _esquema(config: list[dict]) -> dict:
    codigos = [c["codigo"] for c in config] + ["NINGUNA"]
    return {
        "type": "object",
        "properties": {"filas": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "i": {"type": "integer"},
                "categoria": {"type": "string", "enum": codigos},
                "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
                "motivo": {"type": "string"},
            },
            "required": ["i", "categoria", "confianza", "motivo"],
            "additionalProperties": False,
        }}},
        "required": ["filas"],
        "additionalProperties": False,
    }


def _texto_filas(filas: list[dict]) -> str:
    out = []
    for i, f in enumerate(filas):
        out.append(f"[{i}] Glosa: {f['descripcion'][:300]} | Título: {f['titulo'][:150]} | "
                   f"Código ONU: {f['codigo_onu'] or '-'} {f['nombre_onu'] or ''}")
    return "Líneas a revisar:\n" + "\n".join(out)


def _pedidos(estado: dict, config: list[dict]) -> list[tuple[str, list[int], dict]]:
    muestra = estado["muestra"]
    sistema, esquema = _sistema(config), _esquema(config)
    pedidos = []
    for n, ini in enumerate(range(0, len(muestra), POR_PEDIDO)):
        idx = list(range(ini, min(ini + POR_PEDIDO, len(muestra))))
        params = {
            "model": estado["modelo"],
            "max_tokens": 16000,
            "system": sistema,
            "messages": [{"role": "user", "content": _texto_filas([muestra[i] for i in idx])}],
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": esquema}},
        }
        pedidos.append((f"p{n:04d}", idx, params))
    return pedidos


def estimar_usd(cliente, pedidos: list, modelo: str) -> tuple[float, int]:
    """Cuenta los tokens de entrada de verdad (count_tokens, gratis) en el primer
    pedido y escala; la salida se estima con margen."""
    p = pedidos[0][2]
    t = cliente.messages.count_tokens(model=p["model"], system=p["system"], messages=p["messages"]).input_tokens
    filas_total = sum(len(x[1]) for x in pedidos)
    entrada = t * filas_total / len(pedidos[0][1])
    salida = filas_total * SALIDA_ESTIMADA_POR_FILA + len(pedidos) * SALIDA_ESTIMADA_POR_PEDIDO
    pin, pout, _, _ = PRECIO_BATCH[modelo]
    return (entrada * pin + salida * pout) / 1e6, int(entrada)


def enviar(cliente, pedidos: list) -> str:
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    lote = cliente.messages.batches.create(requests=[
        Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**params)) for cid, _idx, params in pedidos])
    return lote.id


def esperar(cliente, batch_id: str, max_horas: float = 6.0):
    fin = time.time() + max_horas * 3600
    while time.time() < fin:
        b = cliente.messages.batches.retrieve(batch_id)
        c = b.request_counts
        log.info("lote %s: %s · procesando %d · ok %d · error %d", batch_id, b.processing_status,
                 c.processing, c.succeeded, c.errored)
        if b.processing_status == "ended":
            return b
        time.sleep(30)
    raise TimeoutError(f"El lote {batch_id} no terminó en {max_horas} h: reanudar con --reanudar")


def recoger(cliente, batch_id: str, estado: dict, pedidos: list) -> tuple[dict, Counter]:
    """{índice de la muestra: (categoria, confianza, motivo)} y los tokens usados."""
    por_cid = {cid: idx for cid, idx, _p in pedidos}
    respuestas, tokens = {}, Counter()
    for r in cliente.messages.batches.results(batch_id):
        if r.result.type != "succeeded":
            tokens["pedidos_fallidos"] += 1
            log.warning("pedido %s: %s", r.custom_id, r.result.type)
            continue
        msg = r.result.message
        u = msg.usage
        tokens["in"] += u.input_tokens
        tokens["out"] += u.output_tokens
        tokens["cache_read"] += u.cache_read_input_tokens or 0
        tokens["cache_write"] += u.cache_creation_input_tokens or 0
        if msg.stop_reason != "end_turn":
            tokens["pedidos_fallidos"] += 1
            log.warning("pedido %s terminó con %s", r.custom_id, msg.stop_reason)
            continue
        texto = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            filas = json.loads(texto)["filas"]
        except (json.JSONDecodeError, KeyError):
            tokens["pedidos_fallidos"] += 1
            log.warning("pedido %s: JSON inválido", r.custom_id)
            continue
        idx = por_cid[r.custom_id]
        for x in filas:
            if 0 <= x["i"] < len(idx):
                respuestas[idx[x["i"]]] = (x["categoria"], x["confianza"], x["motivo"][:300])
    return respuestas, tokens


# --------------------------------------------------------------------------
# 3. Guardar, registrar el gasto y generar el Excel
# --------------------------------------------------------------------------
def _admin():
    import pymysql
    p = parametros("F2_ADMIN_MYSQL")
    return pymysql.connect(host=p["host"], port=p["port"], user=p["user"], password=p["password"],
                           database=p["database"], charset="utf8mb4", autocommit=False)


def guardar(estado: dict, respuestas: dict, tokens: Counter, lote: str, modelo: str) -> float:
    pin, pout, pcr, pcw = PRECIO_BATCH[modelo]
    costo = (tokens["in"] * pin + tokens["out"] * pout + tokens["cache_read"] * pcr + tokens["cache_write"] * pcw) / 1e6
    filas = []
    for i, f in enumerate(estado["muestra"]):
        cat, conf, mot = respuestas.get(i, (None, None, "sin respuesta de la IA"))
        filas.append((lote, f["tabla_origen"], f["fila_id"], f["estrato"].split(" · ")[0], f["motivo_grupo"],
                      f["licitacion"], f["fecha_publicacion"] or None, f["descripcion"], f["titulo"],
                      f["codigo_onu"], f["nombre_onu"], f["ia_metodo"], f["clasificador_f1"], cat, conf, mot, modelo))
    conn = _admin()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO clasificador_f2_auditoria (lote, tabla_origen, fila_id, grupo, motivo_grupo, licitacion, "
                "fecha_publicacion, descripcion, titulo, codigo_onu, nombre_onu, ia_metodo, clasificador_f1, "
                "ia_categoria, ia_confianza, ia_motivo, modelo, creado_en) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())", filas)
            # Libro de costos del proyecto: contexto 'ajuste' (no cuenta en la alerta diaria de producción).
            cur.execute(
                "INSERT INTO clasificador_ia_costos (creado_en, contexto, modelo, tokens_in, tokens_out, cache_read, "
                "cache_write, costo_usd, nota) VALUES (UTC_TIMESTAMP(), 'ajuste', %s, %s, %s, %s, %s, %s, %s)",
                (modelo, tokens["in"], tokens["out"], tokens["cache_read"], tokens["cache_write"], round(costo, 6),
                 f"auditoria fase2 {lote} (batch)"))
        conn.commit()
    finally:
        conn.close()
    return costo


def guardar_lote(estado: dict, respuestas: dict, res: list[dict], lote: str, costo: float) -> None:
    conn = _admin()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO clasificador_f2_auditoria_lotes (lote, dias, modelo, rescatadas, sin_rescatar, revisadas, "
                "costo_usd, resumen_json, creado_en) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW()) "
                "ON DUPLICATE KEY UPDATE resumen_json=VALUES(resumen_json), costo_usd=VALUES(costo_usd)",
                (lote, estado["dias"], estado["modelo"], estado["capturadas"], sum(estado["poblacion"].values()),
                 len(respuestas), round(costo, 4), json.dumps(res, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


def resumen(estado: dict, respuestas: dict) -> list[dict]:
    por = defaultdict(lambda: Counter())
    for i, f in enumerate(estado["muestra"]):
        cat, conf, _m = respuestas.get(i, (None, None, None))
        c = por[f["estrato"]]
        c["revisadas"] += 1 if cat else 0
        if cat and cat != "NINGUNA":
            c["si_alta_media" if conf in ("alta", "media") else "si_baja"] += 1
    out = []
    for e, n in sorted(estado["poblacion"].items(), key=lambda x: -x[1]):
        c = por[e]
        tasa = c["si_alta_media"] / c["revisadas"] if c["revisadas"] else 0.0
        out.append({"estrato": e, "poblacion": n, "revisadas": c["revisadas"], "si": c["si_alta_media"],
                    "si_baja": c["si_baja"], "tasa": tasa, "estimado": round(n * tasa)})
    return out


def excel(ruta: str, estado: dict, respuestas: dict, res: list[dict], lote: str, costo: float) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    import re
    ilegal = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
    xl = lambda v: ilegal.sub(" ", v) if isinstance(v, str) else v
    negrita = Font(bold=True)
    fuentes = {"compra_agil": "Compra ágil", "Licitaciones_diarias": "Licitación", "cotizaciones": "Cotización"}
    wb = Workbook()
    ws = wb.active
    ws.title = "Resumen"
    total_est = sum(r["estimado"] for r in res)
    ws.append([f"Auditoría con IA de lo NO rescatado por la Fase 2 · lote {lote}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([f"Ventana: últimos {estado['dias']} días de publicación · rescatadas por la fase 2: {estado['capturadas']} · "
               f"descartadas sin rescatar: {sum(estado['poblacion'].values())} · revisadas por IA ({estado['modelo']}): "
               f"{len(respuestas)} · costo: US$ {costo:.2f}"])
    ws.append(["La IA revisó una MUESTRA de cada estrato. 'Estimado' = población × tasa de 'sí' (confianza alta o "
               "media) de la muestra: es una estimación, no un conteo."])
    ws.append([])
    ws.append(["Estrato (por qué quedó fuera)", "Población", "Revisadas", "IA: sí (alta/media)", "IA: sí (baja)",
               "Tasa", "Estimado de faltantes"])
    for c in ws[5]:
        c.font = negrita
    for r in res:
        ws.append([r["estrato"], r["poblacion"], r["revisadas"], r["si"], r["si_baja"], round(r["tasa"], 3), r["estimado"]])
    ws.append(["TOTAL", sum(r["poblacion"] for r in res), sum(r["revisadas"] for r in res), sum(r["si"] for r in res),
               sum(r["si_baja"] for r in res), None, total_est])
    for c in ws[ws.max_row]:
        c.font = negrita
    ws.column_dimensions["A"].width = 70

    def hoja(titulo, filtro):
        h = wb.create_sheet(titulo)
        h.append(["Confianza", "Categoría IA", "Motivo IA", "Por qué quedó fuera", "Fuente", "Licitación / código",
                  "Publicación", "Glosa", "Título", "Código ONU", "Nombre ONU", "Fase 1", "Clasificó fase 1"])
        for c in h[1]:
            c.font = negrita
        orden = {"alta": 0, "media": 1, "baja": 2, None: 3}
        items = [(i, f) for i, f in enumerate(estado["muestra"]) if filtro(respuestas.get(i))]
        items.sort(key=lambda x: (orden.get((respuestas.get(x[0]) or (None, None))[1], 3), x[1]["estrato"]))
        for i, f in items:
            cat, conf, mot = respuestas.get(i, (None, None, None))
            h.append([xl(v) for v in [conf, cat, mot, f["motivo_grupo"], fuentes.get(f["tabla_origen"], f["tabla_origen"]),
                                      f["licitacion"], f["fecha_publicacion"], f["descripcion"], f["titulo"],
                                      f["codigo_onu"], f["nombre_onu"], f["ia_metodo"], f["clasificador_f1"]]])
            if conf == "alta" and cat not in (None, "NINGUNA"):
                for c in h[h.max_row][:3]:
                    c.fill = PatternFill("solid", fgColor="FDECCF")
        h.freeze_panes = "A2"
        for col, w in (("C", 45), ("D", 45), ("H", 70), ("I", 45), ("K", 35)):
            h.column_dimensions[col].width = w

    hoja("Posibles faltantes", lambda r: bool(r) and r[0] != "NINGUNA")
    hoja("Todo lo revisado", lambda r: True)
    wb.save(ruta)


# --------------------------------------------------------------------------
def main() -> None:
    import anthropic
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dias", type=int, default=30)
    ap.add_argument("--por-estrato", type=int, default=150, help="muestra por estrato 'cercano'")
    ap.add_argument("--onu-salud", type=int, default=1500)
    ap.add_argument("--resto", type=int, default=1000)
    ap.add_argument("--tope-usd", type=float, default=10.0)
    ap.add_argument("--estado", default="auditoria_estado.json", help="archivo de estado (para reanudar)")
    ap.add_argument("--reanudar", help="archivo de estado de una corrida ya enviada")
    ap.add_argument("--solo-estimar", action="store_true")
    ap.add_argument("--excel", default="auditoria_fase2.xlsx")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    bd = BD()
    config = barrido.cargar_config(bd)
    cliente = anthropic.Anthropic()

    if a.reanudar:
        estado = json.loads(Path(a.reanudar).read_text(encoding="utf-8"))
        pedidos = _pedidos(estado, config)
    else:
        estado = seleccionar(bd, a.dias, a.por_estrato, a.onu_salud, a.resto)
        pedidos = _pedidos(estado, config)
        usd, tin = estimar_usd(cliente, pedidos, estado["modelo"])
        estado["estimado_usd"] = round(usd, 2)
        log.info("Población sin rescatar: %s", estado["poblacion"])
        log.info("Muestra: %d filas en %d pedidos · entrada ~%d tokens · costo estimado US$ %.2f (tope %.2f)",
                 len(estado["muestra"]), len(pedidos), tin, usd, a.tope_usd)
        if a.solo_estimar:
            Path(a.estado).write_text(json.dumps(estado, ensure_ascii=False), encoding="utf-8")
            return
        if usd > a.tope_usd:
            raise SystemExit(f"Costo estimado US$ {usd:.2f} supera el tope US$ {a.tope_usd:.2f}: no se envía nada.")
        estado["batch_id"] = enviar(cliente, pedidos)
        Path(a.estado).write_text(json.dumps(estado, ensure_ascii=False), encoding="utf-8")
        log.info("Lote enviado: %s (estado en %s)", estado["batch_id"], a.estado)

    esperar(cliente, estado["batch_id"])
    respuestas, tokens = recoger(cliente, estado["batch_id"], estado, pedidos)
    lote = f"{datetime.now():%Y%m%d-%H%M}-{estado['batch_id'][-6:]}"
    costo = guardar(estado, respuestas, tokens, lote, estado["modelo"])
    res = resumen(estado, respuestas)
    guardar_lote(estado, respuestas, res, lote, costo)
    excel(a.excel, estado, respuestas, res, lote, costo)
    log.info("Lote %s: %d respuestas, %d pedidos fallidos, costo real US$ %.2f · Excel %s",
             lote, len(respuestas), tokens["pedidos_fallidos"], costo, a.excel)
    for r in res:
        log.info("  %-70s pobl %6d · rev %4d · sí %3d (+%d baja) · estimado %d",
                 r["estrato"][:70], r["poblacion"], r["revisadas"], r["si"], r["si_baja"], r["estimado"])


if __name__ == "__main__":
    main()
