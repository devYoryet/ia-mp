"""Configuración central del clasificador IA (lee variables desde .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


@dataclass(frozen=True)
class Config:
    db_host: str = os.getenv("MYSQL_HOST", "10.0.0.69")
    db_port: int = int(os.getenv("MYSQL_PORT", "3306"))
    db_user: str = os.getenv("MYSQL_USER", "root")
    db_password: str = os.getenv("MYSQL_PASSWORD", "")
    db_name: str = os.getenv("MYSQL_DB", "licitaciones_diarias_total_farma")
    # Catálogo PRIMARIO de pactivos: 0001_td_oc.Base (columnas Pactivo/Comp/MedidaPHT).
    db_catalogo: str = os.getenv("MYSQL_DB_CATALOGO", "0001_td_oc")
    # Catálogo SECUNDARIO: la tabla `diccionario` de CLASIFICACIÓN (pactivo + comp +
    # presentacion + keywords) — la MISMA que usa el legacy de gestor_licitaciones.
    # Vive en la base principal. NO es `principal_app.diccionario_unidad` (esa es
    # el diccionario por cliente, del envío posterior). Verificado 2026-05-22.
    db_diccionario: str = os.getenv("MYSQL_DB_DICCIONARIO", "licitaciones_diarias_total_farma")

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    # Modelo por defecto: Opus 4.7 — máxima precisión para el backtest (definir
    # el techo de calidad). Para producción se puede bajar a claude-sonnet-4-6
    # o claude-haiku-4-5 según el costo/precisión que muestre el backtest.
    modelo: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7")

    intervalo_segundos: int = int(os.getenv("INTERVALO_SEGUNDOS", "300"))
    lote_max: int = int(os.getenv("LOTE_MAX", "200"))
    # Etapa 0 (descarte por Item): nº mínimo de veces que un código de Item
    # debe haber aparecido, SIEMPRE descartado, para confiar en él.
    descarte_item_min_vistas: int = int(os.getenv("DESCARTE_ITEM_MIN_VISTAS", "20"))
    # Etapa 1 (histórico): un DESCARTE del histórico solo se confía si la
    # descripción idéntica se descartó al menos esta cantidad de veces. Por
    # debajo, el descarte NO se devuelve y la fila cae a las etapas siguientes.
    # Evita reproducir errores humanos de descarte de soporte bajo (el backtest
    # 2026-05-22 mostró 9 falsos negativos así, todos con soporte 1-2). El
    # interés no lleva umbral: un falso positivo lo filtra el panel.
    umbral_descarte_historico: int = int(os.getenv("UMBRAL_DESCARTE_HISTORICO", "5"))
    # Etapa de descarte APRENDIDO: probabilidad mínima del clasificador entrenado
    # (modelo_descarte.joblib) para auto-descartar sin llamar a Claude. Por debajo
    # del umbral, la fila sigue a Claude. Conservador a propósito.
    umbral_modelo_descarte: float = float(os.getenv("UMBRAL_MODELO_DESCARTE", "0.97"))
    # Clasificador multiclase de pactivo: probabilidad mínima del modelo
    # entrenado para auto-asignar un pactivo sin pasar por Claude.
    # Calibración medida (2026-05-24, modelo SGD directo sin CalibratedCV,
    # 100 ej/pactivo, 1.593 clases, accuracy top-1 95%):
    #   umbral 0.40 → cubre 61% del residuo con 99.3% acierto
    #   umbral 0.50 → cubre 35% con 99.7%
    # Sin el calibrador externo las probas se concentran bajo (es esperable),
    # por eso el umbral baja de 0.70 a 0.40. Recalibrar si se cambia el modelo.
    umbral_modelo_pactivo: float = float(os.getenv("UMBRAL_MODELO_PACTIVO", "0.40"))
    # Top-K pactivos del catálogo, ordenados por palabras de la descripción, que
    # se pasan a Claude como PISTA en el mensaje de usuario. No acota el
    # catálogo (sigue completo en el system prompt). 0 = desactivado.
    top_k_pactivos: int = int(os.getenv("TOP_K_PACTIVOS", "20"))
    auto_aplicar_descartes: bool = (
        os.getenv("AUTO_APLICAR_DESCARTES", "false").lower() == "true"
    )
    # 'test'  = backtest: clasifica filas que ya clasificó una persona y compara,
    #           SIN escribir nada en compra_agil (no interviene producción).
    # 'produccion' = escribe la clasificación en compra_agil.
    modo: str = os.getenv("MODO", "test")

    # Presupuesto y proyección de costo (para el panel).
    budget_usd: float = float(os.getenv("BUDGET_USD", "350"))
    filas_mes_estimado: int = int(os.getenv("FILAS_MES_ESTIMADO", "350000"))


config = Config()
