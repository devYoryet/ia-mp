"""Carga del modelo_marcas (clasificador por contexto: título + descripción +
vínculos). Capa COMPLEMENTARIA a modelo_pactivo, especializada en glosas donde
el principio activo NO aparece literal y el pactivo se infiere de la marca
comercial + el contexto del tender.

Ver `entrenar_marcas.py` para los datos de entrenamiento y los filtros aplicados.
"""

from __future__ import annotations

import logging
from pathlib import Path

import joblib

from reglas import normalizar

log = logging.getLogger("modelo_marcas")

_MODELO_PATH = Path(__file__).resolve().parent / "modelo_marcas.joblib"

# Mismo separador que se usó en entrenar_marcas.py al construir el texto.
_SEPARADOR = " ::: "


def cargar_modelo_marcas():
    """Carga el modelo entrenado, o None si no existe (fallback: la rama se
    saltea, la cascada sigue como antes)."""
    if not _MODELO_PATH.exists():
        log.warning("modelo_marcas.joblib no existe — rama desactivada")
        return None
    try:
        modelo = joblib.load(_MODELO_PATH)
        n_clases = len(modelo.classes_) if hasattr(modelo, "classes_") else 0
        tam_mb = _MODELO_PATH.stat().st_size / 1e6
        log.info("Modelo de marcas cargado (%s) — %d clases, %.0f MB",
                 _MODELO_PATH.name, n_clases, tam_mb)
        return modelo
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo cargar modelo_marcas (%s) — rama desactivada", exc)
        return None


def _texto_concat(titulo: str, descripcion: str, vinculos: str) -> str:
    """Mismo formato que el entrenamiento."""
    tit = (titulo or "").strip()
    desc = (descripcion or "").strip()
    vin = (vinculos or "").strip()[:400]
    return _SEPARADOR.join(t for t in (tit, desc, vin) if t)


def predecir(modelo, titulo: str, descripcion: str, vinculos: str) -> "tuple[str | None, float]":
    """Devuelve (pactivo_predicho, confianza). Si el modelo no está, (None, 0.0).
    La cascada decide qué hacer con el resultado según los umbrales del config:
      - conf >= umbral_alto    → asignar como interés normal (verde)
      - conf >= umbral_bajo    → estado 'posiblemente de interés' (amarillo)
      - conf <  umbral_bajo    → ignorar, cae a Claude
    """
    if modelo is None:
        return (None, 0.0)
    texto = _texto_concat(titulo, descripcion, vinculos)
    if not texto:
        return (None, 0.0)
    try:
        proba = modelo.predict_proba([texto])[0]
        idx = int(proba.argmax())
        return (str(modelo.classes_[idx]), float(proba[idx]))
    except Exception as exc:  # noqa: BLE001
        log.warning("predecir falló (%s)", exc)
        return (None, 0.0)
