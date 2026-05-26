"""Outbox de aprobaciones — garantía de que NADA se pierde.

Cuando el revisor aprueba un lote en /revision, el endpoint `revisar_hoja`:
  1) PERSISTE el lote como JSON en /app/pending/ (montado del host) ANTES
     de tocar la BD. Escritura atómica con .tmp + rename.
  2) INTENTA aplicar a clasico con `aplicar_lote()`. Si funciona, borra el
     JSON. Si NO funciona (red caída, BD inaccesible, container crash), el
     JSON queda y se reintenta:
       - Manualmente desde el botón "Sincronizar pendientes" en el panel
       - Automáticamente por cron cada 5 min (este mismo script en modo CLI)

Cada item del lote es IDEMPOTENTE: el script verifica `revisado` en el log
y omite las que ya están cerradas. Reintentar el mismo JSON no duplica nada.

Estructura del JSON:
  {
    "ts": "2026-05-26T20:30:45",
    "revisor": "Yoryet Danoun",
    "items": [
      {"log_id": "...", "decision": "aprobar"|"corregir"|"descartar",
       "pactivo": "...", "composicion": "...", "presentacion": "...",
       "motivo": "..."},
      ...
    ]
  }

Uso CLI (cron):
    python3 /app/sync_pendientes.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from db import conectar

log = logging.getLogger("sync_pendientes")

# Dentro del container está montado como /app/pending. En el host es
# /opt/ia-mp/pending (ver docker-compose.yml).
PENDING_DIR = Path("/app/pending")

TABLAS_VALIDAS = ("compra_agil", "Licitaciones_diarias")


def _aplicar_item(cur, item: dict, revisor: str, ahora: datetime) -> str:
    """Aplica UNA fila del lote. Devuelve 'aplicada' | 'ya_revisada' | 'sin_log'.
    Si falla la BD, propaga la excepción para que `aplicar_lote` decida si
    deja el JSON en pending."""
    lid = item["log_id"]
    dec = item.get("decision", "aprobar")
    pact = (item.get("pactivo") or "").strip()
    comp = (item.get("composicion") or "").strip()
    pres = (item.get("presentacion") or "").strip()
    mot = (item.get("motivo") or "").strip()

    # Lee el registro original del log
    cur.execute(
        "SELECT tabla_origen, fila_id, interes_sugerido, pactivo_sugerido, revisado "
        "FROM clasificador_ia_log WHERE id=%s",
        (lid,),
    )
    reg = cur.fetchone()
    if not reg:
        return "sin_log"
    if reg.get("revisado"):
        return "ya_revisada"  # idempotente: alguien (este mismo retry?) ya la aplicó
    tabla_o = reg["tabla_origen"]
    if tabla_o not in TABLAS_VALIDAS:
        return "sin_log"

    # Decisión → estado y campos a escribir
    if dec in ("corregir", "descartar") and not mot:
        return "sin_motivo"
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

    # 1) escribir en la tabla origen
    cur.execute(
        f"UPDATE `{tabla_o}` SET estado_gestor=%s, pactivo=%s, composicion=%s, "
        f"presentacion=%s, nombre_clasificador=%s, fecha_clasificacion=%s "
        f"WHERE id=%s",
        (estado, p, c, pr, revisor, ahora, reg["fila_id"]),
    )
    # 2) cerrar auditoría
    cur.execute(
        "UPDATE clasificador_ia_log SET revisado=1, revisado_por=%s, "
        "revisado_en=%s, feedback_correcto=%s, feedback_pactivo=%s, "
        "feedback_notas=%s WHERE id=%s",
        (revisor, ahora, correcto, p if dec == "corregir" else None,
         mot or None, lid),
    )
    # 3) el motivo se guarda como regla
    if dec in ("corregir", "descartar") and mot:
        cur.execute(
            "INSERT INTO clasificador_ia_reglas "
            "(tipo, texto, fila_ref, pactivo_malo, pactivo_bueno, "
            " creado_por, creado_en, activa) "
            "VALUES ('correccion',%s,%s,%s,%s,%s,%s,1)",
            (mot, f"{tabla_o}#{reg['fila_id']}", reg["pactivo_sugerido"],
             p if dec == "corregir" else None, revisor, ahora),
        )
    return "aplicada"


def aplicar_lote(lote: dict) -> tuple[int, int, int]:
    """Aplica TODO el lote en UNA transacción. Devuelve
    (aplicadas, ya_revisadas, sin_motivo). Si falla la conexión a la BD,
    propaga la excepción — el JSON queda en pending para reintento."""
    revisor = (lote.get("revisor") or "anónimo")[:80]
    ahora = datetime.now()
    aplicadas = ya_revisadas = sin_motivo = 0
    conn = conectar()
    try:
        with conn.cursor() as cur:
            for item in lote.get("items", []):
                r = _aplicar_item(cur, item, revisor, ahora)
                if r == "aplicada":
                    aplicadas += 1
                elif r == "ya_revisada":
                    ya_revisadas += 1
                elif r == "sin_motivo":
                    sin_motivo += 1
        conn.commit()
    finally:
        conn.close()
    return aplicadas, ya_revisadas, sin_motivo


def guardar_pending(lote: dict) -> Path:
    """Escribe el lote a `pending/` con write+rename atómico. Devuelve el path."""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    ts_safe = lote["ts"].replace(":", "-").replace(".", "-")
    tmp = PENDING_DIR / f".{ts_safe}.tmp"
    final = PENDING_DIR / f"{ts_safe}.json"
    tmp.write_text(json.dumps(lote, ensure_ascii=False, indent=2))
    tmp.rename(final)
    return final


def listar_pendientes() -> list[Path]:
    """Lista los lotes JSON pendientes (excluye .tmp en curso)."""
    if not PENDING_DIR.exists():
        return []
    return sorted(p for p in PENDING_DIR.glob("*.json") if not p.name.startswith("."))


def procesar_pendientes() -> dict:
    """Lee `pending/`, intenta aplicar cada lote, borra los que se aplican.
    Devuelve un resumen para el panel y los logs del cron."""
    resumen = {"lotes_ok": 0, "lotes_fallidos": 0, "filas_aplicadas": 0,
               "filas_ya_revisadas": 0, "errores": []}
    for fp in listar_pendientes():
        try:
            lote = json.loads(fp.read_text())
        except Exception as exc:  # noqa: BLE001
            resumen["errores"].append(f"{fp.name}: JSON inválido ({exc})")
            resumen["lotes_fallidos"] += 1
            continue
        try:
            aplic, ya, _ = aplicar_lote(lote)
            resumen["filas_aplicadas"] += aplic
            resumen["filas_ya_revisadas"] += ya
            fp.unlink()  # éxito: se borra el JSON
            resumen["lotes_ok"] += 1
        except Exception as exc:  # noqa: BLE001
            resumen["errores"].append(f"{fp.name}: {exc}")
            resumen["lotes_fallidos"] += 1
    return resumen


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    r = procesar_pendientes()
    log.info("Sync pendientes: %d lotes OK, %d fallidos, %d filas aplicadas, "
             "%d ya estaban revisadas",
             r["lotes_ok"], r["lotes_fallidos"], r["filas_aplicadas"],
             r["filas_ya_revisadas"])
    if r["errores"]:
        log.warning("Errores:\n  - " + "\n  - ".join(r["errores"]))
    sys.exit(0 if r["lotes_fallidos"] == 0 else 1)
