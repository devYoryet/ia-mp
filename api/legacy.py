"""Módulos Legacy portados desde gestor_oc (Laravel) a FastAPI.

Cuatro flujos que comparten la misma forma:
    1. el usuario sube un Excel/CSV por chunks (POST /legacy/<slug>/upload-chunk)
    2. al recibir el último chunk se lanza un script Python en background
       (bin/<script>.py) que escribe a un .log compartido
    3. el navegador polea GET /legacy/<slug>/log cada 500 ms para ver el progreso
    4. botones: detener (kill por PID) y descargar log (.txt)
    Adjudicaciones tiene además "descargar reporte" (último reporte_*.xlsx).

Para que el panel reuse la misma carpeta que el Laravel original (los scripts
del host escriben/leen ahí desde hace años), montamos
/var/www/html/gestor_oc/storage/app/temp dentro del container.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from api.auth import usuario_actual
from api.ui import escape, layout

try:
    from db import conectar
except Exception:  # noqa: BLE001
    conectar = None  # el panel de meses degrada a "sin datos" si no hay BD


router = APIRouter(prefix="/legacy", tags=["legacy"])


# Carpeta compartida con los scripts del host. La default es donde Laravel
# escribe hoy en gestor_oc; en local se puede sobreescribir con LEGACY_TEMP_DIR.
TEMP_DIR = Path(os.getenv("LEGACY_TEMP_DIR", "/host/storage/temp"))
# Intentar crear el directorio al cargar, pero NO romper el import si no se
# puede: si el volumen no está montado o falta el permiso, mejor que la vista
# Legacy muestre el error en runtime que tirar todo el panel al suelo.
try:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

# bin/ del repo: viajan dentro del container.
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"


@dataclass(frozen=True)
class Modulo:
    slug: str
    titulo: str
    descripcion: str
    script: str  # nombre del archivo en bin/
    log: str  # nombre del log dentro de TEMP_DIR
    accept: str  # accept del <input type=file>
    # construye los args del script. Recibe el path absoluto del archivo final
    # y el nombre original, devuelve la lista de args (sin python ni script).
    args: callable
    # tokens que, al aparecer en el log, dan por finalizado el proceso.
    finalizadores: tuple
    # icono FA equivalent en emoji (se evita Font Awesome para no traerlo).
    emoji: str
    # ¿genera reporte XLSX descargable?
    tiene_reporte: bool = False


def _args_subida_td(path: Path, nombre: str) -> list[str]:
    return [str(path)]


def _args_importaciones(path: Path, nombre: str) -> list[str]:
    # ImportOC.py extrae año/mes del nombre del archivo, igual que el controller.
    nums = re.findall(r"\d+", nombre)
    if len(nums) >= 2:
        year = nums[0]
        if len(year) == 2:
            year = "20" + year
        month = nums[1].zfill(2)
        fecha = f"{year}-{month}-01"
    else:
        fecha = datetime.now().strftime("%Y-%m-01")
    return ["--fecha", fecha, "--archivo", str(path)]


def _args_adjudicaciones(path: Path, nombre: str) -> list[str]:
    return [str(path)]


def _args_item_detalle(path: Path, nombre: str) -> list[str]:
    # ScriptCSV.py usa YYYYMM como nombre de tabla.
    nums = re.findall(r"\d+", nombre)
    if len(nums) >= 2:
        tabla = nums[0] + nums[1].zfill(2)
    else:
        tabla = datetime.now().strftime("%Y%m")
    return ["--excel", str(path), "--tabla", tabla, "--server", "clasico"]


def _args_cenabast(path: Path, nombre: str) -> list[str]:
    return [str(path)]


MODULOS: dict[str, Modulo] = {
    "subida-td": Modulo(
        slug="subida-td",
        titulo="Subida Tabla Dinámica",
        descripcion="Carga del Excel de Tabla Dinámica para actualizar Clásico y Prime.",
        script="base_para_sql.py",
        log="subida_td.log",
        accept=".xlsx,.xls",
        args=_args_subida_td,
        finalizadores=("FINALIZADO EXITOSAMENTE", "ERROR CRÍTICO", "Verificacion correcta"),
        emoji="📊",
    ),
    "importaciones": Modulo(
        slug="importaciones",
        titulo="Importaciones",
        descripcion="Carga del Excel mensual de importaciones (ImportOC).",
        script="ImportOC.py",
        log="importaciones.log",
        accept=".xlsm,.xlsx,.xls",
        args=_args_importaciones,
        finalizadores=("FIN", "ERROR CRÍTICO"),
        emoji="📅",
    ),
    "adjudicaciones": Modulo(
        slug="adjudicaciones",
        titulo="Adjudicaciones",
        descripcion="Carga del Excel de adjudicaciones; genera reporte de integridad.",
        script="estructura_adj.py",
        log="adjudicaciones_master.log",
        accept=".xlsx,.xls",
        args=_args_adjudicaciones,
        finalizadores=("[OK] REPORTE CREADO:", "ERROR CRÍTICO", "FIN"),
        emoji="✍️",
        tiene_reporte=True,
    ),
    "item-detalle": Modulo(
        slug="item-detalle",
        titulo="Item Detalle",
        descripcion="Carga del CSV de Item Detalle (licitaciones adjudicadas).",
        script="ScriptCSV.py",
        log="import_oc_csv.log",
        accept=".csv",
        args=_args_item_detalle,
        finalizadores=("TERMINADA CON EXITO", "ERROR CRÍTICO", "FIN"),
        emoji="📄",
    ),
    "cenabast": Modulo(
        slug="cenabast",
        titulo="Subida Cenabast",
        descripcion="Carga del Excel de Cenabast en base (Clásico y Prime) y registra el último mes en Fecha.",
        script="cenabast_para_sql.py",
        log="cenabast.log",
        accept=".xlsx,.xls",
        args=_args_cenabast,
        finalizadores=("FINALIZADO EXITOSAMENTE", "ERROR CRÍTICO"),
        emoji="🏥",
    ),
}


def _mod(slug: str) -> Modulo:
    m = MODULOS.get(slug)
    if not m:
        raise HTTPException(404, f"Módulo desconocido: {slug}")
    return m


def _pid_file(slug: str) -> Path:
    return TEMP_DIR / f".{slug}.pid"


def _log_file(slug: str) -> Path:
    return TEMP_DIR / _mod(slug).log


def _proceso_vivo(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _matar(slug: str) -> None:
    """Mata el proceso del módulo si está vivo, junto con todo su grupo."""
    pf = _pid_file(slug)
    if not pf.exists():
        return
    try:
        pid = int(pf.read_text().strip())
    except (ValueError, OSError):
        pf.unlink(missing_ok=True)
        return
    try:
        # El proceso se lanza con start_new_session=True → mata todo el grupo.
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    pf.unlink(missing_ok=True)


def _limpiar_temp(conservar_reportes: bool = True) -> None:
    """Borra Excel/CSV subidos en temp; conserva reporte_* y los .log."""
    for f in TEMP_DIR.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in (".xlsx", ".xls", ".csv", ".xlsm"):
            continue
        if conservar_reportes and f.name.startswith("reporte_"):
            continue
        try:
            f.unlink()
        except OSError:
            pass


def _lanzar(mod: Modulo, archivo: Path, nombre_original: str) -> int:
    """Lanza el script en background. Devuelve el PID."""
    log_path = _log_file(mod.slug)
    log_path.write_text(
        f"[{datetime.now().isoformat(timespec='seconds')}] "
        f"Archivo recibido: {nombre_original}\n"
        f"Iniciando {mod.script}...\n"
    )
    args = [sys.executable, "-u", str(BIN_DIR / mod.script), *mod.args(archivo, nombre_original)]
    # Redirige stdout+stderr al log y aísla el process group para poder matar
    # con killpg sin tocar el panel.
    fh = open(log_path, "a", buffering=1)
    proc = subprocess.Popen(  # noqa: S603
        args,
        stdout=fh,
        stderr=subprocess.STDOUT,
        cwd=str(BIN_DIR),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        start_new_session=True,
    )
    _pid_file(mod.slug).write_text(str(proc.pid))
    return proc.pid


# ================================================ ITEM DETALLE · MESES ===
# Panel de estado: que meses ya estan cargados en oc_items_segmentado (tabla
# YYYYMM en clasico) y disparador de la descarga+carga automatica.

ITEM_DB = "oc_items_segmentado"
AUTO_SCRIPT = Path(__file__).resolve().parent.parent / "auto_item_detalle.py"

MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
    7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre",
    12: "Diciembre",
}

# Cache corto del panel de meses: COUNT(*) sobre tablas grandes no debe correr
# en cada poll del log.
_MESES_CACHE: dict = {"ts": 0.0, "data": None, "n": 0}
_MESES_TTL = 60.0


def _meses_recientes(n: int) -> list[tuple[int, int]]:
    """(year, month) de los ultimos n meses, desde el actual hacia atras."""
    hoy = datetime.now()
    y, m = hoy.year, hoy.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y -= 1
            m = 12
    return out


def _estado_meses(n: int = 6) -> list[dict]:
    """Para cada uno de los ultimos n meses: si la tabla YYYYMM existe en clasico
    y cuantas filas tiene; mas si hay marcador .done del flujo automatico."""
    ahora = time.time()
    if (_MESES_CACHE["data"] is not None and _MESES_CACHE["n"] == n
            and ahora - _MESES_CACHE["ts"] < _MESES_TTL):
        return _MESES_CACHE["data"]

    meses = _meses_recientes(n)
    objetivo = [f"{y}{m:02d}" for (y, m) in meses]
    counts: dict[str, object] = {}
    if conectar is not None:
        try:
            conn = conectar(ITEM_DB)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT TABLE_NAME FROM information_schema.tables "
                        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME REGEXP '^[0-9]{6}$'",
                        (ITEM_DB,),
                    )
                    existentes = {list(r.values())[0] for r in cur.fetchall()}
                    for yyyymm in objetivo:
                        if yyyymm in existentes:
                            cur.execute(f"SELECT COUNT(*) AS c FROM `{yyyymm}`")
                            counts[yyyymm] = cur.fetchone()["c"]
                        else:
                            counts[yyyymm] = None
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            counts = {k: "?" for k in objetivo}

    filas = []
    for (y, m) in meses:
        yyyymm = f"{y}{m:02d}"
        c = counts.get(yyyymm)
        cargado = isinstance(c, int) and c > 0
        filas.append({
            "periodo": f"{MESES_ES[m]} {y}",
            "yyyymm": yyyymm,
            "ym_url": f"{y}-{m}",  # mes sin cero, como espera el script/blob
            "filas": c,
            "cargado": cargado,
            "marcador": (TEMP_DIR / f".item_detalle_{yyyymm}.done").exists(),
        })
    _MESES_CACHE.update(ts=ahora, data=filas, n=n)
    return filas


def _lanzar_auto(periodo: str | None) -> int:
    """Lanza auto_item_detalle.py en background; escribe al MISMO log que el
    modulo item-detalle para que la consola del panel muestre el progreso."""
    log_path = _log_file("item-detalle")
    args = [sys.executable, "-u", str(AUTO_SCRIPT)]
    if periodo:
        args += ["--periodo", periodo, "--force"]
    log_path.write_text(
        f"[{datetime.now().isoformat(timespec='seconds')}] "
        f"Disparo MANUAL de descarga+carga automatica"
        f"{' · periodo ' + periodo if periodo else ' · mes anterior'}\n"
    )
    fh = open(log_path, "a", buffering=1)
    proc = subprocess.Popen(  # noqa: S603
        args,
        stdout=fh,
        stderr=subprocess.STDOUT,
        cwd=str(AUTO_SCRIPT.parent),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        start_new_session=True,
    )
    _pid_file("item-detalle").write_text(str(proc.pid))
    # Invalida el cache de meses para que el panel refleje el cambio al terminar.
    _MESES_CACHE["ts"] = 0.0
    return proc.pid


# ============================================================== ÍNDICE ===

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def indice(request: Request) -> str:
    cards = "".join(
        f"<a class=modulo-card href='/legacy/{m.slug}'>"
        f"<div class=titulo><span class=emoji>{m.emoji}</span> {escape(m.titulo)}</div>"
        f"<div class=desc>{escape(m.descripcion)}</div></a>"
        for m in MODULOS.values()
    )
    cuerpo = (
        "<h1>Legacy · Procesos de carga</h1>"
        "<div class=aviso>Módulos portados desde la app Laravel <code>gestor_oc</code>. "
        "Cada módulo recibe un archivo por chunks y lanza el script Python correspondiente "
        f"en <code>bin/</code>. Carpeta de trabajo: <code>{escape(TEMP_DIR)}</code>.</div>"
        f"<div class=cards>{cards}</div>"
    )
    return layout("Legacy", cuerpo, usuario=usuario_actual(request))


# ============================================================== VISTA ====

def _vista_modulo(slug: str, usuario: dict | None = None) -> str:
    mod = _mod(slug)
    log_path = _log_file(slug)
    log_inicial = ""
    if log_path.exists():
        try:
            log_inicial = log_path.read_text(errors="replace")
        except OSError:
            pass

    reporte_btn = ""
    if mod.tiene_reporte:
        reporte_btn = (
            f"<a id='btn-reporte' href='/legacy/{mod.slug}/descargar-reporte' "
            "style='display:none;margin-right:8px' "
            "class='' onclick='event.stopPropagation()'>"
            "<button type=button class=sec>⬇ Descargar reporte (.xlsx)</button></a>"
        )

    finalizadores_js = ",".join(f"{f!r}" for f in mod.finalizadores)

    # Panel de meses + disparador automatico: solo para Item Detalle.
    panel_meses = ""
    subtitulo_subida = ""
    if slug == "item-detalle":
        filas_html = ""
        for it in _estado_meses(6):
            if it["cargado"]:
                estado = "<span class='st ok'>✓ cargado</span>"
                filas_txt = f"{it['filas']:,}".replace(",", ".")
                btn = "↻ Recargar"
            elif it["filas"] == "?":
                estado = "<span class='st warn'>sin BD</span>"
                filas_txt = "—"
                btn = "⬇ Cargar"
            else:
                estado = "<span class='st falta'>✗ falta</span>"
                filas_txt = "—"
                btn = "⬇ Cargar ahora"
            marca = " <span title='cargado por el proceso automatico'>🤖</span>" if it["marcador"] else ""
            filas_html += (
                f"<tr><td>{escape(it['periodo'])}{marca}</td>"
                f"<td><code>{it['yyyymm']}</code></td>"
                f"<td>{estado}</td>"
                f"<td style='text-align:right'>{filas_txt}</td>"
                f"<td><button type=button class=sec "
                f"onclick=\"cargarAuto('{it['ym_url']}')\">{btn}</button></td></tr>"
            )
        panel_meses = f"""
