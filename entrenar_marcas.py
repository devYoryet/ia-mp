#!/usr/bin/env python3
"""Entrena el clasificador de pactivo POR CONTEXTO (marcas + título + vínculos).

Complementario a `modelo_pactivo`: este especializa en los casos donde la
descripción NO contiene literal el nombre del pactivo (es decir, viene como
MARCA comercial — Acerdil, Bramedil, Eutirox, Micardis, Cardioplus, etc.) y el
pactivo se infiere del título del tender + de las pistas léxicas del producto.

Idea: para cada pactivo del catálogo, recoger las glosas históricas donde el
HUMANO clasificó ese pactivo PERO la descripción NO menciona literal el
pactivo. Eso fuerza al modelo a aprender marcas/contexto, no match exacto.

Diferencias clave con `modelo_pactivo`:
  - Solo fuentes operativas (compra_agil + Licitaciones_diarias) — tienen
    Título + Descripción + VINCULOS.
  - Texto = TÍTULO + ' ::: ' + DESCRIPCION + ' ::: ' + VINCULOS.
  - Filtro: descartar muestras donde normalizar(pactivo) aparece en
    normalizar(descripcion). Eso deja SOLO marcas y contexto puro.
  - Modelo más COMPACTO (max_features 10K+10K, max_por_pactivo 80, n_iter 20).
    Joblib resultante ~50-80 MB (vs 537 MB de modelo_pactivo). Bajo costo de
    inferencia (~3ms/fila).

Uso:    python3 entrenar_marcas.py [--max-por-pactivo N]
Salida: modelo_marcas.joblib  (lo carga el worker, rama nueva en la cascada)
"""

from __future__ import annotations

import argparse
import logging
import time
from collections import Counter, defaultdict
from pathlib import Path

import joblib
import numpy as np
import pymysql
from pymysql.cursors import SSDictCursor
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import SGDClassifier
from sklearn.model_selection import train_test_split
from sklearn.pipeline import FeatureUnion, Pipeline

from config import config
from reglas import normalizar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("entrenar_marcas")

MODELO_PATH = Path(__file__).resolve().parent / "modelo_marcas.joblib"

# Fuentes operativas SOLAMENTE — necesitamos Titulo + VINCULOS para contexto.
_FUENTES = [
    {
        "db": "licitaciones_diarias_total_farma",
        "tabla": "compra_agil",
        "where": (
            "estado_gestor=1 AND pactivo IS NOT NULL AND pactivo <> '' "
            "AND nombre_clasificador IS NOT NULL "
            "AND nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_)'"
        ),
    },
    {
        "db": "licitaciones_diarias_total_farma",
        "tabla": "Licitaciones_diarias",
        "where": (
            "estado_gestor=1 AND pactivo IS NOT NULL AND pactivo <> '' "
            "AND nombre_clasificador IS NOT NULL "
            "AND nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_)'"
        ),
    },
]

MIN_EJ_POR_PACTIVO = 8       # más alto que modelo_pactivo: aquí queremos señal clara
LARGO_MIN_TEXTO = 12
LIM_FILAS_FUENTE = 800_000   # cada operativa es más chica que las Base

_SEPARADOR = " ::: "


def _conn_streaming(db_name: str):
    return pymysql.connect(
        host=config.db_host, port=config.db_port,
        user=config.db_user, password=config.db_password,
        database=db_name, charset="utf8mb4",
        connect_timeout=15, read_timeout=300,
        cursorclass=SSDictCursor,
    )


def _pactivo_en_descripcion(pactivo: str, descripcion: str) -> bool:
    """¿El nombre del pactivo aparece como token en la descripción? Si sí,
    descartamos la muestra: aquí queremos casos donde la descripción NO menciona
    el principio activo y el pactivo se deduce del contexto/marca."""
    p = normalizar(pactivo)
    d = normalizar(descripcion or "")
    if not p or not d:
        return False
    # Para compuestos A-B-C: si CUALQUIERA de los componentes aparece, ya hay
    # match directo (no es caso de marca pura).
    componentes = [c.strip() for c in p.replace("+", "-").split("-") if c.strip()]
    return any(c in d for c in componentes)


