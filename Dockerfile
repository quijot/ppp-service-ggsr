FROM python:3.11-slim

# Dependencias del sistema necesarias para compilar algunas dependencias Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar dependencias Python primero (aprovecha caché de Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código de la aplicación
COPY app/ ./app/

# Copiar módulos de cálculo geodésico.
# IMPORTANTE: los archivos .pickle (ramsac, iws, sws) NO están en el repo.
# Deben subirse por separado a Railway (ver README → Deploy en Railway).
COPY ppp/ ./ppp/

# Directorio para archivos temporales del pipeline RINEX (worker)
RUN mkdir -p /tmp/ppp_results

# Variables de entorno por defecto (se sobreescriben en Railway)
ENV PYTHONUNBUFFERED=1 \
    PPP_DIR=/app/ppp \
    RESULTS_DIR=/tmp/ppp_results

# Puerto expuesto para el web server
EXPOSE 8000

# Comando por defecto: servidor web
# El worker Celery se configura como servicio separado en Railway (ver Procfile / railway.toml)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
