# Multi-stage production Dockerfile for NeuralGateway
# Stage 1: Compilation and dependency resolution
FROM python:3.11-slim as builder

WORKDIR /build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Install dependencies into /root/.local to copy into runner
RUN pip install --no-cache-dir --user -r requirements.txt

# Stage 2: Minimal hardened non-root runtime
FROM python:3.11-slim as runner

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/appuser/.local/bin:$PATH"

# Create non-root system group and user
RUN groupadd -g 10001 appgroup && \
    useradd -u 10000 -g appgroup -m -s /bin/bash appuser

# Copy prebuilt python wheels
COPY --from=builder /root/.local /home/appuser/.local

# Copy application codebase with ownership
COPY --chown=appuser:appgroup app /app/app

# Drop root privileges
USER appuser

EXPOSE 8000

# Container runtime healthcheck
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').getcode() == 200 else 1)" || exit 1

# Launch high-throughput ASGI worker pool
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
