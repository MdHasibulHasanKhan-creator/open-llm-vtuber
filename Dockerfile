# ---------------------------------------------------------------------------
#  Open VRM VTuber - single image (frontend build + FastAPI runtime).
#
#    docker build -t open-vrm-vtuber .
#    docker run --rm -p 8000:8000 --env-file backend/.env open-vrm-vtuber
#
#  The resulting container serves the UI, the /api routes and the /ws socket on
#  one port, exactly like `python main.py` does locally. Point it at an LLM it
#  can reach: on the host that is http://host.docker.internal:11434/v1, next to
#  it in a compose network it is http://ollama:11434/v1 (see docker-compose.yml).
# ---------------------------------------------------------------------------

# ---- stage 1: build the web client ----------------------------------------
FROM node:20-alpine AS frontend
WORKDIR /app/frontend
# Lockfile first so a source-only change does not reinstall dependencies.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- stage 2: python runtime ----------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app/backend

# Dependencies first (cached until requirements.txt changes).
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Optional: offline speech recognition. Uncomment to bake faster-whisper in
# (adds ~1 GB and downloads model weights on first use).
# COPY backend/requirements-local.txt ./
# RUN pip install --no-cache-dir -r requirements-local.txt

COPY backend/ ./
# The backend looks for ../frontend/dist relative to backend/, so keep the
# same layout as a local checkout.
COPY --from=frontend /app/frontend/dist /app/frontend/dist

# Inside a container we must bind every interface, and the frontend is the point.
ENV VTUBER_SERVER__HOST=0.0.0.0 \
    VTUBER_SERVER__PORT=8000 \
    VTUBER_SERVER__SERVE_FRONTEND=true

EXPOSE 8000

# One worker per container: each uvicorn worker would own its own sessions and
# engine instances, and sessions are in-memory by design.
CMD ["python", "main.py"]
