"""Backtest de la cascada SIN gastar un centavo de API.

Cómo evita pagar
----------------
`clasificador_ia_log` ya guarda lo que Claude respondió para cada fila que
procesó producción. El backtest re-corre la cascada real (`cascada.clasificar_fila`)
con la llamada a Claude REEMPLAZADA por esa respuesta guardada. Resultado:
medición end-to-end de la cascada completa, coste $0. Respeta la regla del
proyecto: los backtests masivos NO llaman a la API.

Las filas cuya respuesta de Claude no está en el log se contabilizan aparte
(`sin_dato_claude`) y NO entran en las métricas — nunca se inventa una respuesta.

Cómo se usa
-----------
    # 1) fotografía del estado actual
    python3 backtest_cascada.py --semanas 4 --por-semana 400 --etiqueta antes

    # 2) se aplica UN cambio (un veto, un umbral, reordenar una rama)

    # 3) misma ventana, misma muestra -> comparable
    python3 backtest_cascada.py --semanas 4 --por-semana 400 --etiqueta despues
    python3 backtest_cascada.py --comparar antes despues

Un cambio por corrida. Si se tocan cuatro cosas juntas no se sabe cuál movió
la aguja.

Sesgo conocido (vale para AMBOS lados de la comparación, no invalida el A/B):
los índices de `cruce_base` e `historico` se construyen HOY, con etiquetas
humanas posteriores a la ventana medida. Eso hace que esas dos ramas se vean
algo mejor de lo que fueron en su momento.
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import cascada
import clasificador_claude as cc
from cruce_base import cargar_cruce_base
from db import conexion_worker
from descarte_items import cargar_descartes
from descarte_modelo import cargar_modelo_descarte
from ejemplos import cargar_ejemplos
from fuentes import FUENTES, precargar_sufijos
from marcas import cargar_marcas_para_prompt
from modelo_adjunto import cargar_modelo_adjunto
from modelo_marcas import cargar_modelo_marcas
from modelo_pactivo import cargar_modelo_pactivo
from preclasificador import precargar_comp_pres
from reglas import indexar_combinaciones, indexar_inverso_pactivos, indexar_pactivos, normalizar
from reglas_negocio import cargar_feedback
from taxonomia import cargar_taxonomia

log = logging.getLogger("backtest")
# Default = las dos fuentes históricas: así las corridas previas siguen siendo
# comparables. Otra fuente se mide aparte con --tablas (ej. --tablas cotizaciones).
TABLAS = ["compra_agil", "Licitaciones_diarias"]
SALIDA = Path(__file__).resolve().parent / "backtest_resultados"

# SEGMENTOS — un cambio dirigido hay que medirlo DONDE ACTÚA, no diluido en el
# flujo entero. Medido 2026-08-24: CENABAST son 70 de 36.502 filas clasificadas
# en 4 semanas (0,19 %); con muestra de 400/semana el backtest global tomaría
# ~3 filas y sería CIEGO a cualquier arreglo de ese segmento. Correr el global
# para no romper nada, y el del segmento para probar que arregla algo.
SEGMENTOS = {
    "todo": "",
    # OJO con los % : el fragmento se concatena a un SQL que después pasa por el
    # formateo de parámetros de pymysql, así que un '%' literal debe ir DUPLICADO
    # o revienta con "unsupported format character" (pasó 2026-08-24).
    "cenabast": "AND t.Demandante LIKE '%%Central De Abastecimiento%%'",
    "adjunto": "AND t.Estado_creado_adjunto = 1",
    "interes": "AND t.estado_gestor = 1",
}

# Filas ya clasificadas por una PERSONA en la ventana, junto con lo que Claude
# respondió en producción (si la fila llegó a Claude). El LEFT JOIN es lo que
# permite no pagar: la respuesta ya está guardada.
_SQL = """
SELECT t.id, t.Titulo, t.Descripcion, t.VINCULOS, t.Item, t.Cod_Onu,
       t.estado_gestor AS h_interes, t.pactivo AS h_pactivo,
       t.composicion AS h_comp, t.presentacion AS h_pres,
       l.metodo AS log_metodo, l.interes_sugerido AS cl_interes,
       l.pactivo_sugerido AS cl_pactivo, l.composicion_sugerida AS cl_comp,
       l.presentacion_sugerida AS cl_pres, l.confianza AS cl_conf,
       l.razon AS cl_razon, l.pactivo_nuevo AS cl_nuevo
FROM `{tabla}` t
LEFT JOIN clasificador_ia_log l
       ON l.tabla_origen = %s AND l.fila_id = t.id
