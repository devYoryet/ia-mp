"""Carga y predicción de modelo_adjunto: clasificador BINARIO '¿es Adjunto?'
(el detalle del producto está en un anexo de la licitación). Capa TEMPRANA de
la cascada (tras historico, antes de descarte_item): con umbral ALTO asigna
Adjunto sólo cuando está muy seguro (medido ~0.864 → 97% precisión), resolviendo
el Hueco B (los Adjunto que hoy mueren en descarte_item/modelo_descarte antes de
llegar a Claude) y bajando la carga de Claude. Si no está seguro, cae a Claude.

Ver `entrenar_adjunto.py`. Mismo molde que `modelo_marcas.py`.
"""
from __future__ import annotations

import logging
from pathlib import Path

import joblib

log = logging.getLogger("modelo_adjunto")

_MODELO_PATH = Path(__file__).resolve().parent / "modelo_adjunto.joblib"
_SEPARADOR = " ::: "  # mismo separador que entrenar_adjunto.py


def cargar_modelo_adjunto():
    """Carga el modelo, o None si no existe (fallback: la rama se saltea)."""
    if not _MODELO_PATH.exists():
        log.warning("modelo_adjunto.joblib no existe — rama desactivada")
        return None
    try:
        modelo = joblib.load(_MODELO_PATH)
        tam_mb = _MODELO_PATH.stat().st_size / 1e6
        log.info("Modelo de adjunto cargado (%s) — %.1f MB", _MODELO_PATH.name, tam_mb)
        return modelo
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo cargar modelo_adjunto (%s) — rama desactivada", exc)
        return None


def _texto(titulo: str, descripcion: str) -> str:
    tit = (titulo or "").strip()
    desc = (descripcion or "").strip()
    return _SEPARADOR.join(t for t in (tit, desc) if t)


def prob_adjunto(modelo, titulo: str, descripcion: str) -> float:
    """Devuelve P(Adjunto) en [0,1]. 0.0 si el modelo no está o el texto vacío.
    La cascada compara contra config.umbral_adjunto para decidir."""
    if modelo is None:
        return 0.0
    texto = _texto(titulo, descripcion)
    if not texto:
        return 0.0
    try:
        proba = modelo.predict_proba([texto])[0]
        # clase 1 = Adjunto (ver entrenar_adjunto.py: y=1 para positivos)
        classes = list(modelo.classes_)
        idx = classes.index(1) if 1 in classes else int(proba.argmax())
        return float(proba[idx])
    except Exception as exc:  # noqa: BLE001
        log.warning("prob_adjunto falló (%s)", exc)
        return 0.0
