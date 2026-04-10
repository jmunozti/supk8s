import os

os.environ.setdefault("FAILURE_RATE", "0")
os.environ.setdefault("APP_VERSION", "v1")

from fastapi.testclient import TestClient

from app import DemoApp, app

client = TestClient(app)


class TestRoot:
    def test_returns_ok(self):
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["version"] == "v1"


class TestHealthz:
    def test_healthy_when_no_failures(self):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"


class TestMetrics:
    def test_returns_prometheus_format(self):
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "http_requests_total" in response.text


class TestDemoAppClass:
    def test_create_with_defaults(self):
        demo = DemoApp()
        assert demo.failure_rate == 0
        assert demo.version == "v1"

    def test_create_with_custom_values(self):
        demo = DemoApp(failure_rate=50, version="v2")
        assert demo.failure_rate == 50
        assert demo.version == "v2"

    def test_should_not_fail_at_zero(self):
        demo = DemoApp(failure_rate=0)
        assert not demo._should_fail()

    def test_should_always_fail_at_100(self):
        demo = DemoApp(failure_rate=100)
        assert demo._should_fail()
