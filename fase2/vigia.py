"""Vigía de la Fase 2: avisa por correo si algo deja de funcionar.

Corre por cron en gestor_oc cada 10 minutos (un container que nace y muere; no
queda nada corriendo):

    */10 * * * * cd /opt/ia-mp-fase2/fase2 && flock -n /tmp/ia-mp-fase2-vigia.lock \
        docker compose --profile vigia run --rm vigia >> /var/log/ia-mp-fase2-vigia.log 2>&1

Revisa, con el usuario de sólo lectura de la fase 2:
  - barrido:     la última corrida buena (rápida o profunda) tiene menos de F2V_MAX_MIN minutos;
  - entrega:     lo mismo para el container de entrega (si F2V_ENTREGA=1);
  - panel:       https://F2V_DOMINIO/salud responde, a través del nginx local;
  - certificado: al del dominio le quedan más de F2V_DIAS_CERT días;
  - revisión:    hay filas esperando y nadie decide nada hace F2V_DIAS_SIN_REVISION días.

Un error suelto no avisa: sólo cuenta que no haya una corrida BUENA reciente.
Avisa cuando aparece un problema, lo recuerda cada F2V_REPETIR_HORAS mientras
siga (la revisión detenida, una vez al día) y avisa cuando se resuelve. El
estado vive en F2V_ESTADO. No escribe en la BD ni toca la fase 1.

    python vigia.py            # revisa y avisa
    python vigia.py --seco     # sólo muestra qué avisaría
    python vigia.py --prueba   # manda un correo de prueba
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import smtplib
import socket
import ssl
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

MAX_MIN = int(os.getenv("F2V_MAX_MIN", "45"))
DIAS_CERT = int(os.getenv("F2V_DIAS_CERT", "14"))
DIAS_SIN_REVISION = int(os.getenv("F2V_DIAS_SIN_REVISION", "2"))
REPETIR_HORAS = int(os.getenv("F2V_REPETIR_HORAS", "6"))
REPETIR_REVISION_HORAS = 24
ESTADO = Path(os.getenv("F2V_ESTADO", "/estado/vigia.json"))
PREFIJO = os.getenv("CORREO_PREFIJO", "[Automático] ")

TITULOS = {
    "bd": "No se puede leer la BD",
    "barrido": "El barrido está detenido",
    "entrega": "La entrega a clientes está detenida",
    "panel": "El panel de revisión no responde",
    "certificado": "El certificado HTTPS vence pronto",
    "revision": "Nadie está revisando",
}


# --------------------------------------------------------------------------
# Revisiones: cada una devuelve {clave: texto} sólo si hay problema
# --------------------------------------------------------------------------
def revisar_bd(bd, entrega: bool) -> dict[str, str]:
    p: dict[str, str] = {}
    for clave, tipos in (("barrido", "'rapido','profundo'"), ("entrega", "'entrega'")):
        if clave == "entrega" and not entrega:
            continue
        ok = bd.uno(f"SELECT creado_en, TIMESTAMPDIFF(MINUTE, creado_en, NOW()) AS hace FROM clasificador_f2_corridas "
                    f"WHERE tipo IN ({tipos}) AND error IS NULL ORDER BY id DESC LIMIT 1")
        if ok and ok["hace"] is not None and ok["hace"] <= MAX_MIN:
            continue
        err = bd.uno(f"SELECT creado_en, error FROM clasificador_f2_corridas WHERE tipo IN ({tipos}) "
                     f"AND error IS NOT NULL ORDER BY id DESC LIMIT 1")
        texto = (f"Última corrida buena: {ok['creado_en']} (hace {ok['hace']} min)." if ok
                 else "No hay ninguna corrida buena registrada.")
        if err and (not ok or err["creado_en"] > ok["creado_en"]):
            texto += f" Último error ({err['creado_en']}): {str(err['error'])[:300]}"
        p[clave] = texto

    pend = bd.uno("SELECT COUNT(*) AS n, MIN(creado_en) AS desde, "
                  "SUM(creado_en < NOW() - INTERVAL %s DAY) AS viejas FROM clasificador_f2_resultado "
                  "WHERE vigente=1 AND decision IS NULL AND estado_auto='revision' AND fecha_cierre >= NOW()",
                  (DIAS_SIN_REVISION,))
    ult = bd.uno("SELECT MAX(revisado_en) AS ult, SUM(revisado_en >= NOW() - INTERVAL %s DAY) AS recientes "
                 "FROM clasificador_f2_resultado WHERE decision IS NOT NULL", (DIAS_SIN_REVISION,))
    if pend and int(pend["n"] or 0) > 0 and int(pend["viejas"] or 0) > 0 and not int((ult or {}).get("recientes") or 0):
        cuando = f"la última fue el {ult['ult']}" if ult and ult["ult"] else "nunca se ha revisado ninguna"
        p["revision"] = (f"{pend['n']} filas abiertas esperan aprobar / no aprobar y no hay decisiones en "
                         f"{DIAS_SIN_REVISION} días ({cuando}). Lo que está en revisión no se entrega al cliente.")
    return p


def revisar_panel(dominio: str, puerto_local: int = 8810) -> dict[str, str]:
    """Pide /salud al panel. Con dominio, pasa por el nginx local con SNI (el
    servidor no llega a su propia IP pública: hairpin NAT) y lee el certificado."""
    p: dict[str, str] = {}
    try:
        if dominio:
            ctx = ssl.create_default_context()
            with socket.create_connection(("127.0.0.1", 443), timeout=15) as s, \
                    ctx.wrap_socket(s, server_hostname=dominio) as t:
                vence = datetime.fromtimestamp(ssl.cert_time_to_seconds(t.getpeercert()["notAfter"]), timezone.utc)
                t.sendall(f"GET /salud HTTP/1.1\r\nHost: {dominio}\r\nConnection: close\r\n\r\n".encode())
                resp = http.client.HTTPResponse(t)
                resp.begin()
                estado = resp.status
            quedan = (vence - datetime.now(timezone.utc)).days
            if quedan < DIAS_CERT:
                p["certificado"] = f"El certificado de {dominio} vence el {vence:%Y-%m-%d} (quedan {quedan} días). " \
                                   "Revisar certbot: certbot renew --dry-run"
        else:
            c = http.client.HTTPConnection("127.0.0.1", puerto_local, timeout=15)
            c.request("GET", "/salud")
            estado = c.getresponse().status
        if estado != 200:
            p["panel"] = f"/salud respondió HTTP {estado}."
    except ssl.SSLCertVerificationError as exc:
        p["certificado"] = f"El certificado de {dominio} no es válido: {exc.verify_message}."
    except Exception as exc:  # noqa: BLE001
        p["panel"] = f"Sin respuesta de {dominio or f'127.0.0.1:{puerto_local}'}: {type(exc).__name__}: {exc}"
    return p


# --------------------------------------------------------------------------
# Cuándo avisar (puro, se testea)
# --------------------------------------------------------------------------
def planificar(actuales: dict[str, str], estado: dict, ahora: datetime) -> tuple[dict, dict]:
    """Devuelve (aviso, estado_nuevo). aviso = {} si no corresponde mandar nada;
    si no, {"nuevos": [...], "resueltos": [...], "recordar": [...]}."""
    previos = estado.get("problemas", {})
    nuevos = [k for k in actuales if k not in previos]
    resueltos = [k for k in previos if k not in actuales]
    recordar = []
    for k in actuales:
        if k in previos:
            horas = REPETIR_REVISION_HORAS if k == "revision" else REPETIR_HORAS
            if ahora - datetime.fromisoformat(previos[k]["aviso"]) >= timedelta(hours=horas):
                recordar.append(k)
    aviso = {"nuevos": nuevos, "resueltos": resueltos, "recordar": recordar} if (nuevos or resueltos or recordar) else {}
    marca = ahora.isoformat(timespec="seconds")
    problemas = {k: {"desde": previos.get(k, {}).get("desde", marca),
                     "aviso": marca if aviso else previos[k]["aviso"],
                     "texto": v} for k, v in actuales.items()}
    return aviso, {"problemas": problemas}


def redactar(aviso: dict, actuales: dict[str, str], estado_nuevo: dict, dominio: str) -> tuple[str, str]:
    if actuales:
        asunto = f"{PREFIJO}Fase 2 Device: " + ", ".join(TITULOS[k].lower() for k in actuales)
    else:
        asunto = f"{PREFIJO}Fase 2 Device: todo resuelto"
    lineas = []
    if actuales:
        lineas.append("PROBLEMAS ACTUALES")
        for k, texto in actuales.items():
            marca = " (nuevo)" if k in aviso.get("nuevos", []) else \
                f" (desde {estado_nuevo['problemas'][k]['desde'].replace('T', ' ')} UTC)"
            lineas += [f"- {TITULOS[k]}{marca}", f"  {texto}"]
    if aviso.get("resueltos"):
        lineas.append("")
        lineas.append("RESUELTOS")
        lineas += [f"- {TITULOS.get(k, k)}" for k in aviso["resueltos"]]
    lineas += ["", f"Panel: https://{dominio}/resumen" if dominio else "Panel: http://127.0.0.1:8810/resumen",
               "En el servidor: cd /opt/ia-mp-fase2/fase2 && docker compose ps && docker compose logs --tail 50",
               "", "La fase 1 (clasificador farma) no depende de nada de esto."]
    return asunto, "\n".join(lineas)


# --------------------------------------------------------------------------
# Correo y estado
# --------------------------------------------------------------------------
def enviar(asunto: str, texto: str) -> bool:
    remitente = os.getenv("ALERT_EMAIL_SENDER", "")
    clave = os.getenv("ALERT_EMAIL_PASSWORD", "").replace(" ", "")
    destinos = [d.strip() for d in os.getenv("F2V_DESTINOS", "").split(",") if d.strip()]
    if not (remitente and clave and destinos):
        print("Sin ALERT_EMAIL_SENDER / ALERT_EMAIL_PASSWORD / F2V_DESTINOS: no se envía.")
        return False
    msg = MIMEText(texto, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = asunto, remitente, ", ".join(destinos)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
            s.login(remitente, clave)
            s.sendmail(remitente, destinos, msg.as_string())
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"No se pudo enviar el correo: {type(exc).__name__}: {exc}")
        return False


def leer_estado() -> dict:
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def guardar_estado(e: dict) -> None:
    ESTADO.parent.mkdir(parents=True, exist_ok=True)
    tmp = ESTADO.with_suffix(".tmp")
    tmp.write_text(json.dumps(e, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ESTADO)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seco", action="store_true", help="no envía ni guarda estado")
    ap.add_argument("--prueba", action="store_true", help="manda un correo de prueba y sale")
    a = ap.parse_args()
    dominio = os.getenv("F2V_DOMINIO", "").strip()
    ahora = datetime.now()
    if a.prueba:
        ok = enviar(f"{PREFIJO}Fase 2 Device: correo de prueba del vigía",
                    f"Prueba del vigía de la fase 2 ({ahora:%Y-%m-%d %H:%M} UTC). Si llega, los avisos funcionan.")
        print("prueba enviada" if ok else "prueba NO enviada")
        return

    actuales: dict[str, str] = {}
    try:
        from bd import BD
        bd = BD()
        try:
            actuales.update(revisar_bd(bd, entrega=os.getenv("F2V_ENTREGA", "1") == "1"))
        finally:
            bd.cerrar()
    except Exception as exc:  # noqa: BLE001
        actuales["bd"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    actuales.update(revisar_panel(dominio))

    estado = leer_estado()
    aviso, nuevo = planificar(actuales, estado, ahora)
    resumen = ", ".join(sorted(actuales)) or "ok"
    if not aviso:
        print(f"{ahora:%Y-%m-%d %H:%M:%S} vigía: {resumen} (sin aviso)")
        if not a.seco:
            guardar_estado(nuevo)
        return
    asunto, texto = redactar(aviso, actuales, nuevo, dominio)
    if a.seco:
        print(asunto + "\n" + texto)
        return
    if enviar(asunto, texto):
        guardar_estado(nuevo)
        print(f"{ahora:%Y-%m-%d %H:%M:%S} vigía: {resumen} → aviso enviado ({aviso})")
    else:
        print(f"{ahora:%Y-%m-%d %H:%M:%S} vigía: {resumen} → aviso PENDIENTE (se reintenta)")


if __name__ == "__main__":
    main()
