from unittest.mock import MagicMock, patch

import pytest

from agent import PrometheusClient, KubernetesController, SupK8sAgent, LLMAnalyzer


class TestPrometheusClient:
    def setup_method(self):
        self.client = PrometheusClient(url="http://localhost:9090")

    @patch("agent.requests.get")
    def test_query_returns_value(self, mock_get):
        mock_get.return_value = MagicMock(
            json=lambda: {"status": "success", "data": {"result": [{"value": [1234, "0.5"]}]}},
        )
        assert self.client.query("up") == 0.5

    @patch("agent.requests.get")
    def test_query_returns_zero_on_empty(self, mock_get):
        mock_get.return_value = MagicMock(
            json=lambda: {"status": "success", "data": {"result": []}},
        )
        assert self.client.query("up") == 0.0

    @patch("agent.requests.get")
    def test_query_returns_zero_on_error(self, mock_get):
        mock_get.side_effect = Exception("connection refused")
        assert self.client.query("up") == 0.0

    @patch.object(PrometheusClient, "query")
    def test_error_rate_from_http(self, mock_query):
        mock_query.side_effect = [0.0, 3.0, 10.0, 0.0]
        self.client._last_restarts = 0.0
        assert self.client.get_error_rate("demo", "demo-app") == 0.3

    @patch.object(PrometheusClient, "query")
    def test_error_rate_zero_traffic(self, mock_query):
        mock_query.side_effect = [0.0, 0.0, 0.0, 0.0]
        self.client._last_restarts = 0.0
        assert self.client.get_error_rate("demo", "demo-app") == 0.0

    @patch.object(PrometheusClient, "query")
    def test_detects_pod_restarts(self, mock_query):
        mock_query.side_effect = [5.0, 0.0, 0.0, 0.0]
        self.client._last_restarts = 3.0
        assert self.client.get_error_rate("demo", "demo-app") == 1.0

    @patch.object(PrometheusClient, "query")
    def test_detects_unavailable_replicas(self, mock_query):
        mock_query.side_effect = [0.0, 0.0, 0.0, 1.0]
        self.client._last_restarts = 0.0
        assert self.client.get_error_rate("demo", "demo-app") == 1.0

    @patch("agent.time.sleep", return_value=None)
    @patch("agent.requests.get")
    def test_wait_until_ready_succeeds(self, mock_get, _sleep):
        mock_get.return_value = MagicMock(status_code=200)
        assert self.client.wait_until_ready(timeout=5) is True
        assert self.client.ready is True

    @patch("agent.time.sleep", return_value=None)
    @patch("agent.time.time")
    @patch("agent.requests.get")
    def test_wait_until_ready_times_out(self, mock_get, mock_time, _sleep):
        mock_get.side_effect = Exception("connection refused")
        # First call sets deadline, then advance past it
        mock_time.side_effect = [0, 1, 2, 999]
        assert self.client.wait_until_ready(timeout=2) is False
        assert self.client.ready is False

    @patch("agent.requests.get")
    def test_query_warmup_suppresses_warnings(self, mock_get):
        """During warmup (ready=False), connection errors should NOT log as WARNING."""
        mock_get.side_effect = Exception("connection refused")
        with patch.object(self.client.logger, "warning") as mock_warn, \
             patch.object(self.client.logger, "debug") as mock_debug:
            self.client.ready = False
            self.client.query("up")
            mock_warn.assert_not_called()
            mock_debug.assert_called_once()

            mock_warn.reset_mock()
            mock_debug.reset_mock()
            self.client.ready = True
            self.client.query("up")
            mock_warn.assert_called_once()
            mock_debug.assert_not_called()


class TestKubernetesController:
    def test_init(self):
        k8s = KubernetesController(namespace="demo", deployment="demo-app")
        assert k8s.namespace == "demo"
        assert k8s.deployment == "demo-app"


class TestSupK8sAgent:
    def setup_method(self):
        self.prometheus = MagicMock(spec=PrometheusClient)
        self.k8s = MagicMock(spec=KubernetesController)
        self.k8s.namespace = "demo"
        self.k8s.deployment = "demo-app"
        self.llm = MagicMock(spec=LLMAnalyzer)
        self.llm.analyze.return_value = "Root cause: container crash"

        self.agent = SupK8sAgent(
            prometheus=self.prometheus,
            k8s=self.k8s,
            llm=self.llm,
            error_threshold=0.3,
            check_interval=5,
            rollback_image="demo-app:v1",
            cooldown_seconds=60,
        )

    def test_no_rollback_when_healthy(self):
        self.prometheus.get_error_rate.return_value = 0.1
        self.agent.check()
        self.k8s.rollback.assert_not_called()
        self.llm.analyze.assert_not_called()

    def test_rollback_when_threshold_exceeded(self):
        self.prometheus.get_error_rate.return_value = 0.8
        self.k8s.get_logs.return_value = "error logs"
        self.agent.check()
        self.k8s.rollback.assert_called_once_with("demo-app:v1")

    def test_cooldown_prevents_double_rollback(self):
        self.prometheus.get_error_rate.return_value = 0.8
        self.k8s.get_logs.return_value = "error logs"
        self.agent.check()
        self.agent.check()
        assert self.k8s.rollback.call_count == 1

    def test_not_in_cooldown_initially(self):
        assert not self.agent.is_in_cooldown()

    def test_llm_called_on_incident(self):
        self.prometheus.get_error_rate.return_value = 0.8
        self.k8s.get_logs.return_value = "panic: ..."
        self.agent.check()
        self.llm.analyze.assert_called_once_with("panic: ...", "demo", "demo-app")
        self.k8s.rollback.assert_called_once_with("demo-app:v1")


class TestLLMAnalyzer:
    def test_raises_without_api_key(self):
        with pytest.raises(ValueError, match="OPENROUTER_API_KEY is required"):
            LLMAnalyzer(api_key="", model="any-model")

    def test_raises_without_model(self):
        with pytest.raises(ValueError, match="OPENROUTER_MODEL is required"):
            LLMAnalyzer(api_key="sk-test", model="")

    def test_initializes_when_key_and_model_provided(self):
        analyzer = LLMAnalyzer(api_key="sk-test", model="deepseek/deepseek-chat-v3:free")
        assert analyzer.model == "deepseek/deepseek-chat-v3:free"

    def test_custom_model_and_base_url(self):
        analyzer = LLMAnalyzer(
            api_key="sk-test",
            model="meta-llama/llama-3.3-70b-instruct:free",
            base_url="https://custom.example.com/v1",
        )
        assert analyzer.model == "meta-llama/llama-3.3-70b-instruct:free"

    def test_analyze_returns_llm_response(self):
        analyzer = LLMAnalyzer(api_key="sk-test", model="deepseek/deepseek-chat-v3:free")
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="Likely OOMKilled"))]
        analyzer.client = MagicMock()
        analyzer.client.chat.completions.create.return_value = mock_response
        result = analyzer.analyze("logs", "demo", "demo-app")
        assert result == "Likely OOMKilled"

    def test_analyze_handles_exception(self):
        analyzer = LLMAnalyzer(api_key="sk-test", model="deepseek/deepseek-chat-v3:free")
        analyzer.client = MagicMock()
        analyzer.client.chat.completions.create.side_effect = Exception("rate limited")
        result = analyzer.analyze("logs", "demo", "demo-app")
        assert "LLM analysis failed" in result
