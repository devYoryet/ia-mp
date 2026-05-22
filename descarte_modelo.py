"""Etapa de DESCARTE APRENDIDO — clasificador entrenado con el histórico.

Carga el modelo `modelo_descarte.joblib` (TF-IDF + regresión logística entrenado
por `entrenar_descarte.py` sobre ~1,1M de filas etiquetadas) y predice la
probabilidad de que una fila sea descarte. Inferencia en memoria, sin API.

Va como última etapa barata antes de Claude: si el modelo está MUY seguro de que
es descarte (prob >= umbral), la fila se descarta sin gastar una llamada — hoy el
~85% de las llamadas a Claude son solo para descartar."""

from __future__ import annotations

import logging
from pathlib import Path

import joblib

log = logging.getLogger("descarte_modelo")

_RUTA = Path(__file__).resolve().parent / "modelo_descarte.joblib"


def cargar_modelo_descarte():
    """Carga el pipeline entrenado. Devuelve None si el modelo aún no existe
    (en ese caso la etapa simplemente no actúa — la cascada sigue sin él)."""
    if not _RUTA.exists():
        log.warning("%s no existe — etapa de descarte aprendido DESACTIVADA "
                    "(corré entrenar_descarte.py)", _RUTA.name)
        return None
    modelo = joblib.load(_RUTA)
    log.info("Modelo de descarte cargado (%s)", _RUTA.name)
    return modelo


def prob_descarte(modelo, texto: str | None) -> float:
    """Probabilidad 0..1 de que la fila sea DESCARTE, según el modelo entrenado.
    0.0 si no hay modelo o no hay texto. El pipeline normaliza el texto solo
    (lleva `normalizar` como preprocessor, igual que en el entrenamiento)."""
    if modelo is None or not texto:
        return 0.0
    try:
        idx0 = list(modelo.classes_).index(0)  # clase 0 = descarte
        return float(modelo.predict_proba([texto])[0][idx0])
    except Exception:  # noqa: BLE001
        return 0.0
