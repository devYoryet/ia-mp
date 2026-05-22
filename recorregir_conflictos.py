"""Re-corrección puntual: aplica el VETO del modelo de descarte a las filas que
la etapa de reglas YA clasificó antes de que el veto existiera en la cascada.

Contexto: `regla_diccionario` hace un match de texto contra un catálogo que
incluye pactivos NO médicos ("Servicio de Aseo", "Cocina", "Adjunto"). Una fila
que salió interés por ese match SIMPLE de diccionario (no combinado) pero que el
clasificador de descarte entrenado descarta con probabilidad >= UMBRAL es un
falso positivo: la regla matcheó ruido. La cascada corregida la resuelve hoy con
método `conflicto_regla_modelo`; este script reescribe las filas ya guardadas
para dejarlas igual.

Toca dos tablas (ambas en config.db_name):
  - clasificador_ia_log  — auditoría que lee el panel.
  - la tabla origen      — compra_agil / Licitaciones_diarias: limpia
    pactivo/composicion/presentacion. `estado_gestor` se deja COMO ESTÁ
    (NULL = pendiente de revisión humana) — la ventana es supervisada, no se
    auto-descarta nada sin que una persona lo confirme.

Idempotente: tras aplicarlo, ninguna fila vuelve a calzar (cambia el método).
Dry-run por defecto; escribe solo con `--aplicar`.
"""

from __future__ import annotations

import sys

import db
import descarte_modelo
from config import config


def main(aplicar: bool) -> None:
    modelo = descarte_modelo.cargar_modelo_descarte()
    if modelo is None:
        sys.exit("modelo_descarte.joblib no encontrado — abortado.")

    conn = db.conectar()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, tabla_origen, fila_id, descripcion, pactivo_sugerido, razon "
            "FROM clasificador_ia_log "
            "WHERE metodo='regla_diccionario' AND interes_sugerido=1"
        )
        filas = cur.fetchall()

    # El veto NO se aplica al match COMBINADO (señal fuerte). Esa vía deja la
    # marca '(combinado)' en la razón; el resto es match simple de diccionario.
    dicc = [f for f in filas if "(combinado)" not in (f["razon"] or "")]

    conflictos = []
    for f in dicc:
        p = descarte_modelo.prob_descarte(modelo, f["descripcion"])
        if p >= config.umbral_modelo_descarte:
            conflictos.append((f, round(p, 3)))

    por_tabla: dict[str, int] = {}
    for f, _ in conflictos:
        por_tabla[f["tabla_origen"]] = por_tabla.get(f["tabla_origen"], 0) + 1

    print(f"regla_diccionario interes=1 : {len(filas)}")
    print(f"  vía match de diccionario  : {len(dicc)}")
    print(f"  CONFLICTO (p_desc>={config.umbral_modelo_descarte}) : {len(conflictos)}")
    for t, n in sorted(por_tabla.items()):
        print(f"    {t}: {n}")

    if not conflictos:
        print("Nada que corregir.")
        conn.close()
        return

    if not aplicar:
        print("\n[DRY-RUN] no se escribió nada. Volvé a correr con --aplicar.")
        conn.close()
        return

    actualizadas = 0
    with conn.cursor() as cur:
        for f, p in conflictos:
            razon = (
                f"La regla matcheó '{f['pactivo_sugerido']}', pero el "
                f"clasificador de descarte entrenado lo descarta "
                f"(probabilidad {p:.2f})."
            )
            cur.execute(
                "UPDATE clasificador_ia_log SET interes_sugerido=0, "
                "pactivo_sugerido=NULL, composicion_sugerida=NULL, "
                "presentacion_sugerida=NULL, metodo='conflicto_regla_modelo', "
                "confianza=%s, razon=%s WHERE id=%s",
                (p, razon, f["id"]),
            )
            # Tabla origen: limpia el pactivo erróneo. estado_gestor intacto.
            cur.execute(
                f"UPDATE `{f['tabla_origen']}` SET pactivo=NULL, "
                f"composicion=NULL, presentacion=NULL WHERE id=%s",
                (f["fila_id"],),
            )
            actualizadas += 1
    conn.commit()
    conn.close()
    print(f"\n[APLICADO] {actualizadas} filas reclasificadas a "
          f"'conflicto_regla_modelo' (descarte).")


if __name__ == "__main__":
    main(aplicar="--aplicar" in sys.argv)
