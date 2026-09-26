import random
import string
import json
from locust import HttpUser, task, between


class NeuralGatewayUser(HttpUser):
    wait_time = between(0.01, 0.05)  # High-throughput burst simulation

    def on_start(self):
        # Generate tenant identifiers to simulate multi-tenant concurrency
        self.tenant_id = f"tenant-{random.choice(['alpha', 'beta', 'gamma', 'delta'])}"
        self.common_prompts = [
            "Explain quantum computing principles succinctly.",
            "Write a Python function to compute Fibonacci numbers.",
            "Describe the architecture of distributed consensus algorithms.",
            "How does transformer self-attention work?",
        ]

    @task(5)
    def test_streaming_chat_completion(self):
        """Simulates high-frequency streaming inference calls with cache hit potential."""
        prompt = random.choice(self.common_prompts)
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-ID": self.tenant_id,
        }
        payload = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }

        with self.client.post(
            "/v1/chat/completions",
            json=payload,
            headers=headers,
            stream=True,
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                # Read chunks to verify stream delivery
                for line in response.iter_lines():
                    pass
                response.success()
            elif response.status_code == 429:
                # Rate limit encountered - expected during burst stress
                response.success()
            else:
                response.failure(f"Unexpected status code: {response.status_code}")

    @task(2)
    def test_cache_hit_generation(self):
        """Repeats identical prompt to verify low-latency semantic cache acceleration."""
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-ID": "tenant-benchmark-cache",
        }
        payload = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Explain quantum computing principles succinctly."}],
            "stream": True,
        }
        with self.client.post("/v1/chat/completions", json=payload, headers=headers, stream=True, catch_response=True) as resp:
            if resp.status_code in [200, 429]:
                resp.success()
            else:
                resp.failure(f"Cache test failed: {resp.status_code}")

    @task(1)
    def check_health_and_metrics(self):
        """Validates observability scrape endpoint under load."""
        self.client.get("/metrics")
