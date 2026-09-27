import time
import json
import uuid
import random
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Any, AsyncGenerator, Optional, Tuple
from fastapi import Request

from app.config import settings
from app.circuit_breaker import CircuitBreaker, CircuitBreakerOpenException
from app.telemetry import (
    record_request_metric,
    record_ttft_metric,
    record_tps_metric,
    record_cost_metric,
    set_circuit_breaker_metric,
    ACTIVE_STREAMS,
)

logger = logging.getLogger("neural_gateway.router")


class ProviderException(Exception):
    """Base exception for upstream provider failure."""
    pass


class AllProvidersUnavailableException(Exception):
    """Raised when all configured providers are tripped by circuit breakers."""
    pass


class BaseLLMProvider(ABC):
    def __init__(self, name: str, initial_ewma_ms: float = 80.0):
        self.name = name
        self.circuit_breaker = CircuitBreaker(
            name=name,
            failure_threshold=settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            recovery_timeout=settings.CIRCUIT_BREAKER_RECOVERY_TIMEOUT,
            half_open_success_threshold=settings.CIRCUIT_BREAKER_HALF_OPEN_SUCCESSES,
        )
        self.ewma_latency_ms: float = initial_ewma_ms
        self.alpha: float = settings.EWMA_DECAY_ALPHA
        self._lock = asyncio.Lock()

    def update_ewma_latency(self, sample_latency_ms: float) -> None:
        """Applies Exponentially Weighted Moving Average (EWMA) to smooth latency spikes."""
        self.ewma_latency_ms = (self.alpha * sample_latency_ms) + ((1.0 - self.alpha) * self.ewma_latency_ms)
        logger.debug(f"[{self.name}] Updated EWMA latency: {self.ewma_latency_ms:.2f}ms")

    @abstractmethod
    async def stream_completion(
        self, prompt: str, request: Request
    ) -> AsyncGenerator[str, None]:
        """Stream token-by-token text delta generator."""
        pass


class MockOpenAIProvider(BaseLLMProvider):
    """Primary provider mock simulating OpenAI gpt-4o inference streaming."""

    def __init__(self, name: str = "openai-primary", initial_ewma_ms: float = 45.0):
        super().__init__(name=name, initial_ewma_ms=initial_ewma_ms)

    async def stream_completion(
        self, prompt: str, request: Request
    ) -> AsyncGenerator[str, None]:
        # Check simulation header for fault injection
        fail_target = request.headers.get("x-mock-fail-provider", "").lower()
        if fail_target == self.name:
            await asyncio.sleep(0.05)
            raise ProviderException(f"Simulated upstream connection failure in {self.name}")

        # Simulate TTFT (time to first token) with realistic network latency
        pre_delay = random.uniform(0.035, 0.075)
        await asyncio.sleep(pre_delay)

        generated_chunks = [
            f"[{self.name}] ",
            "NeuralGateway ",
            "successfully ",
            "routed ",
            "your ",
            "query: '",
            prompt[:40] + ("..." if len(prompt) > 40 else ""),
            "'. ",
            "Processing ",
            "with ",
            "low-latency ",
            "dynamic ",
            "load-balancing ",
            "and ",
            "distributed ",
            "state ",
            "verification.",
        ]

        for chunk in generated_chunks:
            # Client disconnect check: abort upstream immediately
            if await request.is_disconnected():
                logger.info(f"[{self.name}] Client disconnected mid-stream. Halting provider stream.")
                return

            yield chunk
            # Realistic token generation interval (30-50 tokens/sec)
            await asyncio.sleep(random.uniform(0.015, 0.030))


class MockAnthropicProvider(BaseLLMProvider):
    """Secondary provider mock simulating Anthropic claude-3-5-sonnet inference streaming."""

    def __init__(self, name: str = "anthropic-secondary", initial_ewma_ms: float = 75.0):
        super().__init__(name=name, initial_ewma_ms=initial_ewma_ms)

    async def stream_completion(
        self, prompt: str, request: Request
    ) -> AsyncGenerator[str, None]:
        fail_target = request.headers.get("x-mock-fail-provider", "").lower()
        if fail_target == self.name:
            await asyncio.sleep(0.05)
            raise ProviderException(f"Simulated upstream connection failure in {self.name}")

        pre_delay = random.uniform(0.060, 0.110)
        await asyncio.sleep(pre_delay)

        generated_chunks = [
            f"[{self.name}] ",
            "Received ",
            "payload ",
            "via ",
            "secondary ",
            "failover ",
            "pipeline. ",
            "Context: '",
            prompt[:40] + ("..." if len(prompt) > 40 else ""),
            "'. ",
            "Adaptive ",
            "circuit ",
            "breaker ",
            "safeguards ",
            "continuous ",
            "throughput.",
        ]

        for chunk in generated_chunks:
            if await request.is_disconnected():
                logger.info(f"[{self.name}] Client disconnected mid-stream. Halting provider stream.")
                return

            yield chunk
            await asyncio.sleep(random.uniform(0.020, 0.035))


class ProviderPool:
    """Manages provider routing, health tracking, and EWMA latency-weighted selection."""

    def __init__(self):
        self.providers: Dict[str, BaseLLMProvider] = {
            "openai-primary": MockOpenAIProvider(),
            "anthropic-secondary": MockAnthropicProvider(),
        }

    def register_provider(self, provider: BaseLLMProvider) -> None:
        self.providers[provider.name] = provider

    async def select_provider(self) -> BaseLLMProvider:
        """Picks the healthiest provider using inverse latency-weighted probabilities."""
        healthy_providers: List[BaseLLMProvider] = []
        for p in self.providers.values():
            set_circuit_breaker_metric(p.name, p.circuit_breaker.get_state_int())
            if await p.circuit_breaker.can_execute():
                healthy_providers.append(p)

        if not healthy_providers:
            raise AllProvidersUnavailableException("All inference providers have tripped circuit breakers.")

        if len(healthy_providers) == 1:
            return healthy_providers[0]

        # Dynamic EWMA inverse-latency weighting: P(i) ~ 1 / (latency_ms)^1.5
        weights: List[float] = []
        for p in healthy_providers:
            safe_lat = max(1.0, p.ewma_latency_ms)
            weights.append(1.0 / (safe_lat ** 1.5))

        total_weight = sum(weights)
        if total_weight <= 0:
            return random.choice(healthy_providers)

        probabilities = [w / total_weight for w in weights]
        selected = random.choices(healthy_providers, weights=probabilities, k=1)[0]
        logger.debug(
            f"Selected provider '{selected.name}' (EWMA: {selected.ewma_latency_ms:.2f}ms, Weight: {probabilities[healthy_providers.index(selected)]:.3f})"
        )
        return selected

    def get_fallback_provider(self, failed_name: str) -> Optional[BaseLLMProvider]:
        for name, provider in self.providers.items():
            if name != failed_name and provider.circuit_breaker.state.value != "OPEN":
                return provider
        return None

    async def reset_circuits(self) -> None:
        """Resets all provider circuit breakers back to CLOSED."""
        for provider in self.providers.values():
            await provider.circuit_breaker.reset()
        logger.info("All provider circuit breakers have been reset to CLOSED.")



def format_sse_chunk(chunk_id: str, content: str, model: str, finish_reason: Optional[str] = None) -> str:
    """Formats payload strictly following OpenAI Server-Sent Events protocol."""
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content} if content else {},
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n"
