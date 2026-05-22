#!/usr/bin/env python3
"""Entrena el clasificador de DESCARTE.

Aprende la decisión interés/descarte del histórico humano (millones de filas ya
etiquetadas), para resolver esa decisión binaria SIN llamar a Claude — hoy el
~85% de las llamadas a Claude son solo para descartar.

Modelo: TF-IDF + regresión logística. Liviano — el modelo es un vector de pesos,
la inferencia es en memoria, en microsegundos. Se RE-ENTRENA periódicamente: los
patrones de descarte cambian con el tiempo y un modelo lineal regularizado sobre
~1M de ejemplos se adapta sin sobreajustar.

Uso:    python3 entrenar_descarte.py
Salida: modelo_descarte.joblib  (lo carga el worker como una etapa más)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline

from db import conectar
from reglas import normalizar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("entrenar")

MODELO_PATH = Path(__file__).resolve().parent / "modelo_descarte.joblib"

# Muestra por tabla (filas recientes; el ORDER BY id usa la PK, es barato).
# Se toma toda la clase de interés y una muestra de descartes — class_weight
# balanceado compensa el desbalance restante.
LIM_INTERES = 250_000
LIM_DESCARTE = 350_000

_SQL = """
SELECT Descripcion FROM `{tabla}`
WHERE estado_gestor = %s
  AND nombre_clasificador IS NOT NULL
  AND nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_)'
  AND Descripcion IS NOT NULL AND CHAR_LENGTH(Descripcion) >= 8
ORDER BY id DESC
LIMIT %s
"""


def cargar_datos() -> "tuple[list, list]":
    conn = conectar()
    textos: list[str] = []
    etiquetas: list[int] = []
    try:
        for tabla in ("compra_agil", "Licitaciones_diarias"):
            for estado, lim in ((1, LIM_INTERES), (0, LIM_DESCARTE)):
                with conn.cursor() as cur:
                    cur.execute(_SQL.format(tabla=tabla), (estado, lim))
                    filas = cur.fetchall()
                for r in filas:
                    textos.append(r["Descripcion"])
                    etiquetas.append(estado)
                log.info("%s estado_gestor=%s: %d filas", tabla, estado, len(filas))
    finally:
        conn.close()
    return textos, etiquetas


def main() -> None:
    t0 = time.time()
    textos, y = cargar_datos()
    y = np.array(y)
    log.info("Total %d filas (interés=%d, descarte=%d)", len(y), int(y.sum()), int((y == 0).sum()))

    X_tr, X_te, y_tr, y_te = train_test_split(
        textos, y, test_size=0.15, random_state=42, stratify=y
    )
    pipe = Pipeline([
        ("tfidf", TfidfVectorizer(
            preprocessor=normalizar, ngram_range=(1, 2),
            min_df=3, max_features=120_000, sublinear_tf=True,
        )),
        ("clf", LogisticRegression(class_weight="balanced", max_iter=1000, C=4.0)),
    ])
    log.info("Entrenando TF-IDF + regresión logística...")
    pipe.fit(X_tr, y_tr)

    # Evaluación. clase 0 = descarte. Solo se auto-descarta sobre un umbral alto;
    # el dato crítico es el FALSO NEGATIVO: una fila de interés auto-descartada.
    idx0 = list(pipe.classes_).index(0)
    prob_desc = pipe.predict_proba(X_te)[:, idx0]
    y_te = np.array(y_te)
    total_desc = int((y_te == 0).sum())
    print("\n=== Evaluación en test (%d filas) ===" % len(y_te))
    for umbral in (0.90, 0.95, 0.97, 0.99):
        auto = prob_desc >= umbral
        n_auto = int(auto.sum())
        if not n_auto:
            print(f"  umbral {umbral}: nada supera el umbral")
            continue
        fn = int(((y_te == 1) & auto).sum())          # interés auto-descartado = peligroso
        cubre = int(((y_te == 0) & auto).sum())
        print(f"  umbral {umbral}: auto-descarta {n_auto:>6} | "
              f"FALSOS NEGATIVOS {fn} ({fn / n_auto * 100:.3f}%) | "
              f"cubre {cubre}/{total_desc} descartes ({cubre / total_desc * 100:.0f}%)")

    joblib.dump(pipe, MODELO_PATH)
    log.info("Modelo guardado en %s — %.0fs total", MODELO_PATH.name, time.time() - t0)


if __name__ == "__main__":
    main()
