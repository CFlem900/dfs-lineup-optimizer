# ── Build stage: install Python dependencies ──────────────────────
FROM python:3.13-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ── Production stage: lean image with non-root user ───────────────
FROM python:3.13-slim

RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /root/.local /home/appuser/.local
ENV PATH=/home/appuser/.local/bin:$PATH

# Copy application code (respects .dockerignore)
COPY --chown=appuser:appuser . .

# Create cache directory with proper permissions
RUN mkdir -p /app/cache && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# NOTE: With --workers N, each worker is a separate process.
# Set SCHEDULER_ENABLED=false on extra workers to prevent duplicate
# cron jobs.  For single-worker dev mode, scheduler runs by default.
# For production, use docker-compose to run a dedicated scheduler
# worker alongside request workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
