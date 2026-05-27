"""Cruce diario para construir el CATÁLOGO ACTIVO de pactivos.

Regla de negocio (ver [[ops-monitoreo-guardrails]] y [[modelo-pactivo-multiclase]]):

  catalogo_activo =
        pactivos de 0001_td_oc.Base                 ← SAGRADOS (OC reales históricas)
      ∪ delta(licitaciones.diccionario − Base) que aparezcan en al menos UN
        cliente ACTIVO de Pharmatender (pharmatender.company + users)
      ∪ WHITELIST de meta-pactivos                  ← Adjunto

Cliente ACTIVO = compañía con MÁS DE 1 usuario con `deleted_at IS NULL`.
El cruce se rehace en cada arranque/refresh del worker (cada 24h por defecto),
así si entra un cliente nuevo con "Sonda" mañana, "Sonda" vuelve al catálogo
automáticamente. Si se desactiva un cliente, sus pactivos únicos salen.

Conexiones:
  - prime  (10.0.0.68:8806) : `pharmatender` — company + users
  - clasico (10.0.0.69:3306): `principal_app` — unidad_negocio + diccionario_unidad

Credenciales en .env del worker (MYSQL_PRIME_*, MYSQL_PRINCIPAL_*).
"""

from __future__ import annotations

import logging
import os

import pymysql

from reglas import normalizar

log = logging.getLogger("catalogo_activo")

# Pactivos META que siempre quedan en el catálogo aunque no estén en clientes
# activos. Hoy solo "Adjunto" — meta-señal de que el listado real está en un
# archivo anexo (medido en producción: humanos lo asignan 4.938 veces, 99.7%
# son interés). Solo Claude (con contexto) puede asignarlo — ver `reglas.py`
# (PACTIVOS_NO_MATCH_DIRECTO) y `clasificador_claude.py` (prompt).
WHITELIST = {"Adjunto"}

# RUTs que NO se consideran clientes activos aunque tengan usuarios vivos en
# `pharmatender.company`. Pharmatender (12345678-5) es la cuenta INTERNA de
# la empresa — los users son del equipo (Yoryet, Fernando, Evelyn, etc.); no
# es un cliente al que se le vende. Su `diccionario_unidad` contiene pactivos
# de TESTING ("Guantes", "Guante", etc.) que NO deben contaminar el catálogo
# activo. Verificado en producción 2026-05-27.
BLACKLIST_RUTS = {"12345678-5"}


def _conn(host: str, port: int, user: str, pw: str, db: str):
    return pymysql.connect(
        host=host, port=port, user=user, password=pw, database=db,
        charset="utf8mb4", connect_timeout=15, cursorclass=pymysql.cursors.DictCursor,
    )


def _ruts_activos_prime() -> set[str]:
    """RUTs de compañías con más de 1 usuario vivo en prime."""
    host = os.getenv("MYSQL_PRIME_HOST", "10.0.0.68")
    port = int(os.getenv("MYSQL_PRIME_PORT", "8806"))
    user = os.getenv("MYSQL_PRIME_USER", "root")
    pw = os.getenv("MYSQL_PRIME_PASSWORD", "")
    if not pw:
        log.warning("MYSQL_PRIME_PASSWORD no configurado — catálogo NO se filtra")
        return set()
    try:
        with _conn(host, port, user, pw, "pharmatender") as c, c.cursor() as cur:
            cur.execute(
                "SELECT c.rut FROM company c "
                "JOIN users u ON u.company_id = c.id "
                "WHERE u.deleted_at IS NULL "
                "GROUP BY c.id, c.rut HAVING COUNT(*) > 1"
            )
            ruts = {r["rut"].strip() for r in cur.fetchall() if r["rut"]}
            # Quitar RUTs internos (Pharmatender misma, testing) — ver BLACKLIST_RUTS
            return ruts - BLACKLIST_RUTS
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo consultar prime (%s) — catálogo SIN filtro", exc)
        return set()


def _pactivos_de_clientes_activos(ruts: set[str]) -> set[str]:
    """Pactivos (normalizados) registrados en diccionario_unidad de unidades
    cuyo RUT está en `ruts`. Devuelve set de pactivos normalizados — la
    intersección final se hace contra los pactivos originales."""
    if not ruts:
        return set()
    host = os.getenv("MYSQL_HOST", "10.0.0.69")
    port = int(os.getenv("MYSQL_PORT", "3306"))
    user = os.getenv("MYSQL_PRINCIPAL_USER", os.getenv("MYSQL_USER", "root"))
    pw = os.getenv("MYSQL_PRINCIPAL_PASSWORD", os.getenv("MYSQL_PASSWORD", ""))
    placeholders = ",".join(["%s"] * len(ruts))
    try:
        with _conn(host, port, user, pw, "principal_app") as c, c.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT du.pactivo FROM diccionario_unidad du "
                "JOIN unidad_negocio un ON un.id = du.intIdUnidad_negocio "
                f"WHERE un.strRutUsuario IN ({placeholders})",
                tuple(ruts),
            )
            return {normalizar(r["pactivo"]) for r in cur.fetchall() if r["pactivo"]}
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo consultar principal_app (%s) — sin filtro activo", exc)
        return set()


def construir_filtro_activo() -> "set[str] | None":
    """Devuelve el set de pactivos NORMALIZADOS que están "activos" (en algún
    cliente activo + whitelist). Si no se puede consultar prime/principal_app,
    devuelve None y la taxonomía no aplica filtro (fallback seguro: catálogo
    completo, comportamiento anterior).

    El filtro se aplica al DELTA (pactivos del diccionario que NO están en
    `0001_td_oc.Base`). Los de Base son sagrados — no se filtran.
    """
    ruts = _ruts_activos_prime()
    if not ruts:
        return None
    activos = _pactivos_de_clientes_activos(ruts)
    log.info("Filtro activo: %d RUTs activos, %d pactivos en clientes activos",
             len(ruts), len(activos))
    # Whitelist (pactivos META que siempre se conservan) se suma normalizada
    return activos | {normalizar(p) for p in WHITELIST}
