#!/usr/bin/env python3
"""Crea las tablas de auditoría (clasificador_ia_log, clasificador_ia_backtest)
en la base licitaciones_diarias_total_farma del servidor clásico."""

from __future__ import annotations

from pathlib import Path

from db import conectar

SQL = (Path(__file__).resolve().parent / "schema" / "auditoria.sql").read_text(encoding="utf-8")


def main() -> None:
    # quitar comentarios de línea y separar por ';'
    limpio = "\n".join(l for l in SQL.splitlines() if not l.strip().startswith("--"))
    sentencias = [s.strip() for s in limpio.split(";") if s.strip()]
    conn = conectar()
    try:
        with conn.cursor() as cur:
            for s in sentencias:
                cur.execute(s)
                print(f"OK  {s.splitlines()[0][:64]}")
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE 'clasificador_ia%'")
            print("Tablas presentes:", [list(r.values())[0] for r in cur.fetchall()])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
