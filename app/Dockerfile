# Image unique pour Cloud Run : on compile React, puis on le sert avec Flask.
# Etape 1 : compilation de l'interface React
FROM node:22-slim AS interface
WORKDIR /interface
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Etape 2 : serveur Python (API + fichiers de l'interface compilée)
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=interface /interface/dist ./static
# Cloud Run fournit la variable PORT. 1 processus, 8 fils : suffisant pour une démo,
# et un délai long car l'exécution des contrôles peut prendre plusieurs minutes.
CMD exec gunicorn --bind :${PORT:-8080} --workers 1 --threads 8 --timeout 900 "app:creer_app()"
