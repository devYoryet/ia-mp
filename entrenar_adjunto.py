"""Entrena modelo_adjunto: clasificador BINARIO '¿es Adjunto?' sobre título +
descripción. Adjunto = meta-pactivo (el detalle del producto está en un anexo
de la licitación, no en la glosa). Capa TEMPRANA de la cascada (tras historico,
antes de descarte_item): captura el Adjunto "obvio" de forma precisa y barata
(sin Claude), resolviendo el Hueco B (los que hoy mueren en descarte_item/
modelo_descarte antes de llegar a Claude). El resto (dudoso) cae a Claude como
hoy (recall-first backstop). Mismo molde que entrenar_marcas.py.

Umbral: se usa uno ALTO (predict_proba de la clase Adjunto) para asignar sólo
cuando el modelo está muy seguro; medido en CV honesto, ~0.89 → 97% precisión.

Salida: modelo_adjunto.joblib (lo carga el worker).
NO llama a Claude — respeta la regla de backtests sin API.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import precision_recall_curve
from sklearn.model_selection import cross_val_predict
from sklearn.pipeline import Pipeline

import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("entrenar_adjunto")

MODELO_PATH = Path(__file__).resolve().parent / "modelo_adjunto.joblib"
SEPARADOR = " ::: "
DIAS_POS = 150      # ventana para positivos (Adjunto humano)
DIAS_NEG = 45       # ventana para negativos (más recientes, catálogo vigente)
NEG_POR_POS = 8     # razón negativos:positivos


def _texto(titulo, descripcion):
    tit = (titulo or "").strip()
    desc = (descripcion or "").strip()
    return SEPARADOR.join(t for t in (tit, desc) if t)


def cargar_datos():
    cn = db.conectar(); cur = cn.cursor()
    def g(x, k): return x[k] if isinstance(x, dict) else x
    pos, neg = [], []
    for t in ("compra_agil", "Licitaciones_diarias"):
        cur.execute(f"""SELECT Titulo, Descripcion FROM {t}
            WHERE pactivo='Adjunto' AND estado_gestor=1
              AND fecha_clasificacion>=NOW()-INTERVAL {DIAS_POS} DAY""")
        pos += [_texto(g(r, 'Titulo'), g(r, 'Descripcion')) for r in cur.fetchall()]
    for t in ("compra_agil", "Licitaciones_diarias"):
        # negativos = descartes + interés NO-Adjunto (para separar de ambos)
        cur.execute(f"""SELECT Titulo, Descripcion FROM {t}
            WHERE estado_gestor IS NOT NULL AND (pactivo IS NULL OR pactivo<>'Adjunto')
              AND fecha_clasificacion>=NOW()-INTERVAL {DIAS_NEG} DAY
            ORDER BY RAND() LIMIT {len(pos) * NEG_POR_POS // 2}""")
        neg += [_texto(g(r, 'Titulo'), g(r, 'Descripcion')) for r in cur.fetchall()]
    pos = [p for p in pos if p]
    neg = [n for n in neg if n]
    return pos, neg


def construir_pipeline():
    vec = TfidfVectorizer(max_features=6000, ngram_range=(1, 2), min_df=2,
                          sublinear_tf=True, strip_accents="unicode")
    clf = LogisticRegression(max_iter=2000, class_weight="balanced", C=2.0)
    return Pipeline([("vec", vec), ("clf", clf)])


def main():
    t0 = time.time()
    pos, neg = cargar_datos()
    X = pos + neg
    y = np.array([1] * len(pos) + [0] * len(neg))
    log.info("datos: %d positivos (Adjunto), %d negativos", len(pos), len(neg))
    pipe = construir_pipeline()

    # métricas honestas out-of-fold
    proba = cross_val_predict(pipe, X, y, cv=5, method="predict_proba")[:, 1]
    prec, rec, thr = precision_recall_curve(y, proba)
    log.info("== curva precision-recall (CV 5-fold) ==")
    umbral_97 = None
    for target in (0.99, 0.97, 0.95, 0.90):
        idx = np.where(prec[:-1] >= target)[0]
        if len(idx):
            i = idx[0]
            log.info("  precision>=%.0f%%: recall=%.0f%% (umbral %.3f)",
                     target * 100, rec[i] * 100, thr[i])
            if target == 0.97:
                umbral_97 = float(thr[i])
    log.info("umbral sugerido (97%% prec): %s", umbral_97)

    # entrenar en todo y guardar
    pipe.fit(X, y)
    joblib.dump(pipe, MODELO_PATH, compress=3)
    tam = MODELO_PATH.stat().st_size / 1e6
    log.info("modelo_adjunto.joblib guardado (%.1f MB) en %.1fs", tam, time.time() - t0)


if __name__ == "__main__":
    main()
