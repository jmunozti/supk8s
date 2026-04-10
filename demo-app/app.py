"""Demo app with configurable failure rate for testing supK8s agent."""

import logging
import os
import random
import time

from fastapi import FastAPI, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Prometheus metrics (module-level to avoid duplicate registration)
REQUEST_COUNT = Counter("http_requests_total", "Total requests", ["method", "endpoint", "status"])
REQUEST_LATENCY = Histogram("http_request_duration_seconds", "Request latency", ["endpoint"])


class DemoApp:
    """FastAPI app that simulates failures at a configurable rate."""

    def __init__(self, failure_rate: int = 0, version: str = "v1"):
        self.failure_rate = failure_rate
        self.version = version
        self.logger = logging.getLogger("demo-app")
        self.app = FastAPI(title=f"demo-app {self.version}")
        self._register_routes()

    def _should_fail(self) -> bool:
        return random.randint(1, 100) <= self.failure_rate

    def _register_routes(self) -> None:
        @self.app.get("/")
        def root():
            return self._handle_root()

        @self.app.get("/healthz")
        def healthz():
            return self._handle_healthz()

        @self.app.get("/metrics")
        def metrics():
            return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    def _handle_root(self):
        start = time.time()

        if self._should_fail():
            REQUEST_COUNT.labels(method="GET", endpoint="/", status="500").inc()
            REQUEST_LATENCY.labels(endpoint="/").observe(time.time() - start)
            self.logger.error("Simulated failure (version=%s, failure_rate=%d%%)", self.version, self.failure_rate)
            return Response(content='{"error": "internal server error"}', status_code=500)

        REQUEST_COUNT.labels(method="GET", endpoint="/", status="200").inc()
        REQUEST_LATENCY.labels(endpoint="/").observe(time.time() - start)
        return {"status": "ok", "version": self.version}

    def _handle_healthz(self):
        if self._should_fail():
            return Response(content='{"status": "unhealthy"}', status_code=503)
        return {"status": "healthy", "version": self.version}


# Create app instance from environment
FAILURE_RATE = int(os.environ.get("FAILURE_RATE", "0"))
VERSION = os.environ.get("APP_VERSION", "v1")

demo = DemoApp(failure_rate=FAILURE_RATE, version=VERSION)
app = demo.app
