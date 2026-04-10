"""
supK8s agent — auto-rollback with AI-powered log analysis

Monitors Prometheus for pod restarts, unavailable replicas, and HTTP error rates.
When errors exceed threshold, asks an LLM (via OpenRouter) to analyze logs,
then triggers automatic rollback.
"""

import logging
import os
import subprocess
import time

import requests
from openai import OpenAI


class PrometheusClient:
    """Queries Prometheus for metrics."""

    def __init__(self, url: str):
        self.url = url
        self.logger = logging.getLogger("prometheus-client")
        self.ready = False

    def wait_until_ready(self, timeout: int = 120) -> bool:
        """Poll /-/ready with exponential backoff. Logs once at INFO, not WARNING per attempt.

        Returns True when Prometheus responds, False on timeout. The agent should still
        run on timeout (Prometheus may come up later); subsequent query() calls will
        log warnings as usual.
        """
        deadline = time.time() + timeout
        delay = 1.0
        attempt = 0
        self.logger.info("Waiting for Prometheus at %s ...", self.url)
        while time.time() < deadline:
            attempt += 1
            try:
                res = requests.get(f"{self.url}/-/ready", timeout=3)
                if res.status_code == 200:
                    self.logger.info("Prometheus ready (after %d attempt(s))", attempt)
                    self.ready = True
                    return True
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 10.0)
        self.logger.warning("Prometheus did not become ready within %ds — continuing anyway", timeout)
        return False

    def query(self, promql: str) -> float:
        try:
            res = requests.get(f"{self.url}/api/v1/query", params={"query": promql}, timeout=5)
            data = res.json()
            if data["status"] == "success" and data["data"]["result"]:
                return float(data["data"]["result"][0]["value"][1])
        except Exception as e:
            # During warmup, suppress noisy connection errors
            if self.ready:
                self.logger.warning("Query failed: %s", e)
            else:
                self.logger.debug("Query failed (warmup): %s", e)
        return 0.0

    def get_error_rate(self, namespace: str, deployment: str) -> float:
        """Check pod restarts + HTTP error rate. Returns highest signal."""
        restarts = self.query(
            f'sum(kube_pod_container_status_restarts_total{{namespace="{namespace}",pod=~"{deployment}.*"}})'
        )
        last_restarts = getattr(self, '_last_restarts', restarts)
        self._last_restarts = restarts
        restart_delta = restarts - last_restarts

        errors = self.query(
            f'sum(rate(http_requests_total{{namespace="{namespace}",status="500"}}[2m]))'
        )
        total = self.query(
            f'sum(rate(http_requests_total{{namespace="{namespace}"}}[2m]))'
        )
        http_error_rate = errors / total if total > 0 else 0.0

        unavailable = self.query(
            f'kube_deployment_status_replicas_unavailable{{namespace="{namespace}",deployment="{deployment}"}}'
        )

        if restart_delta > 0:
            self.logger.info("Detected %d new pod restarts", int(restart_delta))
            return 1.0
        if unavailable > 0:
            self.logger.info("Detected %d unavailable replicas", int(unavailable))
            return 1.0
        return http_error_rate


class LLMAnalyzer:
    """Analyzes logs using an LLM via OpenRouter (OpenAI-compatible API)."""

    def __init__(self, api_key: str, model: str, base_url: str = "https://openrouter.ai/api/v1"):
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required — agent cannot start without it")
        if not model:
            raise ValueError("OPENROUTER_MODEL is required")
        self.model = model
        self.logger = logging.getLogger("llm-analyzer")
        self.client = OpenAI(base_url=base_url, api_key=api_key)

    def analyze(self, logs: str, namespace: str, deployment: str) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an SRE analyzing Kubernetes logs from a failing deployment. "
                            "Identify the likely root cause in 2-3 short sentences. Be concise and technical."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Deployment {namespace}/{deployment} is failing. Recent logs:\n\n{logs}\n\n"
                            "What is the likely root cause?"
                        ),
                    },
                ],
                timeout=30,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            self.logger.warning("LLM analysis failed: %s", e)
            return f"LLM analysis failed: {e}"


