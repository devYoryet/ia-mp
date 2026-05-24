#!/usr/bin/env python3
"""Entrena el clasificador MULTICLASE de pactivo.

Idea: para cada pactivo del catálogo, unir TODAS las glosas que históricamente
quedaron clasificadas con ese pactivo (en varias fuentes), y entrenar un modelo
que infiera el pactivo desde el texto — no por match exacto, sino por
componentes léxicos compartidos.

Cubre el caso que falta hoy: una glosa nueva escrita DISTINTO pero que comparte
palabras/raíces/marcas con las históricas de un pactivo, queda atrapada acá sin
gastar Claude. Lo que es match EXACTO ya lo resuelve `cruce_base`/`histórico`;
lo inequívoco por palabra suelta lo resuelve `regla_diccionario`; este modelo
es la capa "soft" que captura el resto antes de Claude.

Fuentes (todas en clásico):
  - 0001_td_oc.Base       EspComprador + EspProveedor → Pactivo
  - analisis_precios.Base EspComprador + Esp_Proveedores → Pactivo
  - compra_agil           Descripcion (estado_gestor=1) → pactivo
  - Licitaciones_diarias  Descripcion (estado_gestor=1) → pactivo

Modelo: TF-IDF (palabras + char n-gramas) + SGDClassifier(loss=log_loss).
Inferencia: ~5ms por fila, en memoria, sin API.

Uso:   python3 entrenar_pactivo.py [--max-por-pactivo N]
Salida: modelo_pactivo.joblib  (lo carga el worker en la cascada)
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
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import SGDClassifier
from sklearn.model_selection import train_test_split
from sklearn.pipeline import FeatureUnion, Pipeline

from config import config
from reglas import normalizar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("entrenar_pactivo")

MODELO_PATH = Path(__file__).resolve().parent / "modelo_pactivo.joblib"

# Cada fuente declara: (database, tabla, columna_label, columnas_texto, where).
# Las dos Bases aportan dos textos por fila (comprador + proveedor). Las dos
# tablas operativas (compra_agil/Licitaciones_diarias) viven en la base
# principal del worker.
_FUENTES = [
    {
        "db": "0001_td_oc",
        "tabla": "Base",
        "label": "Pactivo",
        "textos": ["EspComprador", "EspProveedor"],
        "where": "Pactivo IS NOT NULL AND Pactivo <> ''",
    },
    {
        "db": "analisis_precios",
        "tabla": "Base",
        "label": "Pactivo",
        "textos": ["EspComprador", "Esp_Proveedores"],
        "where": "Pactivo IS NOT NULL AND Pactivo <> ''",
    },
    {
        "db": "licitaciones_diarias_total_farma",
        "tabla": "compra_agil",
        "label": "pactivo",
        "textos": ["Descripcion"],
        "where": (
            "estado_gestor=1 AND pactivo IS NOT NULL AND pactivo <> '' "
            "AND nombre_clasificador IS NOT NULL "
            "AND nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_)'"
        ),
    },
    {
        "db": "licitaciones_diarias_total_farma",
        "tabla": "Licitaciones_diarias",
        "label": "pactivo",
        "textos": ["Descripcion"],
        "where": (
            "estado_gestor=1 AND pactivo IS NOT NULL AND pactivo <> '' "
            "AND nombre_clasificador IS NOT NULL "
            "AND nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_)'"
        ),
    },
]

MIN_EJ_POR_PACTIVO = 5     # debajo de eso un pactivo no se aprende, lo retira
LARGO_MIN_TEXTO = 8        # textos muy cortos son ambiguos y sumar ruido

# Tope duro de filas leídas por fuente — protege ante tablas de 1M+ y permite
# muestreos rápidos. El sampler por pactivo (en Python) actúa por encima.
LIM_FILAS_FUENTE = 2_000_000


def _conn_streaming(db_name: str):
    """Conexión con cursor server-side (streaming). Para tablas grandes:
    el cursor por defecto de PyMySQL bufferea TODO en memoria del cliente
    antes del primer fetch — con 920K filas eso es minutos perdidos."""
    return pymysql.connect(
        host=config.db_host, port=config.db_port,
        user=config.db_user, password=config.db_password,
        database=db_name, charset="utf8mb4",
        connect_timeout=15, read_timeout=300,
        cursorclass=SSDictCursor,
    )


def cargar_pares(max_por_pactivo: int) -> tuple[list[str], list[str]]:
    """Recolecta (texto, pactivo) de las 4 fuentes, con un tope por pactivo por
    fuente para no dejar que una clase muy frecuente domine al modelo."""
    por_pactivo: dict[str, list[str]] = defaultdict(list)

    for f in _FUENTES:
        cols = ", ".join(f"`{c}`" for c in f["textos"])
        sql = (
            f"SELECT {cols}, `{f['label']}` AS _lab "
            f"FROM `{f['tabla']}` WHERE {f['where']} "
            f"ORDER BY id DESC LIMIT {LIM_FILAS_FUENTE}"
        )
        conn = _conn_streaming(f["db"])
        contado_fuente: Counter = Counter()
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
                        pact = (r["_lab"] or "").strip()
                        if not pact:
                            continue
                        # tope por pactivo, ponderado por nº de columnas de texto
                        if contado_fuente[pact] >= max_por_pactivo * len(f["textos"]):
                            continue
                        for col in f["textos"]:
                            texto = (r[col] or "").strip()
                            if len(texto) < LARGO_MIN_TEXTO:
                                continue
                            por_pactivo[pact].append(texto)
                            contado_fuente[pact] += 1
        finally:
            conn.close()
        log.info(
            "Fuente %s.%s: %d filas leídas, %d pactivos, %d ejemplos sumados (%.0fs)",
            f["db"], f["tabla"], leidas, len(contado_fuente),
            sum(contado_fuente.values()), time.time() - t0,
        )

    # Filtra pactivos con muy pocos ejemplos — no se aprenden y aportan ruido.
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
    """Top-1 accuracy a varios umbrales de confianza. El uso real es:
    el worker solo asigna el pactivo si predict_proba >= UMBRAL — esa cifra
    define cobertura y precisión por banda."""
    proba = modelo.predict_proba(X_te)
    pred = modelo.classes_[proba.argmax(axis=1)]
    pmax = proba.max(axis=1)
    print(f"\n=== Evaluación en test ({len(y_te)} filas, "
          f"{len(set(y_te))} pactivos) ===")
    acc_global = (pred == y_te).mean()
    print(f"  Acierto top-1 global       : {acc_global * 100:.2f}%")
    for umbral in (0.40, 0.50, 0.60, 0.70, 0.80, 0.90):
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
    parser.add_argument("--max-por-pactivo", type=int, default=300,
                        help="ejemplos máximos por pactivo POR FUENTE (default 300)")
    args = parser.parse_args()

    t0 = time.time()
    textos, etiquetas = cargar_pares(args.max_por_pactivo)
    y = np.array(etiquetas)

    X_tr, X_te, y_tr, y_te = train_test_split(
        textos, y, test_size=0.10, random_state=42, stratify=y
    )

    # Dos vectorizadores en paralelo: palabras y char n-gramas. Los char n-gramas
    # son robustos a errores de tipeo y a marcas pegadas a la dosis ("PARAC500").
    vec = FeatureUnion([
        ("w", TfidfVectorizer(
            preprocessor=normalizar, ngram_range=(1, 2),
            min_df=3, max_features=60_000, sublinear_tf=True,
        )),
        ("c", TfidfVectorizer(
            preprocessor=normalizar, analyzer="char_wb", ngram_range=(3, 5),
            min_df=3, max_features=40_000, sublinear_tf=True,
        )),
    ])
    # SGD con loss log_loss da scores logísticos; los calibramos a probabilidades
    # con sigmoid + cross-validation barato (3 folds), para que predict_proba sea
    # interpretable como confianza y comparable entre clases.
    # n_jobs=1 deliberado: el CalibratedClassifierCV(cv=3) con n_jobs=-1 sobre
    # 1.6K clases × 100K features se va a swap y el OOM killer corta. Secuencial
    # tarda ~2-3x más pero entra cómodo en 4-5 GB. Probado en gestor_oc (2 cores,
    # 7.8GB RAM): paralelo crashea con SIGKILL; secuencial NO.
    base = SGDClassifier(
        loss="log_loss", alpha=1e-5, max_iter=20,
        class_weight="balanced", n_jobs=1, random_state=42,
    )
    clf = CalibratedClassifierCV(base, method="sigmoid", cv=3, n_jobs=1)

    pipe = Pipeline([("vec", vec), ("clf", clf)])

    log.info("Entrenando TF-IDF (word+char) + SGD calibrado...")
    pipe.fit(X_tr, y_tr)

    evaluar(pipe, X_te, y_te)

    _optimizar(pipe)
    joblib.dump(pipe, MODELO_PATH, compress=3)
    tam_mb = MODELO_PATH.stat().st_size / 1e6
    log.info("Modelo guardado en %s (%.0f MB, float32+compress) — %.0fs total",
             MODELO_PATH.name, tam_mb, time.time() - t0)


def _optimizar(modelo) -> None:
    """Reduce el tamaño del .joblib sin tocar la accuracy:
    - los pesos del SGD subyacente bajan de float64 a float32 (la inferencia
      lineal no necesita doble precisión);
    - el `joblib.dump(..., compress=3)` comprime al guardar (las matrices de
      pesos del SGD calibrado son altamente compresibles).
    De 3.8 GB → 1.6 GB (~57% menos) con los 5 sanity tests intactos."""
    if not hasattr(modelo, "named_steps") or "clf" not in modelo.named_steps:
        return
    clf = modelo.named_steps["clf"]
    if not hasattr(clf, "calibrated_classifiers_"):
        return
    for cc in clf.calibrated_classifiers_:
        for inner in (getattr(cc, "estimator", None),
                      getattr(cc, "base_estimator", None)):
            if inner is None:
                continue
            if hasattr(inner, "coef_") and inner.coef_.dtype == np.float64:
                inner.coef_ = inner.coef_.astype(np.float32)
            if hasattr(inner, "intercept_") and inner.intercept_.dtype == np.float64:
                inner.intercept_ = inner.intercept_.astype(np.float32)


if __name__ == "__main__":
    main()
