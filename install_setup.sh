#!/usr/bin/env bash
# Setup ONE-TIME en gestor_oc para el clasificador IA.
#
# Instala:
#   1) Cron del monitor de salud (cada 5 min → /opt/ia-mp/monitor/salud.csv)
#   2) Cron de restart diario preventivo (03:00 cada noche, libera RAM/CPU)
#
# Idempotente: si los cron entries ya están, no los duplica.
#
# Uso (como root en gestor_oc):
#   bash /opt/ia-mp/install_setup.sh

set -e

cd /opt/ia-mp

# 1) directorio del monitor
mkdir -p /opt/ia-mp/monitor
chmod 755 /opt/ia-mp/monitor

# 2) crontab — sin pisar lo que ya esté
TMP=$(mktemp)
crontab -l 2>/dev/null > "$TMP" || true

ADD_MON='*/5 * * * * /usr/bin/python3 /opt/ia-mp/monitor_salud.py 2>>/opt/ia-mp/monitor/cron.err'
ADD_RESTART='0 3 * * * cd /opt/ia-mp && /usr/bin/docker compose restart worker panel backtest 2>>/opt/ia-mp/monitor/restart.log'

grep -qF "monitor_salud.py" "$TMP" || echo "$ADD_MON" >> "$TMP"
grep -qF "docker compose restart" "$TMP" || echo "$ADD_RESTART" >> "$TMP"

crontab "$TMP"
rm "$TMP"

echo "OK — cron instalado:"
crontab -l | grep -E "monitor_salud|docker compose restart"
echo
echo "Monitor: corre cada 5 min, log en /opt/ia-mp/monitor/salud.csv"
echo "Restart: cada noche 03:00, log en /opt/ia-mp/monitor/restart.log"
