#!/bin/bash
# Purga periódica de tablas que crecen sin control.
#
# QUÉ TOCA:
#   - clasificador_ia_backtest: mantiene 30 días. Tabla de experimento, los
#     hallazgos viejos no aportan al panel ni a las métricas vigentes.
#
# QUÉ NO TOCA (auditorías sagradas — nunca purgar):
#   - clasificador_ia_costos    (control financiero del presupuesto)
#   - clasificador_ia_log       (auditoría producción / cola de revisión)
#   - clasificador_ia_reglas    (reglas configuradas por humanos)
#
# Reversibilidad: este script es DESTRUCTIVO. Antes de borrar, hace un dump
# comprimido a /opt/ia-mp/monitor/backups/backtest-YYYYMMDD.sql.gz que
# logrotate maneja (rota mensual, retiene 6 meses).
set -euo pipefail

LOG=/opt/ia-mp/monitor/purgar.log
exec >>"$LOG" 2>&1

echo "=== $(date -Iseconds) purga iniciada ==="
set -a
# shellcheck source=/dev/null
source /opt/ia-mp/.env
set +a

BACKUP_DIR=/opt/ia-mp/monitor/backups
mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d)

# Dump comprimido SOLO de las filas que voy a borrar.
mysqldump \
  -h "${MYSQL_HOST:-10.0.0.69}" -P "${MYSQL_PORT:-3306}" \
  -u "$MYSQL_USER" --password="$MYSQL_PASSWORD" \
  --no-create-info --skip-add-drop-table --skip-add-locks \
  --where="creado_en < NOW() - INTERVAL 30 DAY" \
  "${MYSQL_DB:-licitaciones_diarias_total_farma}" clasificador_ia_backtest \
  | gzip > "$BACKUP_DIR/backtest-$STAMP.sql.gz"
echo "backup: $BACKUP_DIR/backtest-$STAMP.sql.gz ($(du -h "$BACKUP_DIR/backtest-$STAMP.sql.gz" | cut -f1))"

# Borrado por lotes (no bloquea la tabla con un solo DELETE gigante).
mysql -h "${MYSQL_HOST:-10.0.0.69}" -P "${MYSQL_PORT:-3306}" \
      -u "$MYSQL_USER" --password="$MYSQL_PASSWORD" \
      "${MYSQL_DB:-licitaciones_diarias_total_farma}" <<'SQL'
SET @batch := 10000;
SELECT COUNT(*) AS antes FROM clasificador_ia_backtest;
loop_purga: REPEAT
  DELETE FROM clasificador_ia_backtest
   WHERE creado_en < NOW() - INTERVAL 30 DAY
   LIMIT 10000;
  SELECT ROW_COUNT() INTO @borradas;
UNTIL @borradas = 0 END REPEAT;
SELECT COUNT(*) AS despues FROM clasificador_ia_backtest;
SQL

echo "=== $(date -Iseconds) purga ok ==="
