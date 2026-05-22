# Imagen del Clasificador IA — sirve para los dos servicios (worker y panel).
# El contenedor trae su propio Python 3.12: no depende del Python viejo del host.
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Dependencias primero (mejor uso de la caché de capas).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código del proyecto.
COPY . .

# Usuario no-root.
RUN useradd -m clasificador && chown -R clasificador /app
USER clasificador

# Por defecto el worker; docker-compose sobreescribe el comando por servicio.
CMD ["python", "worker.py"]
