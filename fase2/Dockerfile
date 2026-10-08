# Imagen PROPIA de la Fase 2 (clasificador-f2). No usa ni modifica la imagen
# clasificador-ia del worker/panel. Contexto de build = esta carpeta (fase2/).
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m fase2 && chown -R fase2 /app
USER fase2

CMD ["python", "servicio.py"]
