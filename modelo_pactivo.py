"""Etapa de pactivo APRENDIDO — clasificador multiclase entrenado.

Carga el modelo `modelo_pactivo.joblib` (TF-IDF + SGD/LogReg calibrado, ~1.5K
clases) y, dado el texto de una glosa, devuelve el pactivo más probable junto a
su confianza.

Va en la cascada entre `modelo_descarte` y `claude`: si el modelo está MUY
seguro de un pactivo del catálogo (predict_proba >= umbral), la fila se
resuelve sin gastar una llamada API. Atrapa los casos donde la glosa se
escribe distinto a las históricas pero comparte componentes léxicos —
exactamente lo que hoy va a Claude.
"""

from __future__ import annotations

import logging
from pathlib import Path

import joblib

log = logging.getLogger("modelo_pactivo")

_RUTA = Path(__file__).resolve().parent / "modelo_pactivo.joblib"


def cargar_modelo_pactivo():
    """Carga el pipeline entrenado. Devuelve None si no existe (la etapa se
    desactiva — la cascada sigue funcionando sin él)."""
    if not _RUTA.exists():
        log.warning("%s no existe — etapa modelo_pactivo DESACTIVADA "
                    "(corré entrenar_pactivo.py)", _RUTA.name)
        return None
    modelo = joblib.load(_RUTA)
    log.info("Modelo de pactivo cargado (%s) — %d clases",
             _RUTA.name, len(modelo.classes_))
    return modelo


def predecir(modelo, texto: str | None) -> "tuple[str | None, float]":
    """Top-1 pactivo y su probabilidad. (None, 0.0) si no hay modelo o texto."""
    if modelo is None or not texto:
        return (None, 0.0)
    try:
        proba = modelo.predict_proba([texto])[0]
        idx = int(proba.argmax())
        return (modelo.classes_[idx], float(proba[idx]))
    except Exception:  # noqa: BLE001
        return (None, 0.0)