def cargar_pares(max_por_pactivo: int) -> tuple[list[str], list[str]]:
    """Recolecta (texto_concatenado, pactivo) FILTRANDO los casos donde el
    pactivo ya aparece en la descripción (esos los resuelve modelo_pactivo o
    regla_diccionario)."""
    por_pactivo: dict[str, list[str]] = defaultdict(list)
    total_leidas = 0
    total_descartadas_por_match = 0

    for f in _FUENTES:
        sql = (
            f"SELECT Titulo, Descripcion, VINCULOS, pactivo "
            f"FROM `{f['tabla']}` WHERE {f['where']} "
            f"ORDER BY id DESC LIMIT {LIM_FILAS_FUENTE}"
        )
        conn = _conn_streaming(f["db"])
        contado_fuente: Counter = Counter()
        descartadas_fuente = 0
        leidas = 0
        t0 = time.time()
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                while True:
                    lote = cur.fetchmany(5000)
                    if not lote:
                        break
                    for r in lote:
                        leidas += 1
                        pact = (r["pactivo"] or "").strip()
                        if not pact:
                            continue
                        desc = (r["Descripcion"] or "").strip()
                        if len(desc) < LARGO_MIN_TEXTO:
                            continue
                        # FILTRO CLAVE: descartar si el pactivo aparece literal
                        if _pactivo_en_descripcion(pact, desc):
                            descartadas_fuente += 1
                            continue
                        # tope por pactivo
                        if contado_fuente[pact] >= max_por_pactivo:
                            continue
                        # Texto concatenado: Título + Descripción + VINCULOS
                        tit = (r["Titulo"] or "").strip()
                        vin = (r["VINCULOS"] or "").strip()[:400]
                        texto = _SEPARADOR.join(t for t in (tit, desc, vin) if t)
                        por_pactivo[pact].append(texto)
                        contado_fuente[pact] += 1
        finally:
            conn.close()
        total_leidas += leidas
        total_descartadas_por_match += descartadas_fuente
        log.info(
            "Fuente %s.%s: %d filas leídas, %d descartadas por match directo, "
            "%d pactivos, %d ejemplos sumados (%.0fs)",
            f["db"], f["tabla"], leidas, descartadas_fuente,
            len(contado_fuente), sum(contado_fuente.values()), time.time() - t0,
        )
    log.info("TOTAL: %d filas leídas, %d descartadas por match directo "
             "(modelo_pactivo ya las cubre)",
             total_leidas, total_descartadas_por_match)

    textos: list[str] = []
    etiquetas: list[str] = []
    descartados = 0
    for pact, lista in por_pactivo.items():
        if len(lista) < MIN_EJ_POR_PACTIVO:
            descartados += 1
            continue
        for t in lista:
            textos.append(t)
            etiquetas.append(pact)
    log.info("Pactivos válidos: %d (descartados %d con <%d ejemplos). Total: %d filas",
             len(set(etiquetas)), descartados, MIN_EJ_POR_PACTIVO, len(textos))
    return textos, etiquetas


def evaluar(modelo, X_te: list[str], y_te: np.ndarray) -> None:
    proba = modelo.predict_proba(X_te)
    pred = modelo.classes_[proba.argmax(axis=1)]
    pmax = proba.max(axis=1)
    print(f"\n=== Evaluación en test ({len(y_te)} filas, "
          f"{len(set(y_te))} pactivos) ===")
    acc_global = (pred == y_te).mean()
    print(f"  Acierto top-1 global       : {acc_global * 100:.2f}%")
    for umbral in (0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90):
        mask = pmax >= umbral
        n = int(mask.sum())
        if not n:
            print(f"  umbral {umbral}: nadie")
            continue
        acc = (pred[mask] == y_te[mask]).mean()
        cobertura = n / len(y_te)
        print(f"  umbral {umbral}: cubre {cobertura * 100:5.1f}%  | "
              f"acierto {acc * 100:5.2f}%")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-por-pactivo", type=int, default=80,
                        help="ejemplos máximos por pactivo POR FUENTE (default 80)")
    args = parser.parse_args()

    t0 = time.time()
    textos, etiquetas = cargar_pares(args.max_por_pactivo)
    if len(textos) < 1000:
        log.error("Muy pocos datos para entrenar (%d). Abortar.", len(textos))
        return
    y = np.array(etiquetas)

    X_tr, X_te, y_tr, y_te = train_test_split(
        textos, y, test_size=0.10, random_state=42, stratify=y
    )

    # Modelo COMPACTO — la idea es complementar modelo_pactivo, no reemplazar.
    # Features pequeñas + n-gramas char para capturar marcas (Acerdil, Eutirox).
    vec = FeatureUnion([
        ("w", TfidfVectorizer(
            preprocessor=normalizar, ngram_range=(1, 2),
            min_df=3, max_features=15_000, sublinear_tf=True,
        )),
        ("c", TfidfVectorizer(
            preprocessor=normalizar, analyzer="char_wb", ngram_range=(3, 5),
            min_df=3, max_features=15_000, sublinear_tf=True,
        )),
    ])
    clf = SGDClassifier(
        loss="log_loss", alpha=1e-5, max_iter=20,
        class_weight="balanced", n_jobs=1, random_state=42,
    )
    pipe = Pipeline([("vec", vec), ("clf", clf)])

    log.info("Entrenando modelo_marcas (TF-IDF word+char + SGD log_loss)...")
    pipe.fit(X_tr, y_tr)

    evaluar(pipe, X_te, y_te)

    # Optimizar tamaño antes de guardar
    if hasattr(pipe.named_steps["clf"], "coef_"):
        pipe.named_steps["clf"].coef_ = pipe.named_steps["clf"].coef_.astype(np.float32)
    if hasattr(pipe.named_steps["clf"], "intercept_"):
        pipe.named_steps["clf"].intercept_ = pipe.named_steps["clf"].intercept_.astype(np.float32)

    joblib.dump(pipe, MODELO_PATH, compress=3)
    tam_mb = MODELO_PATH.stat().st_size / 1e6
    log.info("Modelo guardado en %s (%.0f MB) — %.0fs total",
             MODELO_PATH.name, tam_mb, time.time() - t0)


if __name__ == "__main__":
    main()
