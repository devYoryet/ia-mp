"""Login del panel — autentica contra la tabla `users` del legacy.

Los usuarios y contraseñas viven en `licitaciones_diarias_total_farma.users`
(Laravel 5.4, bcrypt `$2y$10$`). Esa BD es la MISMA a la que ya se conecta el
panel (`config.db_name`), así que no se copian datos: se consulta directo. Si
en el legacy cambian la contraseña, el panel queda sincronizado solo.

Sesión = cookie firmada con `itsdangerous` vía `SessionMiddleware` de Starlette.
La firma usa `config.session_secret`. La cookie guarda el `id` y `name` del
usuario; al aprobar hojas, ese `name` reemplaza al input "revisor" anterior y
queda en `nombre_clasificador`.
"""

from __future__ import annotations

import bcrypt
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER

from db import conectar


router = APIRouter()


# ---------------------------------------------------------------- BD ---
def _buscar_usuario(email: str) -> dict | None:
    """Busca un usuario por email (excluye soft-deleted del legacy)."""
    email = (email or "").strip().lower()
    if not email:
        return None
    conn = conectar()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, email, password FROM users "
                "WHERE LOWER(email)=%s AND deleted_at IS NULL LIMIT 1",
                (email,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def _verificar_password(plano: str, hashed: str | None) -> bool:
    """Valida un hash bcrypt de Laravel (`$2y$10$...`). PHP usa el prefijo $2y$
    y Python espera $2b$: son funcionalmente idénticos, así que se cambia el
    prefijo antes de validar. Hashes inválidos (NULL, "null", longitud rara)
    fallan en silencio."""
    if not plano or not hashed:
        return False
    h = hashed.encode("utf-8", "ignore")
    if len(h) < 50:  # bcrypt válido tiene 60 chars; valores tipo "null" no
        return False
    if h.startswith(b"$2y$"):
        h = b"$2b$" + h[4:]
    try:
        return bcrypt.checkpw(plano.encode("utf-8"), h)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------- Sesión ---
def usuario_actual(request: Request) -> dict | None:
    """Devuelve {id, name, email} si la cookie de sesión es válida, o None."""
    if not hasattr(request, "session"):
        return None
    u = request.session.get("usuario")
    if not isinstance(u, dict) or "id" not in u or "name" not in u:
        return None
    return u


def cerrar_sesion(request: Request) -> None:
    if hasattr(request, "session"):
        request.session.pop("usuario", None)


# ----------------------------------------------------------- HTML ---
# Página de login standalone: no usa el layout del panel (cuyo header asume
# usuario logueado). Tema oscuro con motivo de "agentes" y un robotito SVG.
def _pagina_login(error: str = "", next_url: str = "/", email: str = "") -> str:
    e = (error or "").replace("<", "&lt;")
    nxt = (next_url or "/").replace('"', "")
    em = (email or "").replace('"', "")
    aviso = (
        f"<div class='login-error'>{e}</div>" if e else ""
    )
    return f"""<!doctype html>
<html lang=es><head>
<meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>Acceso · IA Bot · Pharmatender</title>
<style>{_CSS_LOGIN}</style>
</head><body>
<div class='bg-grid'></div>
<div class='bg-orbs'>
  <span class='orb o1'></span><span class='orb o2'></span><span class='orb o3'></span>
</div>

<main class='login-wrap'>
  <div class='login-card'>
    <div class='login-head'>
      <div class='bot-avatar' aria-hidden='true'>
        <svg viewBox='0 0 64 64' width='52' height='52'>
          <defs>
            <linearGradient id='g1' x1='0' x2='1' y1='0' y2='1'>
              <stop offset='0%' stop-color='#6cf'/>
              <stop offset='100%' stop-color='#7df0a8'/>
            </linearGradient>
          </defs>
          <line x1='32' y1='4' x2='32' y2='13' stroke='url(#g1)' stroke-width='2.5'/>
          <circle cx='32' cy='4' r='3' fill='url(#g1)'/>
          <rect x='10' y='15' width='44' height='34' rx='10' fill='none'
                stroke='url(#g1)' stroke-width='2.5'/>
          <circle cx='23' cy='32' r='4' fill='url(#g1)'>
            <animate attributeName='r' values='4;2.5;4' dur='2.4s' repeatCount='indefinite'/>
          </circle>
          <circle cx='41' cy='32' r='4' fill='url(#g1)'>
            <animate attributeName='r' values='4;2.5;4' dur='2.4s' begin='1.2s'
                     repeatCount='indefinite'/>
          </circle>
          <rect x='27' y='42' width='10' height='2.5' rx='1.2' fill='url(#g1)'/>
          <line x1='8' y1='27' x2='2' y2='27' stroke='url(#g1)' stroke-width='2.5'/>
          <line x1='8' y1='37' x2='2' y2='37' stroke='url(#g1)' stroke-width='2.5'/>
          <line x1='56' y1='27' x2='62' y2='27' stroke='url(#g1)' stroke-width='2.5'/>
          <line x1='56' y1='37' x2='62' y2='37' stroke='url(#g1)' stroke-width='2.5'/>
        </svg>
      </div>
      <div class='login-marca'>
        <div class='login-titulo'>IA Bot · Pharmatender</div>
        <div class='login-sub'>Panel de agentes de clasificación</div>
      </div>
    </div>

    <div class='agentes'>
      <span class='agente'><span class='dot'></span>cascada</span>
      <span class='agente'><span class='dot'></span>modelo pactivo</span>
      <span class='agente'><span class='dot'></span>Claude</span>
    </div>

    {aviso}

    <form method='post' action='/login' class='login-form' autocomplete='on'>
      <input type='hidden' name='next' value='{nxt}'>
      <label class='campo'>
        <span>Correo</span>
        <input type='email' name='email' value='{em}' required autofocus
               placeholder='nombre@pharmatender.cl'>
      </label>
      <label class='campo'>
        <span>Contraseña</span>
        <input type='password' name='password' required placeholder='••••••••'>
      </label>
      <button type='submit' class='login-btn'>
        <span>Iniciar sesión</span>
        <svg viewBox='0 0 24 24' width='18' height='18' aria-hidden='true'>
          <path d='M5 12h13M13 6l6 6-6 6' fill='none' stroke='currentColor'
                stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'/>
        </svg>
      </button>
    </form>

    <div class='login-pie'>
      <span class='pulso'></span>
      <span>Sistema activo · clasificación automatizada con Claude</span>
    </div>
  </div>
</main>
</body></html>"""


_CSS_LOGIN = """
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
  background: radial-gradient(1200px 700px at 70% 10%, #1d3557 0%, #0d1830 55%, #07101f 100%);
  color: #e6ecf5; overflow: hidden; position: relative;
}
.bg-grid {
  position: fixed; inset: 0; pointer-events: none; opacity: .18;
  background-image:
    linear-gradient(rgba(120,180,255,.15) 1px, transparent 1px),
    linear-gradient(90deg, rgba(120,180,255,.15) 1px, transparent 1px);
  background-size: 42px 42px;
  mask-image: radial-gradient(closest-side, #000 60%, transparent 100%);
}
.bg-orbs { position: fixed; inset: 0; pointer-events: none; overflow: hidden; }
.orb { position: absolute; border-radius: 50%; filter: blur(70px); opacity: .55; }
.orb.o1 { width: 380px; height: 380px;
  background: radial-gradient(circle, #4d8ff0 0%, transparent 70%);
  top: -80px; left: -60px; animation: flotar 14s ease-in-out infinite; }
.orb.o2 { width: 320px; height: 320px;
  background: radial-gradient(circle, #6ee7a8 0%, transparent 70%);
  bottom: -90px; right: -50px; animation: flotar 17s ease-in-out infinite reverse; }
.orb.o3 { width: 260px; height: 260px;
  background: radial-gradient(circle, #b380ff 0%, transparent 70%);
  top: 35%; left: 55%; animation: flotar 19s ease-in-out infinite; }
@keyframes flotar {
  0%,100% { transform: translate(0,0) scale(1); }
  50%     { transform: translate(20px,-30px) scale(1.08); }
}

.login-wrap {
  position: relative; z-index: 2;
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  padding: 24px;
}
.login-card {
  width: 100%; max-width: 420px;
  background: rgba(14, 22, 40, 0.66);
  backdrop-filter: blur(18px) saturate(1.2);
  -webkit-backdrop-filter: blur(18px) saturate(1.2);
  border: 1px solid rgba(120, 180, 255, .18);
  border-radius: 18px;
  padding: 32px 30px 22px;
  box-shadow: 0 20px 50px rgba(0,0,0,.45),
              inset 0 1px 0 rgba(255,255,255,.06);
  animation: aparecer .6s cubic-bezier(.2,.8,.2,1);
}
@keyframes aparecer {
  from { opacity: 0; transform: translateY(14px); }
  to   { opacity: 1; transform: translateY(0); }
}

.login-head { display: flex; align-items: center; gap: 14px; margin-bottom: 18px; }
.bot-avatar {
  width: 64px; height: 64px; flex-shrink: 0;
  border-radius: 14px; display: grid; place-items: center;
  background: linear-gradient(140deg, rgba(108,210,255,.18), rgba(125,240,168,.10));
  border: 1px solid rgba(108,210,255,.28);
  box-shadow: 0 0 24px rgba(108,210,255,.22), inset 0 0 16px rgba(108,210,255,.10);
}
.login-titulo { font-size: 18px; font-weight: 700; letter-spacing: .2px; color: #f3f8ff; }
.login-sub { font-size: 12.5px; color: #93a4c0; margin-top: 2px; }

.agentes {
  display: flex; gap: 8px; flex-wrap: wrap; margin: 6px 0 22px;
  font-size: 11.5px; color: #93a4c0;
}
.agente {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border-radius: 999px;
  background: rgba(255,255,255,.04);
  border: 1px solid rgba(120,180,255,.14);
}
.agente .dot {
  width: 6px; height: 6px; border-radius: 50%; background: #7df0a8;
  box-shadow: 0 0 8px #7df0a8;
  animation: parpadeo 1.6s ease-in-out infinite;
}
.agente:nth-child(2) .dot { animation-delay: .5s; background: #6cf;
                            box-shadow: 0 0 8px #6cf; }
.agente:nth-child(3) .dot { animation-delay: 1s; background: #b380ff;
                            box-shadow: 0 0 8px #b380ff; }
@keyframes parpadeo {
  0%, 100% { opacity: 1; transform: scale(1); }
  50%      { opacity: .35; transform: scale(.7); }
}

.login-error {
  background: rgba(244, 82, 89, .14);
  border: 1px solid rgba(244, 82, 89, .35);
  color: #ffb4b8;
  padding: 9px 12px; border-radius: 10px;
  font-size: 13px; margin-bottom: 14px;
}

.login-form { display: flex; flex-direction: column; gap: 12px; }
.campo { display: block; }
.campo span {
  display: block; font-size: 11.5px; color: #93a4c0;
  letter-spacing: .5px; text-transform: uppercase;
  margin-bottom: 6px;
}
.campo input {
  width: 100%; padding: 11px 13px;
  background: rgba(8, 14, 28, .72);
  border: 1px solid rgba(120,180,255,.18);
  border-radius: 10px;
  color: #f3f8ff; font-size: 14px;
  transition: border-color .15s, box-shadow .15s, background .15s;
}
.campo input::placeholder { color: #5f6f8c; }
.campo input:focus {
  outline: none;
  border-color: rgba(108,210,255,.55);
  box-shadow: 0 0 0 3px rgba(108,210,255,.18);
  background: rgba(8, 14, 28, .92);
}

.login-btn {
  margin-top: 6px;
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  padding: 12px 16px; border: 0; border-radius: 10px; cursor: pointer;
  font-size: 14.5px; font-weight: 600; letter-spacing: .2px; color: #07101f;
  background: linear-gradient(120deg, #6cf 0%, #7df0a8 100%);
  box-shadow: 0 8px 20px rgba(108,210,255,.30);
  transition: transform .08s, box-shadow .15s, filter .15s;
}
.login-btn:hover { filter: brightness(1.05);
                   box-shadow: 0 10px 26px rgba(108,210,255,.42); }
.login-btn:active { transform: translateY(1px); }

.login-pie {
  margin-top: 18px; padding-top: 14px;
  border-top: 1px dashed rgba(120,180,255,.14);
  display: flex; align-items: center; gap: 8px;
  font-size: 11.5px; color: #7f90ad;
}
.pulso {
  width: 8px; height: 8px; border-radius: 50%; background: #7df0a8;
  box-shadow: 0 0 0 0 rgba(125,240,168,.65);
  animation: pulso 2s infinite;
}
@keyframes pulso {
  0%   { box-shadow: 0 0 0 0   rgba(125,240,168,.55); }
  70%  { box-shadow: 0 0 0 10px rgba(125,240,168,0);   }
  100% { box-shadow: 0 0 0 0   rgba(125,240,168,0);   }
}

@media (max-width: 480px) {
  .login-card { padding: 24px 20px 18px; border-radius: 14px; }
  .bot-avatar { width: 56px; height: 56px; }
}
"""


# ---------------------------------------------------------- Rutas ---
@router.get("/login", response_class=HTMLResponse)
def get_login(request: Request, next: str = "/", err: str = ""):
    if usuario_actual(request):
        return RedirectResponse(next or "/", status_code=HTTP_303_SEE_OTHER)
    return HTMLResponse(_pagina_login(error=err, next_url=next))


@router.post("/login", response_class=HTMLResponse)
def post_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    u = _buscar_usuario(email)
    if not u or not _verificar_password(password, u.get("password")):
        return HTMLResponse(
            _pagina_login(
                error="Correo o contraseña incorrectos.",
                next_url=next,
                email=email,
            ),
            status_code=401,
        )
    request.session["usuario"] = {
        "id": int(u["id"]),
        "name": u["name"],
        "email": u["email"],
    }
    destino = next if (next or "").startswith("/") else "/"
    return RedirectResponse(destino, status_code=HTTP_303_SEE_OTHER)


@router.get("/logout")
def logout(request: Request):
    cerrar_sesion(request)
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
