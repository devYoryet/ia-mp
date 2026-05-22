#!/usr/bin/env python3
"""Muestra el resultado del backtest: acierto IA vs humano, costo y por método."""

from __future__ import annotations

from db import conectar

LINE = "-" * 78


def main() -> None:
    conn = conectar()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM clasificador_ia_backtest")
        total = cur.fetchone()["n"]
        print(f"\nFilas en backtest: {total}")
        if not total:
            print("(vacío — corre el worker en MODO=test)")
            conn.close()
            return

        cur.execute("""
            SELECT COUNT(*) n,
                   SUM(coincide_interes) ci,
                   SUM(coincide_pactivo) cp, COUNT(coincide_pactivo) ncp,
                   SUM(coincide_composicion) cc, COUNT(coincide_composicion) ncc,
                   SUM(coincide_presentacion) cpr, COUNT(coincide_presentacion) ncpr,
                   SUM(costo_usd) costo, AVG(costo_usd) prom,
                   SUM(ia_pactivo_nuevo IS NOT NULL) nuevos
            FROM clasificador_ia_backtest""")
        r = cur.fetchone()

        def pct(ok, tot):
            return f"{(ok or 0) / tot * 100:.1f}%" if tot else "—"

        print(LINE)
        print(f"  Acierto INTERÉS      : {r['ci']}/{r['n']}  ({pct(r['ci'], r['n'])})")
        print(f"  Acierto PACTIVO      : {r['cp']}/{r['ncp']}  ({pct(r['cp'], r['ncp'])})"
              f"   (sobre filas de interés)")
        print(f"  Acierto COMPOSICIÓN  : {r['cc']}/{r['ncc']}  ({pct(r['cc'], r['ncc'])})")
        print(f"  Acierto PRESENTACIÓN : {r['cpr']}/{r['ncpr']}  ({pct(r['cpr'], r['ncpr'])})")
        print(f"  Costo total          : ${float(r['costo'] or 0):.4f}   "
              f"(promedio ${float(r['prom'] or 0):.5f}/fila)")
        print(f"  Pactivos nuevos detectados: {r['nuevos']}")
        print(LINE)

        print("  Por método (cuánto resuelve cada etapa, y a qué costo):")
        cur.execute("""
            SELECT ia_metodo, COUNT(*) n, SUM(coincide_interes) ci,
                   SUM(coincide_pactivo) cp, COUNT(coincide_pactivo) ncp,
                   SUM(coincide_composicion) cc, SUM(coincide_presentacion) cpr,
                   SUM(costo_usd) costo
            FROM clasificador_ia_backtest GROUP BY ia_metodo ORDER BY n DESC""")
        for m in cur.fetchall():
            print(f"    {str(m['ia_metodo']):<18} n={m['n']:<4} "
                  f"interés={m['ci']}/{m['n']:<5} "
                  f"pactivo={m['cp']}/{m['ncp']:<4} "
                  f"comp={m['cc'] or 0}/{m['ncp']:<4} "
                  f"pres={m['cpr'] or 0}/{m['ncp']:<4} "
                  f"${float(m['costo'] or 0):.4f}")
        print(LINE)

        print("  Muestra (H=humano, IA=clasificador):")
        cur.execute("""
            SELECT tabla_origen tabla, fila_id, LEFT(descripcion,40) d,
                   humano_estado_gestor he, humano_pactivo hp,
                   ia_interes ii, ia_pactivo ip, ia_metodo met,
                   ia_confianza conf, coincide_interes cint, coincide_pactivo cpac
            FROM clasificador_ia_backtest ORDER BY id DESC LIMIT 25""")
        for s in cur.fetchall():
            ok_i = "✓" if s["cint"] == 1 else "✗"
            ok_p = "·" if s["cpac"] is None else ("✓" if s["cpac"] == 1 else "✗")
            print(f"    [{ok_i}{ok_p}] {s['tabla'][:4]}#{s['fila_id']}  {s['d']}")
            print(f"         H: estado={s['he']} pactivo={s['hp']}")
            print(f"         IA: interés={s['ii']} pactivo={s['ip']} "
                  f"[{s['met']}, conf={s['conf']}]")
    conn.close()
    print()


if __name__ == "__main__":
    main()
