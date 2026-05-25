#!/usr/bin/env bash
# Setup ONE-TIME en gestor_oc para el clasificador IA.
#
# Instala:
#   1) python3-pymysql (necesario por salud.py)
#   2) Cron del monitor de salud detallado (cada hora → salud.txt + ALERTAS.txt)
#   3) Cron del monitor liviano de CSV (cada 5 min → salud.csv)
#   4) Cron de restart diario preventivo (03:00 cada noche)
#   5) Ajusta permisos del .env (640, no world-readable)
#   6) rsync diario del modelo a clasico (backup; idempotente)
#
# Idempotente: si los cron entries ya están, no los duplica.
#
# Uso (como root en gestor_oc):
#   bash /opt/ia-mp/install_setup.sh

set -e
cd /opt/ia-mp

# 0) deps de salud.py
if ! python3 -c "import pymysql" 2>/dev/null; then
  echo "→ instalando python3-pymysql..."
  apt-get update -qq && apt-get install -y -qq python3-pymysql
fi

# 1) directorio del monitor
mkdir -p /opt/ia-mp/monitor
chmod 755 /opt/ia-mp/monitor

# 2) permisos del .env (640: root puede leer, grupo del owner también, others no)
if [ -f /opt/ia-mp/.env ]; then
  chmod 640 /opt/ia-mp/.env || true
fi

# 3) crontab — sin pisar lo que ya esté
TMP=$(mktemp)
crontab -l 2>/dev/null > "$TMP" || true

ADD_SALUD='0 * * * * /usr/bin/python3 /opt/ia-mp/salud.py >/dev/null 2>>/opt/ia-mp/monitor/salud.err'
ADD_MON='*/5 * * * * /usr/bin/python3 /opt/ia-mp/monitor_salud.py 2>>/opt/ia-mp/monitor/cron.err'
ADD_RESTART='0 3 * * * cd /opt/ia-mp && /usr/bin/docker compose restart worker panel backtest 2>>/opt/ia-mp/monitor/restart.log'
ADD_BACKUP='0 4 * * 0 /usr/bin/rsync -a /opt/ia-mp/modelo_pactivo.joblib root@10.0.0.69:/backup/ia-mp/ 2>>/opt/ia-mp/monitor/backup.log || true'

grep -qF "salud.py" "$TMP" || echo "$ADD_SALUD" >> "$TMP"
grep -qF "monitor_salud.py" "$TMP" || echo "$ADD_MON" >> "$TMP"
grep -qF "docker compose restart" "$TMP" || echo "$ADD_RESTART" >> "$TMP"
grep -qF "rsync -a /opt/ia-mp/modelo" "$TMP" || echo "$ADD_BACKUP" >> "$TMP"

crontab "$TMP"
rm "$TMP"

# 4) primera corrida del health check — valida que todo esté OK
echo "→ corrida inicial de salud.py..."
/usr/bin/python3 /opt/ia-mp/salud.py | head -40

echo
echo "OK — cron instalado:"
crontab -l | grep -E "salud|monitor_salud|docker compose restart|rsync"
echo
echo "Outputs:"
echo "  /opt/ia-mp/monitor/salud.txt     ← reporte humano-legible más reciente"
echo "  /opt/ia-mp/monitor/salud.jsonl   ← historial JSON (una línea por hora)"
echo "  /opt/ia-mp/monitor/ALERTAS.txt   ← solo si algo está rojo"
echo "  /opt/ia-mp/monitor/salud.csv     ← snapshots cada 5 min (RAM/CPU básico)"
