from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    generate_latest,
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    REGISTRY,
)
from typing import Tuple

# Custom Prometheus Metrics
REQUESTS_TOTAL = Counter(
    "llm_gateway_requests_total",
    "Total count of LLM inference requests received by the gateway",
    labelnames=["tenant", "provider", "status", "cached"],
)

TTFT_SECONDS = Histogram(
    "llm_gateway_ttft_seconds",
    "Time to first token (TTFT) in seconds for streaming inference",
    labelnames=["tenant", "provider"],
    buckets=(0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 10.0),
)

TOKENS_PER_SECOND = Gauge(
    "llm_gateway_tokens_per_second",
    "Instantaneous generation speed in tokens per second",
    labelnames=["tenant", "provider"],
)

COST_USD_TOTAL = Counter(
    "llm_gateway_cost_usd_total",
    "Accumulated estimated cost in USD based on input and output tokens",
    labelnames=["tenant", "provider"],
)

ACTIVE_STREAMS = Gauge(
    "llm_gateway_active_streams",
    "Number of currently active streaming connections",
    labelnames=["tenant", "provider"],
)

CIRCUIT_BREAKER_STATE = Gauge(
    "llm_gateway_circuit_breaker_state",
    "Current state of the provider circuit breaker (0=CLOSED, 1=HALF_OPEN, 2=OPEN)",
    labelnames=["provider"],
)


def record_request_metric(tenant: str, provider: str, status: str, cached: bool) -> None:
    """Record request count with tenant, provider, status, and cached flag."""
    REQUESTS_TOTAL.labels(
        tenant=tenant,
        provider=provider,
        status=status,
        cached="true" if cached else "false",
    ).inc()


def record_ttft_metric(tenant: str, provider: str, duration_seconds: float) -> None:
    """Record Time-To-First-Token for a streaming request."""
    if duration_seconds > 0:
        TTFT_SECONDS.labels(tenant=tenant, provider=provider).observe(duration_seconds)


def record_tps_metric(tenant: str, provider: str, tokens: int, duration_seconds: float) -> None:
    """Calculate and set tokens per second gauge."""
    if duration_seconds > 0.001 and tokens > 0:
        tps = tokens / duration_seconds
        TOKENS_PER_SECOND.labels(tenant=tenant, provider=provider).set(round(tps, 2))


def record_cost_metric(
    tenant: str,
    provider: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_per_1k_prompt: float,
    cost_per_1k_completion: float,
) -> float:
    """Calculate and record total monetary cost in USD."""
    prompt_cost = (prompt_tokens / 1000.0) * cost_per_1k_prompt
    completion_cost = (completion_tokens / 1000.0) * cost_per_1k_completion
    total_cost = prompt_cost + completion_cost
    if total_cost > 0:
        COST_USD_TOTAL.labels(tenant=tenant, provider=provider).inc(total_cost)
    return total_cost


def set_circuit_breaker_metric(provider: str, state_value: int) -> None:
    """Record circuit breaker state (0=CLOSED, 1=HALF_OPEN, 2=OPEN)."""
    CIRCUIT_BREAKER_STATE.labels(provider=provider).set(state_value)


def get_latest_metrics() -> Tuple[bytes, str]:
    """Expose Prometheus scrape data formatted as bytes and appropriate content type."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
