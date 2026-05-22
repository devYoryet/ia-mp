"""Persistencia del resultado de clasificación.

- modo 'test'       → `registrar_backtest`: guarda la predicción de la IA junto
  a lo que dejó la persona, en `clasificador_ia_backtest`. NO toca compra_agil.
- modo 'produccion' → `aplicar_produccion`: escribe la clasificación en la fila
  origen y registra auditoría en `clasificador_ia_log`.

Usa la conexión compartida del worker (no abre una por fila)."""

from __future__ import annotations

from datetime import datetime

from cascada import Resultado
from clasificador_claude import PROMPT_VERSION
from config import config
from db import conexion_worker
from reglas import normalizar

CLASIFICADOR = "Bot IA"


def _registrar_costo(cur, r: Resultado) -> None:
    """Anota el costo de una llamada a Claude en el libro de costos
    (clasificador_ia_costos). Esa tabla no se trunca: es el control de presupuesto."""
    if r.metodo == "claude" and r.costo_usd:
        cur.execute(
            "INSERT INTO clasificador_ia_costos (creado_en, contexto, modelo, "
            "tokens_in, tokens_out, cache_read, cache_write, costo_usd) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (datetime.now(), config.modo, config.modelo, r.tokens_in,
             r.tokens_out, r.cache_read, r.cache_write, r.costo_usd),
        )


# --------------------------------------------------------------------------
# MODO TEST — comparación humano vs IA, sin tocar producción
# --------------------------------------------------------------------------
def registrar_backtest(tabla: str, fila: dict, r: Resultado) -> None:
    humano_estado = fila.get("humano_estado")
    humano_pactivo = fila.get("humano_pactivo")
    humano_comp = fila.get("humano_composicion")
    humano_pres = fila.get("humano_presentacion")

    coincide_interes = int(r.interes == humano_estado) if r.interes is not None else 0
    coincide_pactivo = coincide_comp = coincide_pres = None
    if humano_estado == 1 and r.interes == 1:
        coincide_pactivo = int(bool(normalizar(r.pactivo))
                               and normalizar(r.pactivo) == normalizar(humano_pactivo))
        coincide_comp = int(normalizar(r.composicion) == normalizar(humano_comp))
        coincide_pres = int(normalizar(r.presentacion) == normalizar(humano_pres))

    conn = conexion_worker()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT IGNORE INTO clasificador_ia_backtest "
            "(tabla_origen, fila_id, descripcion, "
            " humano_estado_gestor, humano_pactivo, humano_composicion, humano_presentacion, "
            " ia_interes, ia_pactivo, ia_composicion, ia_presentacion, ia_confianza, "
            " ia_metodo, ia_razon, ia_pactivo_nuevo, "
            " coincide_interes, coincide_pactivo, coincide_composicion, coincide_presentacion, "
            " modelo, tokens_in, tokens_out, cache_read_tok, cache_write_tok, costo_usd, "
            " creado_en) "
            "VALUES (" + ",".join(["%s"] * 26) + ")",
            (
                tabla, fila["id"], (fila.get("Descripcion") or "")[:1000],
                humano_estado, humano_pactivo, humano_comp, humano_pres,
                r.interes, r.pactivo, r.composicion, r.presentacion, r.confianza,
                r.metodo, r.razon, r.pactivo_propuesto,
                coincide_interes, coincide_pactivo, coincide_comp, coincide_pres,
                config.modelo, r.tokens_in, r.tokens_out, r.cache_read,
                r.cache_write, r.costo_usd, datetime.now(),
            ),
        )
        _registrar_costo(cur, r)
    conn.commit()


# --------------------------------------------------------------------------
# MODO PRODUCCION — escribe la clasificación en la fila origen
# --------------------------------------------------------------------------
def aplicar_produccion(tabla: str, fila: dict, r: Resultado) -> None:
    """`estado_gestor` queda NULL (pendiente de confirmación humana) salvo
    descarte automático de alta confianza si está habilitado."""
    estado_gestor = None
    if config.auto_aplicar_descartes and r.interes == 0 and r.confianza >= 0.9:
        estado_gestor = 0

    ahora = datetime.now()
    conn = conexion_worker()
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE `{tabla}` SET pactivo=%s, composicion=%s, presentacion=%s, "
            f"nombre_clasificador=%s, fecha_clasificacion=%s, estado_gestor=%s "
            f"WHERE id=%s",
            (r.pactivo, r.composicion, r.presentacion, CLASIFICADOR,
             ahora, estado_gestor, fila["id"]),
        )
        cur.execute(
            "INSERT INTO clasificador_ia_log "
            "(tabla_origen, fila_id, descripcion, interes_sugerido, "
            " pactivo_sugerido, composicion_sugerida, presentacion_sugerida, "
            " metodo, confianza, razon, pactivo_nuevo, modelo, prompt_version, "
            " tokens_in, tokens_out, cache_read_tok, cache_write_tok, costo_usd, "
            " creado_en) "
            "VALUES (" + ",".join(["%s"] * 19) + ")",
            (tabla, fila["id"], (fila.get("Descripcion") or "")[:1000],
             r.interes, r.pactivo, r.composicion, r.presentacion, r.metodo,
             r.confianza, r.razon, r.pactivo_propuesto, config.modelo, PROMPT_VERSION,
             r.tokens_in, r.tokens_out, r.cache_read, r.cache_write, r.costo_usd, ahora),
        )
        _registrar_costo(cur, r)
    conn.commit()
