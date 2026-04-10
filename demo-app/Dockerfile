# Multi-stage build for smaller image
FROM python:3.12-slim AS builder

WORKDIR /app
RUN pip install --prefix=/install --no-cache-dir fastapi uvicorn prometheus-client

FROM python:3.12-slim

ARG FAILURE_RATE=0

WORKDIR /app

COPY --from=builder /install /usr/local
COPY app.py .

ENV FAILURE_RATE=${FAILURE_RATE}
ENV APP_VERSION=v1

RUN useradd --create-home --no-log-init app && chown -R app:app /app
USER app

HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