WHERE t.estado_gestor IS NOT NULL
  AND t.nombre_clasificador IS NOT NULL
  AND t.nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_)'
  AND t.`{col_fecha}` >= %s AND t.`{col_fecha}` < %s
  {segmento}
ORDER BY MD5(t.id)
LIMIT %s
"""
# MUESTREO: `ORDER BY MD5(t.id)` en vez de `ORDER BY t.id`. Con el orden por id
# se tomaban las PRIMERAS 400 filas de cada semana — las de menor id, o sea las
# insertadas más temprano: una submuestra sesgada por hora de scraping y por lote
# de origen. El hash da una muestra pseudo-aleatoria y, sobre todo, DETERMINISTA:
# la corrida 'antes' y la 'despues' evalúan exactamente las mismas filas, que es
# la condición para que el delta signifique algo.


class _ClaudeDesdeLog:
    """Sustituye la llamada a la API por la respuesta que ya está en el log.

    `cascada` llama `cc.clasificar(...)` y espera (Clasificacion, Uso). Acá se
    devuelve lo guardado, con Uso en cero: el backtest no gasta. Si la fila no
    tiene respuesta guardada, se marca y la cascada recibe un descarte neutro
    que el reporte excluye de las métricas (nunca se inventa una respuesta)."""

    def __init__(self) -> None:
        self.fila: dict = {}
        self.sin_dato = 0
        self.usadas = 0

    def __call__(self, descripcion, titulo, vinculos, taxonomia, ejemplos="",
                 candidatos=None, marcas_texto=""):
        f = self.fila
        uso = cc.Uso(tokens_in=0, tokens_out=0, cache_read=0, cache_write=0, costo_usd=0.0)
        if f.get("cl_interes") is None or not (f.get("log_metodo") or "").startswith("claude"):
            self.sin_dato = self.sin_dato + 1
            f["_sin_dato_claude"] = True
            return cc.Clasificacion(interes=0, pactivo=None, composicion=None,
                                    presentacion=None, confianza=0.0,
                                    razon="SIN DATO DE CLAUDE EN EL LOG",
                                    pactivo_fuera_de_lista=0, pactivo_propuesto=None), uso
        self.usadas += 1
        return cc.Clasificacion(
            interes=int(f["cl_interes"]),
            pactivo=f.get("cl_pactivo"),
            composicion=f.get("cl_comp"),
            presentacion=f.get("cl_pres"),
            confianza=float(f.get("cl_conf") or 0.5),
            razon=f.get("cl_razon") or "",
            pactivo_fuera_de_lista=1 if f.get("cl_nuevo") else 0,
            pactivo_propuesto=f.get("cl_nuevo"),
        ), uso


def _asegurar_credenciales_prime() -> None:
    """El catálogo ACTIVO se arma cruzando contra prime (clientes vivos). Sin esas
    credenciales `construir_filtro_activo` no ve RUTs y el catálogo local queda
    degradado (~1.200 pactivos en vez de ~1.400) — el backtest mediría contra un
    catálogo que no es el de producción y el final guard descartaría de más.

    En el container las variables vienen del .env. En una máquina de desarrollo
    se toman del helper central `Conexiones_Mysql/connections.py` si está a mano,
    para no tener que copiar la clave a ningún archivo. Falla blanda: si no se
    puede, se avisa fuerte y se sigue (la corrida NO es comparable con prod)."""
    import os
    if os.getenv("MYSQL_PRIME_PASSWORD"):
        return
    for ruta in (Path(__file__).resolve().parents[1] / "Conexiones_Mysql",):
        if not (ruta / "connections.py").exists():
            continue
        import sys as _sys
        _sys.path.insert(0, str(ruta))
        try:
            from connections import get_config  # type: ignore
            c = get_config("prime")
            os.environ.setdefault("MYSQL_PRIME_HOST", c.host)
            os.environ.setdefault("MYSQL_PRIME_PORT", str(c.port))
            os.environ.setdefault("MYSQL_PRIME_USER", c.user)
            os.environ["MYSQL_PRIME_PASSWORD"] = c.password
            log.info("Credenciales de prime tomadas de Conexiones_Mysql (dev).")
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("no se pudo usar el helper de conexiones (%s)", exc)
    log.warning("SIN credenciales de prime: el catálogo activo quedará DEGRADADO "
                "y esta corrida NO es comparable con producción.")


def cargar_recursos() -> dict:
    _asegurar_credenciales_prime()
    log.info("Cargando catálogo, índices y modelos (una vez)...")
    tax = cargar_taxonomia()
    precargar_comp_pres(TABLAS)
    precargar_sufijos(TABLAS)
    pactivos_norm = indexar_pactivos(tax.pactivos)
    try:
        marcas_texto = cargar_marcas_para_prompt(pactivos_norm)
    except Exception:  # noqa: BLE001
        marcas_texto = ""
    r = {
        "taxonomia": tax,
        "pactivos_norm": pactivos_norm,
        "combinaciones": indexar_combinaciones(tax.pactivos),
        "indice_inverso": indexar_inverso_pactivos(tax.pactivos),
        "descartes": cargar_descartes(),
        "cruce": cargar_cruce_base(),
        "modelo_descarte": cargar_modelo_descarte(),
        "modelo_pactivo": cargar_modelo_pactivo(),
        "modelo_marcas": cargar_modelo_marcas(),
        "modelo_adjunto": cargar_modelo_adjunto(),
        "contexto": "\n\n".join(p for p in (cargar_ejemplos(), cargar_feedback()) if p),
        "marcas_texto": marcas_texto,
    }
    cascada.recargar_vetos_dinamicos()

    # GUARD DE FIDELIDAD. Si un modelo no está en disco, su rama se saltea EN
    # SILENCIO y las filas que debía resolver caen a las ramas de abajo — el
    # backtest mediría una cascada que no es la de producción, y el error se
    # atribuiría a la rama equivocada. Los .joblib grandes están gitignoreados
    # (modelo_pactivo ~537 MB, modelo_marcas ~76 MB: viven en /opt/ia-mp del
    # host), así que en una máquina de desarrollo esto pasa fácil. Se registra
    # en el resultado y se imprime en TODA comparación: una corrida incompleta
    # nunca se puede confundir con una completa.
    r["ramas_ausentes"] = [
        nombre for nombre, obj in (
            ("modelo_descarte", r["modelo_descarte"]),
            ("modelo_pactivo", r["modelo_pactivo"]),
            ("modelo_marcas", r["modelo_marcas"]),
            ("modelo_adjunto", r["modelo_adjunto"]),
        ) if obj is None
    ]
    if r["ramas_ausentes"]:
        log.warning("RAMAS AUSENTES (no medidas, sus filas caen a ramas de abajo): %s",
                    ", ".join(r["ramas_ausentes"]))
    log.info("Recursos listos: %d pactivos activos.", len(tax.pactivos))
    return r


def _filas(tabla: str, desde: str, hasta: str, limite: int,
           segmento: str = "") -> list[dict]:
    with conexion_worker().cursor() as cur:
        # Cada fuente declara con qué fecha se muestrea (fuentes.py): en
        # cotizaciones los descartes humanos no tienen fecha_clasificacion.
        col_fecha = FUENTES[tabla].columna_fecha_muestreo
        cur.execute(_SQL.format(tabla=tabla, segmento=segmento, col_fecha=col_fecha),
                    (tabla, desde, hasta, limite))
        return list(cur.fetchall())


def correr_semana(r: dict, stub: _ClaudeDesdeLog, desde: str, hasta: str,
                  por_semana: int, segmento: str = "") -> dict:
    """Re-corre la cascada sobre una semana y compara contra el humano."""
    acc: dict = defaultdict(lambda: defaultdict(int))
    tot = defaultdict(int)
    for tabla in TABLAS:
        for fila in _filas(tabla, desde, hasta, por_semana, segmento):
            stub.fila = fila
            fila.pop("_sin_dato_claude", None)
            try:
                res = cascada.clasificar_fila(
                    tabla, fila, r["taxonomia"], r["pactivos_norm"], r["descartes"],
                    r["cruce"], r["combinaciones"], r["modelo_descarte"], r["contexto"],
                    indice_inverso=r["indice_inverso"], modelo_pactivo=r["modelo_pactivo"],
                    marcas_texto=r["marcas_texto"], modelo_marcas=r["modelo_marcas"],
                    modelo_adjunto=r["modelo_adjunto"],
                )
            except Exception as exc:  # noqa: BLE001
                tot["error"] += 1
                log.debug("fila %s: %s", fila.get("id"), exc)
                continue
            tot["evaluadas"] += 1
            if fila.get("_sin_dato_claude"):
                tot["sin_dato_claude"] += 1
                # Fuente nueva (sin historia en el log): estas filas son las que
                # LLEGARÍAN a Claude. Se cuentan con su etiqueta humana para
                # dimensionar costo y cuánto interés depende de Claude.
                tot["a_claude_h_interes"] += int(fila["h_interes"] == 1)
                continue
            tot["comparables"] += 1
            m = res.metodo
            a = acc[m]
            a["n"] += 1
            h_int = int(fila["h_interes"])
            if res.interes == 1:
                a["interes"] += 1
                if h_int == 0:
                    a["fp"] += 1; tot["fp"] += 1          # la persona lo ELIMINA
                elif normalizar(res.pactivo) != normalizar(fila.get("h_pactivo")):
                    a["pact_malo"] += 1; tot["pact_malo"] += 1   # la persona lo EDITA
                else:
                    a["ok"] += 1
                    if (normalizar(res.composicion) != normalizar(fila.get("h_comp"))
                            or normalizar(res.presentacion) != normalizar(fila.get("h_pres"))):
                        a["comp_pres"] += 1; tot["comp_pres"] += 1
            else:
                if h_int == 1:
                    a["fn"] += 1; tot["fn"] += 1          # la persona lo RESCATA
                else:
                    a["ok"] += 1
    return {"desde": desde, "hasta": hasta, "totales": dict(tot),
            "por_metodo": {k: dict(v) for k, v in acc.items()}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--semanas", type=int, default=4)
    ap.add_argument("--por-semana", type=int, default=400,
                    help="filas por tabla y por semana")
    ap.add_argument("--etiqueta", help="nombre con el que se guarda esta corrida")
    ap.add_argument("--segmento", default="todo", choices=sorted(SEGMENTOS),
                    help="acotar a un segmento: un cambio dirigido se mide donde actúa")
    ap.add_argument("--comparar", nargs=2, metavar=("ANTES", "DESPUES"))
    ap.add_argument("--tablas", default=",".join(TABLAS),
                    help="fuentes a medir, separadas por coma (default: las dos históricas)")
    args = ap.parse_args()
    TABLAS[:] = [t.strip() for t in args.tablas.split(",") if t.strip()]
    desconocidas = [t for t in TABLAS if t not in FUENTES]
    if desconocidas and not args.comparar:
        ap.error(f"tablas no registradas en fuentes.py: {desconocidas}")

    SALIDA.mkdir(exist_ok=True)
    if args.comparar:
        return comparar(*args.comparar)
    if not args.etiqueta:
        ap.error("hace falta --etiqueta (o --comparar A B)")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = cargar_recursos()
    stub = _ClaudeDesdeLog()
    cc.clasificar = stub  # a partir de acá la cascada NO llama a la API

    semanas = []
    for i in range(args.semanas, 0, -1):
        with conexion_worker().cursor() as cur:
            cur.execute("SELECT DATE_SUB(CURDATE(), INTERVAL %s DAY) d1, "
                        "DATE_SUB(CURDATE(), INTERVAL %s DAY) d2", (i * 7, (i - 1) * 7))
            f = cur.fetchone()
        d1, d2 = str(f["d1"]), str(f["d2"])
        log.info("Semana %s -> %s ...", d1, d2)
        s = correr_semana(r, stub, d1, d2, args.por_semana, SEGMENTOS[args.segmento])
        semanas.append(s)
        t = s["totales"]
        log.info("  comparables=%s FP=%s pact_malo=%s FN=%s",
                 t.get("comparables", 0), t.get("fp", 0), t.get("pact_malo", 0), t.get("fn", 0))

    res = {"etiqueta": args.etiqueta, "corrida": datetime.now().isoformat(timespec="seconds"),
           "semanas": semanas, "claude_desde_log": stub.usadas, "sin_dato_claude": stub.sin_dato,
           "ramas_ausentes": r["ramas_ausentes"], "pactivos_activos": len(r["taxonomia"].pactivos),
           "segmento": args.segmento, "tablas": TABLAS}
    (SALIDA / f"{args.etiqueta}.json").write_text(json.dumps(res, indent=2, ensure_ascii=False))
    imprimir(res)


def _agregado(res: dict) -> dict:
    t = defaultdict(int)
    for s in res["semanas"]:
        for k, v in s["totales"].items():
            t[k] += v
    return dict(t)


def imprimir(res: dict) -> None:
    print(f"\n=== BACKTEST '{res['etiqueta']}' · {len(res['semanas'])} semanas · "
          f"segmento: {res.get('segmento','todo')} · $0 de API ===\n")
    print(f"{'semana':<26}{'comparables':>12}{'FP':>7}{'pact✗':>7}{'FN':>6}{'c/p':>7}{'err/1000':>10}")
    for s in res["semanas"]:
        t = s["totales"]
        n = t.get("comparables", 0) or 1
        err = t.get("fp", 0) + t.get("pact_malo", 0) + t.get("fn", 0)
        print(f"{s['desde']+' → '+s['hasta']:<26}{t.get('comparables',0):>12}"
              f"{t.get('fp',0):>7}{t.get('pact_malo',0):>7}{t.get('fn',0):>6}"
              f"{t.get('comp_pres',0):>7}{1000*err/n:>10.1f}")
    a = _agregado(res)
    n = a.get("comparables", 0) or 1
    err = a.get("fp", 0) + a.get("pact_malo", 0) + a.get("fn", 0)
    print(f"{'TOTAL':<26}{a.get('comparables',0):>12}{a.get('fp',0):>7}"
          f"{a.get('pact_malo',0):>7}{a.get('fn',0):>6}{a.get('comp_pres',0):>7}{1000*err/n:>10.1f}")
    print(f"\nrespuestas de Claude reusadas del log: {res['claude_desde_log']}  ·  "
          f"filas sin dato de Claude (excluidas): {res['sin_dato_claude']}")
    print(f"catálogo activo: {res.get('pactivos_activos','?')} pactivos  ·  "
          f"tablas: {', '.join(res.get('tablas') or ['compra_agil', 'Licitaciones_diarias'])}")
    if a.get("sin_dato_claude"):
        print(f"llegarían a Claude sin respuesta guardada: {a['sin_dato_claude']} "
              f"(de {a.get('evaluadas', 0)} evaluadas), de ellas "
              f"{a.get('a_claude_h_interes', 0)} son interés humano")
    _avisar_ramas(res)
    print()


def _avisar_ramas(res: dict) -> None:
    """Una corrida a la que le faltan ramas NO es comparable con producción."""
    faltan = res.get("ramas_ausentes") or []
    if faltan:
        print(f"\n  ⚠ CORRIDA INCOMPLETA — ramas no medidas: {', '.join(faltan)}")
        print("    Sus filas cayeron a ramas de abajo; el error está mal atribuido.")


def comparar(a: str, b: str) -> None:
    ra = json.loads((SALIDA / f"{a}.json").read_text())
    rb = json.loads((SALIDA / f"{b}.json").read_text())
    # Comparar dos corridas con distinto set de ramas es comparar dos cascadas
    # distintas: el delta no significaría nada. Se corta antes de imprimirlo.
    if ra.get("segmento", "todo") != rb.get("segmento", "todo"):
        print(f"\n✖ NO COMPARABLE: '{a}' es del segmento '{ra.get('segmento','todo')}' "
              f"y '{b}' del segmento '{rb.get('segmento','todo')}'.\n")
        return
    _def = ["compra_agil", "Licitaciones_diarias"]
    if (ra.get("tablas") or _def) != (rb.get("tablas") or _def):
        print(f"\n✖ NO COMPARABLE: '{a}' mide {ra.get('tablas') or _def} y "
              f"'{b}' mide {rb.get('tablas') or _def}.\n")
        return
    if sorted(ra.get("ramas_ausentes") or []) != sorted(rb.get("ramas_ausentes") or []):
        print(f"\n✖ NO COMPARABLE: '{a}' y '{b}' se corrieron con ramas distintas "
              f"({ra.get('ramas_ausentes')} vs {rb.get('ramas_ausentes')}). "
              f"Volvé a correr ambas con los mismos modelos disponibles.\n")
        return
    ta, tb = _agregado(ra), _agregado(rb)
    na = ta.get("comparables", 0) or 1
    nb = tb.get("comparables", 0) or 1
    print(f"\n=== {a}  vs  {b} ===\n")
    print(f"{'métrica':<26}{a:>12}{b:>12}{'delta':>10}{'por 1000':>12}")
    for k, etiq in (("fp", "FP (la eliminan)"), ("pact_malo", "pactivo malo (editan)"),
                    ("fn", "FN (la rescatan)"), ("comp_pres", "sólo comp/pres")):
        va, vb = ta.get(k, 0), tb.get(k, 0)
        d = 1000 * vb / nb - 1000 * va / na
        print(f"{etiq:<26}{va:>12}{vb:>12}{vb-va:>+10}{d:>+12.2f}")
    ea = ta.get("fp", 0) + ta.get("pact_malo", 0) + ta.get("fn", 0)
    eb = tb.get("fp", 0) + tb.get("pact_malo", 0) + tb.get("fn", 0)
    d = 1000 * eb / nb - 1000 * ea / na
    print(f"{'TOTAL errores':<26}{ea:>12}{eb:>12}{eb-ea:>+10}{d:>+12.2f}")
    print(f"\ncomparables: {a}={na}  {b}={nb}")
    _avisar_ramas(ra)
    print("MEJORA\n" if d < 0 else ("EMPEORA\n" if d > 0 else "SIN CAMBIO\n"))


if __name__ == "__main__":
    main()