class KubernetesController:
    """Interacts with Kubernetes for logs and rollbacks."""

    def __init__(self, namespace: str, deployment: str):
        self.namespace = namespace
        self.deployment = deployment
        self.logger = logging.getLogger("k8s-controller")

    def get_logs(self, tail: int = 20) -> str:
        try:
            result = subprocess.run(
                ["kubectl", "logs", f"deployment/{self.deployment}", "-n", self.namespace, f"--tail={tail}"],
                capture_output=True, text=True, timeout=10,
            )
            return result.stdout[-2000:] if result.stdout else "No logs available"
        except Exception as e:
            return f"Failed to get logs: {e}"

    def rollback(self, image: str) -> bool:
        self.logger.info("ROLLING BACK %s/%s to %s", self.namespace, self.deployment, image)
        try:
            subprocess.run(
                ["kubectl", "patch", "deployment", self.deployment, "-n", self.namespace,
                 "--type=json", '-p=[{"op":"remove","path":"/spec/template/spec/containers/0/command"}]'],
                capture_output=True, timeout=30,
            )
            subprocess.run(
                ["kubectl", "set", "env", f"deployment/{self.deployment}",
                 "FAILURE_RATE=0", "APP_VERSION=v1", "-n", self.namespace],
                check=True, timeout=30,
            )
            subprocess.run(
                ["kubectl", "set", "image", f"deployment/{self.deployment}",
                 f"demo-app={image}", "-n", self.namespace],
                check=True, timeout=30,
            )
            self.logger.info("Rollback successful")
            return True
        except subprocess.CalledProcessError as e:
            self.logger.error("Rollback failed: %s", e)
            return False


class SupK8sAgent:
    """Main agent that orchestrates monitoring and remediation."""

    def __init__(
        self,
        prometheus: PrometheusClient,
        k8s: KubernetesController,
        llm: LLMAnalyzer,
        error_threshold: float,
        check_interval: int,
        rollback_image: str,
        cooldown_seconds: int = 300,
    ):
        self.prometheus = prometheus
        self.k8s = k8s
        self.llm = llm
        self.error_threshold = error_threshold
        self.check_interval = check_interval
        self.rollback_image = rollback_image
        self.cooldown_seconds = cooldown_seconds
        self.cooldown_until = 0.0
        self.logger = logging.getLogger("supk8s-agent")

    def is_in_cooldown(self) -> bool:
        return time.time() < self.cooldown_until

    def check(self) -> None:
        if self.is_in_cooldown():
            self.logger.info("In cooldown period, skipping check")
            return

        error_rate = self.prometheus.get_error_rate(self.k8s.namespace, self.k8s.deployment)
        self.logger.info(
            "Error rate: %.1f%% (threshold: %.0f%%)",
            error_rate * 100, self.error_threshold * 100,
        )

        if error_rate > self.error_threshold:
            self._handle_incident(error_rate)
        else:
            self.logger.info("All clear")

    def _handle_incident(self, error_rate: float) -> None:
        self.logger.warning("ERROR RATE EXCEEDED THRESHOLD!")

        logs = self.k8s.get_logs()
        self.logger.info("=== Recent Logs ===")
        self.logger.info(logs)
        self.logger.info("===================")

        self.logger.info("=== LLM Root Cause Analysis ===")
        analysis = self.llm.analyze(logs, self.k8s.namespace, self.k8s.deployment)
        self.logger.info(analysis)
        self.logger.info("===============================")

        self.k8s.rollback(self.rollback_image)
        self.cooldown_until = time.time() + self.cooldown_seconds
        self.logger.info("Cooldown: next check in %d seconds", self.cooldown_seconds)

    def run(self) -> None:
        self.logger.info("supK8s agent started")
        self.logger.info(
            "Watching: %s/%s | Threshold: %.0f%% | Interval: %ds",
            self.k8s.namespace, self.k8s.deployment,
            self.error_threshold * 100, self.check_interval,
        )

        # Block until Prometheus is reachable so we don't spam connection-refused warnings
        self.prometheus.wait_until_ready(timeout=120)

        while True:
            time.sleep(self.check_interval)
            self.check()


def create_agent_from_env() -> SupK8sAgent:
    """Factory: creates agent from environment variables.

    OPENROUTER_API_KEY is REQUIRED. The agent will refuse to start without it.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise SystemExit(
            "FATAL: OPENROUTER_API_KEY is not set. "
            "The agent requires an OpenRouter API key to perform LLM root-cause analysis. "
            "Get one at https://openrouter.ai/keys and set it via Secret 'supk8s-llm'."
        )

    prometheus = PrometheusClient(
        url=os.environ.get("PROMETHEUS_URL", "http://prometheus-server.monitoring.svc.cluster.local"),
    )
    k8s = KubernetesController(
        namespace=os.environ.get("WATCH_NAMESPACE", "demo"),
        deployment=os.environ.get("WATCH_DEPLOYMENT", "demo-app"),
    )
    llm = LLMAnalyzer(
        api_key=api_key,
        model=os.environ.get("OPENROUTER_MODEL", "openai/gpt-oss-120b:free"),
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    )

    return SupK8sAgent(
        prometheus=prometheus,
        k8s=k8s,
        llm=llm,
        error_threshold=float(os.environ.get("ERROR_THRESHOLD", "0.3")),
        check_interval=int(os.environ.get("CHECK_INTERVAL", "15")),
        rollback_image=os.environ.get("ROLLBACK_IMAGE", "demo-app:v1"),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    agent = create_agent_from_env()
    agent.run()
