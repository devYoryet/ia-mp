#!/bin/bash
# Despliegue de la Fase 2 en gestor_oc. Sólo toca /opt/ia-mp-fase2 y el
# container ia-mp-fase2. NO toca /opt/ia-mp ni los containers del clasificador.
set -euo pipefail
cd /opt/ia-mp-fase2
git fetch origin fase2-device
git reset --hard origin/fase2-device
cd fase2
docker compose build
docker compose up -d
docker compose ps
