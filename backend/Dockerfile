# Stage 1: build the React UI.
FROM node:20-alpine AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
ARG VITE_API_KEY
ENV VITE_API_KEY=$VITE_API_KEY
RUN npm run build

# Stage 2: Flask middleware serving the built UI + API.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /srv
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --shell /usr/sbin/nologin appuser
COPY backend/app.py .
COPY --from=ui /ui/dist /frontend/dist
COPY data/football.duckdb /data/football.duckdb
USER appuser
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--worker-class", "gthread", "--timeout", "300", "app:app"]
