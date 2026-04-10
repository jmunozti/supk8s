# Multi-stage build: install deps in builder, copy to slim runtime
FROM python:3.12-slim AS builder

WORKDIR /app
RUN pip install --prefix=/install --no-cache-dir requests openai

FROM python:3.12-slim

WORKDIR /app

# Install kubectl
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -LO "https://dl.k8s.io/release/v1.29.0/bin/linux/amd64/kubectl" && \
    chmod +x kubectl && mv kubectl /usr/local/bin/ && \
    apt-get remove -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY agent.py .

RUN useradd --create-home --no-log-init app && chown -R app:app /app
USER app

CMD ["python", "-u", "agent.py"]
