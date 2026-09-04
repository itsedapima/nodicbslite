# ══════════════════════════════════════════════════════════════════════════════
#  Stage 1 — Build wheels
# ══════════════════════════════════════════════════════════════════════════════
FROM python:3.12-slim AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    gcc \
    pkg-config \
    libcairo2-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip
COPY requirements.txt .
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /app/wheels -r requirements.txt

# ══════════════════════════════════════════════════════════════════════════════
#  Stage 2 — Runtime
# ══════════════════════════════════════════════════════════════════════════════
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=1

# Runtime system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    libcairo2 \
    libpango-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    postgresql-client \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages from pre-built wheels
COPY --from=builder /app/wheels /tmp/wheels
COPY --from=builder /app/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir /tmp/wheels/* \
    && rm -rf /tmp/wheels

# Copy project source
COPY . .

# Prepare volume mount-points
RUN mkdir -p /app/staticfiles /app/mediafiles

# Fix Windows line endings + make executable
COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
