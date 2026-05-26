"""Layout y CSS compartidos por las vistas HTML del panel.

Se separa de main.py para que api/legacy.py reuse el mismo header/CSS sin
imports circulares.
"""

from __future__ import annotations

import html


CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background: #eef1f4;
       color: #1d2330; line-height: 1.5; }
header { background: #16263d; color: #fff; padding: 14px 28px; display: flex;
         align-items: center; justify-content: space-between; flex-wrap: wrap;
         gap: 12px; }
header b { font-size: 18px; }
header nav { display: flex; align-items: center; flex-wrap: wrap; }
header nav a { color: #b9c6d6; text-decoration: none; margin-left: 20px; font-size: 14px; }
header nav a:hover { color: #fff; }
header .usuario { display: inline-flex; align-items: center; gap: 8px;
                  margin-left: 22px; padding: 5px 10px;
                  background: rgba(255,255,255,.08); border-radius: 999px;
                  font-size: 13px; color: #d6e0ec; }
header .usuario .avatar { width: 22px; height: 22px; border-radius: 50%;
                          background: linear-gradient(135deg,#6cf,#7df0a8);
                          color: #07101f; font-weight: 700; font-size: 11px;
                          display: grid; place-items: center; }
header .usuario a { margin-left: 4px; color: #93a4c0; font-size: 12px;
                    text-decoration: none; }
header .usuario a:hover { color: #fff; }
main { max-width: 1120px; margin: 24px auto; padding: 0 20px; }
h1 { font-size: 20px; margin-bottom: 14px; }
h2 { font-size: 15px; color: #6b7689; margin: 22px 0 10px; }
.cards { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 22px; }
.card { background: #fff; border-radius: 10px; padding: 16px 20px; flex: 1; min-width: 140px;
        box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.card .n { font-size: 26px; font-weight: 700; }
.card .l { font-size: 12px; color: #6b7689; margin-top: 2px; }
table { width: 100%; background: #fff; border-radius: 10px; overflow: hidden;
        box-shadow: 0 1px 3px rgba(0,0,0,.08); border-collapse: collapse; }
th, td { text-align: left; padding: 9px 13px; font-size: 14px; }
th { background: #f3f6fa; color: #6b7689; }
tr + tr td { border-top: 1px solid #eef1f4; }
.fila { background: #fff; border-radius: 9px; padding: 13px 16px; margin-bottom: 10px;
        box-shadow: 0 1px 3px rgba(0,0,0,.08); border-left: 4px solid #2f6fb0; }
.fila.t-descarte { border-left-color: #c0392b; }
.fila.t-nuevo { border-left-color: #d68910; background: #fffaf2; }
.fila .meta { font-size: 12px; color: #6b7689; }
.fila .desc { font-size: 14px; margin: 4px 0 4px; }
.fila .razon { font-size: 12px; color: #6b7689; font-style: italic; margin-bottom: 8px; }
.fila .nuevo-aviso { background: #fff3e0; border: 1px solid #f0d9a8; border-radius: 6px;
        padding: 8px 11px; font-size: 13px; margin-bottom: 9px; }
.linea { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.linea label { font-size: 12px; color: #6b7689; }
.linea input, .linea select { padding: 6px 8px; border: 1px solid #cdd5e0; border-radius: 6px;
        font-size: 13px; }
.linea input.motivo { flex: 1; min-width: 200px; }
.linea select.decision { font-weight: 600; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 12px;
         font-weight: 600; }
.b-alta { background: #d9f0e1; color: #1b6b3a; }
.b-baja { background: #fbe4cf; color: #9a6212; }
.b-int { background: #d9f0e1; color: #1b6b3a; }
.b-desc { background: #f8d7da; color: #9a2530; }
.b-nuevo { background: #fde2c0; color: #9a6212; }
.b-hist { background: #d6e4f5; color: #1a4a7a; }
.b-met { background: #e6e9ef; color: #3a4252; }
.b-ent-d { background: #f3d6d6; color: #7a2530; border: 1px dashed #c98; }
.b-ent-i { background: #d6ecd9; color: #1b5e2a; border: 1px dashed #9b9; }
button { padding: 9px 18px; border: 0; border-radius: 6px; font-size: 14px;
         font-weight: 600; cursor: pointer; background: #1b6b3a; color: #fff; }
button.sec { background: #16263d; }
button.peligro { background: #c0392b; }
button:disabled { background: #6c757d; color: #e0e0e0; cursor: not-allowed; }
.barra { display: flex; gap: 12px; align-items: center; margin: 16px 0; flex-wrap: wrap; }
.barra input, .barra select { padding: 8px 10px; border: 1px solid #cdd5e0; border-radius: 6px; }
.filtros { display: flex; gap: 10px; align-items: center; margin-bottom: 14px; flex-wrap: wrap;
        font-size: 13px; }
.filtros a { text-decoration: none; color: #2f6fb0; padding: 4px 10px; border-radius: 6px;
        border: 1px solid #cdd5e0; background: #fff; }
.filtros a.on { background: #16263d; color: #fff; border-color: #16263d; }
.pag a { margin-right: 12px; text-decoration: none; color: #2f6fb0; font-size: 14px; }
.aviso { background: #fff7e6; border: 1px solid #f0d9a8; padding: 10px 14px;
         border-radius: 8px; margin-bottom: 14px; font-size: 14px; }
.vacio { background: #fff; border-radius: 10px; padding: 40px; text-align: center;
         color: #6b7689; }
form.alta { background: #fff; border-radius: 10px; padding: 16px 20px; margin-bottom: 18px;
            box-shadow: 0 1px 3px rgba(0,0,0,.08); display: flex; gap: 8px; flex-wrap: wrap; }
form.alta input, form.alta select, form.alta textarea { padding: 8px 10px;
        border: 1px solid #cdd5e0; border-radius: 6px; font-size: 13px; }
form.alta textarea { flex: 1; min-width: 320px; }
.consola { background: #1d2330; color: #f0f0f0; padding: 14px; border-radius: 8px;
           height: 320px; overflow-y: auto; font-family: 'SF Mono', Menlo, Consolas, monospace;
           font-size: 13px; white-space: pre-wrap; word-wrap: break-word; }
.modulo-card { background: #fff; border-radius: 10px; padding: 18px 22px; text-decoration: none;
               color: inherit; box-shadow: 0 1px 3px rgba(0,0,0,.08); display: block;
               transition: transform .1s, box-shadow .1s; }
.modulo-card:hover { transform: translateY(-2px); box-shadow: 0 4px 10px rgba(0,0,0,.10); }
.modulo-card .titulo { font-size: 17px; font-weight: 700; margin-bottom: 6px; }
.modulo-card .desc { font-size: 13px; color: #6b7689; }

/* /revision — patrón "todas tildadas por defecto, destildá las dudosas" */
.fila { cursor: pointer; transition: opacity .15s, background .15s; }
.fila-head { display: flex; align-items: center; gap: 12px; }
.fila input.marcar { transform: scale(1.5); accent-color: #1b6b3a; cursor: pointer;
                     flex-shrink: 0; }
.fila.skip { opacity: 0.45; background: #f5f5f5; border-left-color: #95a5b8 !important; }
.fila.skip .desc { text-decoration: line-through; color: #6b7689; }
.fila.skip:hover { opacity: 0.7; }
/* la línea de edición no togglea el card cuando se interactúa con ella */
.fila .linea-edicion { cursor: auto; }
.fila .linea-edicion[hidden] { display: none; }
#cuenta { background: rgba(255,255,255,.22); padding: 1px 8px; border-radius: 4px;
          margin: 0 2px; font-weight: 700; }

/* Vista de auditoría — filas ya revisadas, modo lectura */
.fila.revisada { cursor: default; opacity: 0.95; background: #fafbfc;
                 border-left-color: #95a5b8; }
.fila.revisada:hover { opacity: 1; }
.ts { font-size: 11px; color: #95a5b8; margin-top: 3px; }
.veredicto { margin-top: 6px; font-size: 13px; color: #3a4252; }
input[type=date], input[type=text] { padding: 4px 8px; border: 1px solid #cdd5e0;
                   border-radius: 6px; font-size: 13px; }

/* Encabezado de SUPERGRUPO en la cola de revisión — separa visualmente bloques
   afines (INTERÉS · Cruce histórico vs INTERÉS · Claude vs DESCARTE · Rubro)
   para que el revisor procese por lotes claros. */
.grupo-hdr { margin: 18px 0 8px; padding: 10px 14px; background: #1d2330; color: #fff;
             border-radius: 8px; font-size: 14px; font-weight: 600;
             letter-spacing: 0.2px; }
.grupo-hdr .grupo-n { color: #b9c6d6; font-weight: 400; font-size: 13px; margin-left: 8px; }

/* /comparacion — cross matrix de humano vs IA */
.matriz { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
          margin: 12px 0 24px; }
.mcell { display: block; padding: 16px 14px; border-radius: 10px; text-decoration: none;
         color: inherit; box-shadow: 0 1px 3px rgba(0,0,0,.08);
         transition: transform .08s, box-shadow .08s; }
.mcell:hover { transform: translateY(-2px); box-shadow: 0 4px 10px rgba(0,0,0,.12); }
.mn { font-size: 22px; font-weight: 700; }
.ml { font-size: 12px; color: #6b7689; margin-top: 2px; }
.m-ok   { background: #d9f0e1; }
.m-fp   { background: #f8d7da; }   /* IA dijo interés, humano no — ruido */
.m-fn   { background: #fde2c0; }   /* IA dijo descarte, humano sí — perdimos venta */
.m-warn { background: #fff3e0; }   /* pactivo nuevo, revisar */
.lineh { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; font-size: 13px;
         margin-top: 8px; padding-top: 8px; border-top: 1px dashed #e6e9ef; }
"""

# Items del nav. Si se agrega una sección al panel, se suma acá.
NAV = (
    ("Resumen", "/"),
    ("Backtest", "/comparacion"),
    ("Cola de revisión", "/revision"),
    ("Reglas", "/reglas"),
    ("Legacy", "/legacy"),
)


def escape(v) -> str:
    return html.escape("" if v is None else str(v))


def _iniciales(nombre: str) -> str:
    partes = [p for p in (nombre or "").split() if p]
    if not partes:
        return "?"
    if len(partes) == 1:
        return partes[0][:2].upper()
    return (partes[0][:1] + partes[-1][:1]).upper()


def layout(titulo: str, cuerpo: str, usuario: dict | None = None) -> str:
    nav = "".join(f"<a href='{h}'>{escape(t)}</a>" for t, h in NAV)
    if usuario and usuario.get("name"):
        chip = (
            f"<span class='usuario'>"
            f"<span class='avatar'>{escape(_iniciales(usuario['name']))}</span>"
            f"<span>{escape(usuario['name'])}</span>"
            f"<a href='/logout' title='Cerrar sesión'>salir</a>"
            f"</span>"
        )
    else:
        chip = ""
    return (
        "<!doctype html><html lang=es><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{escape(titulo)} · IA Bot</title><style>{CSS}</style></head><body>"
        f"<header><b>IA Bot · Pharmatender</b>"
        f"<nav>{nav}{chip}</nav></header>"
        f"<main>{cuerpo}</main></body></html>"
    )