<style>
  table.meses {{ width:100%; border-collapse:collapse; margin:6px 0 4px; font-size:13.5px; }}
  table.meses th, table.meses td {{ padding:8px 10px; border-bottom:1px solid rgba(120,140,170,.18); text-align:left; }}
  table.meses th {{ font-size:11.5px; text-transform:uppercase; letter-spacing:.4px; color:#6b7689; }}
  .st {{ font-weight:600; font-size:12.5px; }}
  .st.ok {{ color:#1a9d5a; }}
  .st.falta {{ color:#c0392b; }}
  .st.warn {{ color:#b8860b; }}
</style>
<h2>📦 Meses de Item Detalle</h2>
<div class=aviso>Estado de la tabla mensual <code>YYYYMM</code> en <code>oc_items_segmentado</code> (clásico).
🤖 = cargado por el proceso automático. <b>Cargar</b> baja el ZIP del portal de transparencia,
lo descomprime, valida e importa a clásico + prime — todo automático; seguí el avance en la consola de abajo.
Se muestran los últimos 6 meses.</div>
<table class=meses>
<thead><tr><th>Periodo</th><th>Tabla</th><th>Estado</th><th>Filas</th><th>Acción</th></tr></thead>
<tbody>{filas_html}</tbody>
</table>
<div style='margin:8px 0 20px'>
  <button type=button class=sec onclick="cargarAuto('')">⬇ Cargar mes anterior automáticamente</button>
</div>
"""
        subtitulo_subida = (
            "<h2>📤 Subida manual (respaldo)</h2>"
            "<div class=aviso>Si el automático no encontró el archivo o querés forzar uno propio, "
            "subí el CSV a mano. Funciona igual que siempre.</div>"
        )

    cuerpo = f"""
<h1>{mod.emoji} {escape(mod.titulo)}</h1>
<div class=aviso>{escape(mod.descripcion)} &nbsp;·&nbsp; Script:
<code>bin/{mod.script}</code> &nbsp;·&nbsp; Log: <code>{escape(mod.log)}</code></div>
{panel_meses}
{subtitulo_subida}
<div class=cards>
  <div class=card style='flex-basis:100%'>
    <form id=formSubida onsubmit='return false'>
      <div style='margin-bottom:12px'>
        <label for=archivo style='display:block;margin-bottom:6px;font-size:13px;color:#6b7689'>
          Archivo ({escape(mod.accept)})
        </label>
        <input type=file id=archivo accept='{escape(mod.accept)}'>
      </div>
      <div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap'>
        <button type=submit id=btnEjecutar disabled>▶ Ejecutar</button>
        <button type=button id=btnDetener class=peligro>■ Detener</button>
        {reporte_btn}
        <a id='btn-log' href='/legacy/{mod.slug}/descargar-log' style='display:none'>
          <button type=button class=sec>⬇ Descargar log (.txt)</button></a>
      </div>
    </form>
  </div>
</div>

<h2>Consola de salida</h2>
<div id=consola class=consola>{escape(log_inicial) or 'Esperando ejecución...'}</div>

<script>
(function() {{
  const SLUG = '{mod.slug}';
  const BASE = '/legacy/' + SLUG;
  const FINALIZADORES = [{finalizadores_js}];
  const TIENE_REPORTE = {str(mod.tiene_reporte).lower()};

  const inputArchivo = document.getElementById('archivo');
  const btnEjecutar = document.getElementById('btnEjecutar');
  const btnDetener = document.getElementById('btnDetener');
  const formSubida = document.getElementById('formSubida');
  const consola = document.getElementById('consola');
  const btnLog = document.getElementById('btn-log');
  const btnReporte = document.getElementById('btn-reporte');

  let abortarSubida = false;
  let intervaloLog = null;

  inputArchivo.addEventListener('change', () => {{
    btnEjecutar.disabled = !(inputArchivo.files && inputArchivo.files.length);
  }});

  function setLog(texto) {{
    if (consola.innerText !== texto) {{
      const cerca = (consola.scrollHeight - consola.scrollTop - consola.clientHeight) < 150;
      consola.innerText = texto;
      if (cerca) consola.scrollTop = consola.scrollHeight;
    }}
  }}

  function esLogin(t) {{
    return t && (t.indexOf('login-card') >= 0 || t.indexOf('<title>Acceso') >= 0);
  }}

  window.cargarAuto = async function(periodo) {{
    const txt = periodo ? ('el periodo ' + periodo) : 'el mes anterior';
    if (!confirm('¿Descargar e importar ' + txt + ' automáticamente?\\n'
                 + 'Baja el ZIP del portal, valida e importa a clásico + prime.')) return;
    setLog('Disparando descarga+carga automática de ' + txt + '...');
    try {{
      const fd = new FormData();
      if (periodo) fd.append('periodo', periodo);
      const resp = await fetch(BASE + '/auto', {{method: 'POST', body: fd}});
      if (!resp.ok) {{ setLog('No se pudo iniciar (HTTP ' + resp.status + '). ¿Sesión expirada? Recargá con F5.'); return; }}
      iniciarSeguimiento();
    }} catch (e) {{ setLog('Error al iniciar: ' + e); }}
  }};

  function iniciarSeguimiento() {{
    if (intervaloLog) clearInterval(intervaloLog);
    intervaloLog = setInterval(() => {{
      fetch(BASE + '/log?t=' + Date.now())
        .then(r => r.text())
        .then(data => {{
          if (esLogin(data)) {{
            clearInterval(intervaloLog);
            setLog('⚠ Sesión expirada. Recargá la página (F5) e iniciá sesión para ver el progreso.');
            return;
          }}
          setLog(data);
          const upper = data.toUpperCase();
          if (FINALIZADORES.some(f => upper.includes(f.toUpperCase()))) {{
            clearInterval(intervaloLog);
            btnEjecutar.disabled = !(inputArchivo.files && inputArchivo.files.length);
            btnEjecutar.innerHTML = '▶ Ejecutar';
            btnLog.style.display = 'inline-block';
            if (TIENE_REPORTE && btnReporte) btnReporte.style.display = 'inline-block';
          }}
        }})
        .catch(() => {{}});
    }}, 500);
  }}

  async function subirPorChunks(archivo) {{
    const CHUNK = 20 * 1024 * 1024;
    const total = Math.ceil(archivo.size / CHUNK);
    const nombreUnico = Date.now() + '_' + archivo.name;

    for (let i = 0; i < total; i++) {{
      if (abortarSubida) return false;
      const inicio = i * CHUNK;
      const fin = Math.min(inicio + CHUNK, archivo.size);
      const fd = new FormData();
      fd.append('file_chunk', archivo.slice(inicio, fin));
      fd.append('chunk_index', i);
      fd.append('total_chunks', total);
      fd.append('filename', nombreUnico);
      const pct = Math.round((i / total) * 100);
      const barra = '█'.repeat(pct / 5) + '░'.repeat(20 - Math.floor(pct / 5));
      setLog('Subiendo ' + archivo.name + '\\n[' + barra + '] ' + pct + '%\\n'
             + 'Bloque ' + (i + 1) + ' de ' + total + '...');
      const resp = await fetch(BASE + '/upload-chunk', {{method: 'POST', body: fd}});
      if (!resp.ok) {{
        setLog('Error subiendo bloque ' + (i + 1) + ': HTTP ' + resp.status);
        return false;
      }}
    }}
    return true;
  }}

  formSubida.addEventListener('submit', async (e) => {{
    e.preventDefault();
    const archivo = inputArchivo.files[0];
    if (!archivo) return;
    btnEjecutar.disabled = true;
    btnEjecutar.innerHTML = '⏳ Subiendo...';
    btnLog.style.display = 'none';
    if (btnReporte) btnReporte.style.display = 'none';
    abortarSubida = false;
    const ok = await subirPorChunks(archivo);
    if (ok) {{
      setLog('Subida completa. Procesando...');
      iniciarSeguimiento();
    }} else {{
      btnEjecutar.disabled = false;
      btnEjecutar.innerHTML = '▶ Ejecutar';
    }}
  }});

  btnDetener.addEventListener('click', async () => {{
    if (!confirm('¿Detener el proceso?')) return;
    abortarSubida = true;
    btnDetener.disabled = true;
    btnDetener.innerHTML = '⏳ Deteniendo...';
    await fetch(BASE + '/detener', {{method: 'POST'}});
    if (intervaloLog) clearInterval(intervaloLog);
    btnEjecutar.disabled = !(inputArchivo.files && inputArchivo.files.length);
    btnEjecutar.innerHTML = '▶ Ejecutar';
    btnDetener.disabled = false;
    btnDetener.innerHTML = '■ Detener';
    btnLog.style.display = 'inline-block';
  }});

  // Si al cargar la página ya hay un proceso vivo, arrancar el seguimiento.
  fetch(BASE + '/log?t=' + Date.now())
    .then(r => r.text())
    .then(data => {{
      if (data && !esLogin(data) && !FINALIZADORES.some(f => data.toUpperCase().includes(f.toUpperCase()))) {{
        iniciarSeguimiento();
      }}
    }})
    .catch(() => {{}});
}})();
</script>
"""
    return layout(mod.titulo, cuerpo, usuario=usuario)


@router.post("/item-detalle/auto")
async def item_detalle_auto(request: Request, periodo: str = Form(default="")):
    """Dispara la descarga+carga automatica del mes anterior (o `periodo` YYYY-M)."""
    pf = _pid_file("item-detalle")
    if pf.exists():
        try:
            if _proceso_vivo(int(pf.read_text().strip())):
                raise HTTPException(409, "Ya hay un proceso de Item Detalle en curso.")
        except (ValueError, OSError):
            pass
    pid = _lanzar_auto(periodo.strip() or None)
    return {"ok": True, "pid": pid}


@router.get("/item-detalle/estado-meses")
def item_detalle_estado_meses(n: int = 6):
    return JSONResponse(_estado_meses(min(max(n, 1), 24)))


@router.get("/{slug}", response_class=HTMLResponse)
def vista(request: Request, slug: str) -> str:
    return _vista_modulo(slug, usuario=usuario_actual(request))


# =========================================================== ENDPOINTS ===

@router.post("/{slug}/upload-chunk")
async def upload_chunk(
    slug: str,
    file_chunk: UploadFile,
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    filename: str = Form(...),
):
    mod = _mod(slug)
    # Sanea el nombre: solo basename, sin path traversal.
    nombre = os.path.basename(filename)
    if not nombre or nombre in (".", ".."):
        raise HTTPException(400, "filename inválido")
    destino = TEMP_DIR / nombre

    # Primer chunk → trunca; resto → append.
    modo = "wb" if chunk_index == 0 else "ab"
    contenido = await file_chunk.read()
    with open(destino, modo) as fh:
        fh.write(contenido)

    if chunk_index == total_chunks - 1:
        os.chmod(destino, 0o666)
        # Limpia el prefijo `<timestamp>_` que pone el JS para evitar colisiones.
        partes = nombre.split("_", 1)
        if len(partes) == 2 and partes[0].isdigit():
            limpio = TEMP_DIR / partes[1]
            destino.rename(limpio)
            destino = limpio
        _lanzar(mod, destino, destino.name)
    return {"ok": True, "chunk": chunk_index, "total": total_chunks}


@router.get("/{slug}/log", response_class=PlainTextResponse)
def leer_log(slug: str) -> str:
    p = _log_file(slug)
    if p.exists():
        try:
            return p.read_text(errors="replace")
        except OSError as exc:
            return f"Error leyendo log: {exc}"
    return "Esperando inicio del proceso..."


@router.get("/{slug}/descargar-log")
def descargar_log(slug: str):
    mod = _mod(slug)
    p = _log_file(slug)
    if not p.exists():
        raise HTTPException(404, "Log no disponible")
    fname = f"log_{mod.slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    return FileResponse(p, media_type="text/plain", filename=fname)


@router.post("/{slug}/detener")
def detener(slug: str):
    _mod(slug)  # valida slug
    _matar(slug)
    _limpiar_temp()
    return {"ok": True, "estado": "detenido"}


@router.get("/{slug}/descargar-reporte")
def descargar_reporte(slug: str):
    mod = _mod(slug)
    if not mod.tiene_reporte:
        raise HTTPException(404, "Este módulo no genera reporte")
    reportes = sorted(
        TEMP_DIR.glob("reporte_*.xlsx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not reportes:
        raise HTTPException(404, "No hay reporte generado")
    return FileResponse(
        reportes[0],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=reportes[0].name,
    )


@router.get("/{slug}/estado")
def estado(slug: str):
    """Diagnóstico: ¿hay un proceso vivo para este módulo?"""
    _mod(slug)
    pf = _pid_file(slug)
    if not pf.exists():
        return {"vivo": False}
    try:
        pid = int(pf.read_text().strip())
    except (ValueError, OSError):
        return {"vivo": False}
    return {"vivo": _proceso_vivo(pid), "pid": pid}
