FROM python:3.11-slim

# Dependencias del sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código
COPY app/        ./app/
COPY ppp/        ./ppp/

# Crear directorios temporales
RUN mkdir -p /tmp/ppp_uploads /tmp/ppp_results

# Variables de entorno por defecto (se sobreescriben en Railway)
ENV PYTHONUNBUFFERED=1 \
    PPP_DIR=/app/ppp \
    UPLOAD_DIR=/tmp/ppp_uploads \
    RESULTS_DIR=/tmp/ppp_results

# Puerto expuesto para el web server
EXPOSE 8000

# Comando por defecto: web server
# El worker se lanza por separado (ver Procfile / railway.toml)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
