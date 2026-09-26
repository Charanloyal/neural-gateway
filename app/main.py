import time
import uuid
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any, AsyncGenerator

from fastapi import FastAPI, Request, Response, Header, HTTPException, status
from fastapi.responses import StreamingResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.config import settings
from app.circuit_breaker import CircuitBreakerOpenException
from app.router import (
    ProviderPool,
    BaseLLMProvider,
    format_sse_chunk,
    AllProvidersUnavailableException,
    ProviderException,
)
from app.rate_limiter import RedisRateLimiter
from app.cache import SemanticCache, EmbeddingEngine
from app.kafka_producer import kafka_audit_producer, AuditEvent
from app.telemetry import (
    get_latest_metrics,
    record_request_metric,
    record_ttft_metric,
    record_tps_metric,
    record_cost_metric,
    ACTIVE_STREAMS,
)

# Configure structured logging
logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("neural_gateway.main")

# Global state instances
redis_client: Optional[aioredis.Redis] = None
rate_limiter: Optional[RedisRateLimiter] = None
semantic_cache: Optional[SemanticCache] = None
provider_pool: Optional[ProviderPool] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, rate_limiter, semantic_cache, provider_pool

    logger.info(f"Initializing {settings.APP_NAME} platform services...")

    # 1. Connect to Redis with retry mechanism
    for attempt in range(1, settings.REDIS_RETRY_ATTEMPTS + 1):
        try:
            redis_client = aioredis.from_url(
                settings.REDIS_URL,
                max_connections=settings.REDIS_MAX_CONNECTIONS,
                socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
                socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT,
                decode_responses=False,
            )
            await redis_client.ping()
            logger.info("Connected to Redis successfully.")
            break
        except Exception as e:
            logger.warning(f"Redis initialization attempt {attempt} failed: {e}")
            if attempt == settings.REDIS_RETRY_ATTEMPTS:
                logger.error("Failed to connect to Redis after maximum retries. Starting in degraded mode.")
            await asyncio.sleep(settings.REDIS_RETRY_DELAY)

    # 2. Instantiate Rate Limiter & Semantic Vector Cache
    if redis_client:
        rate_limiter = RedisRateLimiter(redis_client)
        embedding_engine = EmbeddingEngine(
            model_name=settings.EMBEDDING_MODEL_NAME,
            dimension=settings.EMBEDDING_DIMENSION,
        )
        semantic_cache = SemanticCache(redis_client, embedding_engine)
    else:
        logger.warning("Operating without Redis. Rate limiting and caching will be bypassed.")

    # 3. Instantiate Provider Pool
    provider_pool = ProviderPool()

    # 4. Start Kafka Audit Producer
    await kafka_audit_producer.start()

    logger.info(f"{settings.APP_NAME} is fully online and ready for traffic.")
    yield

    # Graceful Shutdown
    logger.info("Initiating graceful shutdown sequence...")
    await kafka_audit_producer.stop()
    if redis_client:
        await redis_client.aclose()
        logger.info("Closed Redis connection pool.")
    logger.info("Shutdown complete.")


app = FastAPI(
    title=settings.APP_NAME,
    description="High-Throughput Multi-Tenant Distributed LLM Inference Gateway",
    version="1.0.0",
    lifespan=lifespan,
)


# Request and Response Models
class ChatMessage(BaseModel):
    role: str = Field(..., description="Role of the author (system, user, assistant)")
    content: str = Field(..., description="Contents of the message")


class ChatCompletionRequest(BaseModel):
    model: str = Field(default="gpt-4o", description="Target model name")
    messages: List[ChatMessage] = Field(..., description="Conversation history")
    stream: bool = Field(default=True, description="Enable Server-Sent Events (SSE) streaming")
    temperature: Optional[float] = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=1024, ge=1)


def extract_prompt_text(messages: List[ChatMessage]) -> str:
    """Concatenates conversation messages into a canonical string for semantic comparison."""
    user_prompts = [m.content for m in messages if m.role.lower() == "user"]
    if user_prompts:
        return user_prompts[-1]
    return " ".join([f"{m.role}:{m.content}" for m in messages])


@app.get("/healthz", tags=["System"])
async def healthz():
    """Health check for container orchestrators (Docker / Kubernetes)."""
    redis_healthy = False
    if redis_client:
        try:
            await redis_client.ping()
            redis_healthy = True
        except Exception:
            redis_healthy = False

    kafka_healthy = kafka_audit_producer.producer is not None

    status_code = status.HTTP_200_OK if (redis_healthy and kafka_healthy) else status.HTTP_200_OK
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "healthy" if (redis_healthy and kafka_healthy) else "degraded",
            "redis_connected": redis_healthy,
            "kafka_connected": kafka_healthy,
            "providers_registered": len(provider_pool.providers) if provider_pool else 0,
        },
    )


@app.get("/metrics", tags=["Observability"])
async def metrics():
    """Prometheus telemetry scrape endpoint."""
    data, content_type = get_latest_metrics()
    return Response(content=data, media_type=content_type)


@app.get("/v1/providers", tags=["Inference Gateway"])
async def get_providers():
    """Returns real-time status, rolling EWMA latency, and circuit breaker metrics for each provider."""
    if not provider_pool:
        raise HTTPException(status_code=503, detail="Provider pool not initialized")

    results = {}
    for name, p in provider_pool.providers.items():
        results[name] = {
            "name": p.name,
            "circuit_state": p.circuit_breaker.state.value,
            "ewma_latency_ms": round(p.ewma_latency_ms, 2),
            "consecutive_failures": p.circuit_breaker.failure_count,
            "retry_after_seconds": round(p.circuit_breaker.retry_after(), 2),
        }
    return JSONResponse(content={"providers": results})


@app.post("/v1/chat/completions", tags=["Inference Gateway"])
async def chat_completions(
    request: Request,
    payload: ChatCompletionRequest,
    x_tenant_id: Optional[str] = Header(default="tenant-default", alias="X-Tenant-ID"),
):
    request_id = str(uuid.uuid4())
    tenant_id = x_tenant_id.strip() if x_tenant_id else "tenant-default"
    req_start_time = time.monotonic()

    # 1. Atomic Rate Limiting (Token Bucket via Lua in Redis)
    if rate_limiter:
        rate_res = await rate_limiter.check_rate_limit(tenant_id=tenant_id, requested_tokens=1.0)
        if not rate_res.allowed:
            record_request_metric(
                tenant=tenant_id,
                provider="none",
                status="429",
                cached=False,
            )
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "error": {
                        "message": f"Rate limit burst exceeded. Retry in {rate_res.retry_after:.1f}s.",
                        "type": "tokens_per_second_quota_exceeded",
                        "code": 429,
                    }
                },
                headers=rate_res.headers,
            )

    canonical_prompt = extract_prompt_text(payload.messages)
    prompt_tokens = max(1, len(canonical_prompt.split()))

    # 2. Semantic Vector Caching Check
    if semantic_cache and settings.SEMANTIC_CACHE_ENABLED:
        cached_hit = await semantic_cache.get(tenant_id=tenant_id, prompt=canonical_prompt)
        if cached_hit is not None:
            cached_text, similarity = cached_hit
            total_time_ms = (time.monotonic() - req_start_time) * 1000.0
            completion_tokens = max(1, len(cached_text.split()))

            # Record Telemetry for Cache Hit
            record_request_metric(tenant=tenant_id, provider="cache", status="200", cached=True)
            record_ttft_metric(tenant=tenant_id, provider="cache", duration_seconds=total_time_ms / 1000.0)

            # Produce Kafka Audit Event
            kafka_audit_producer.enqueue(
                AuditEvent(
                    request_id=request_id,
                    tenant_id=tenant_id,
                    provider="semantic-cache",
                    model=payload.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                    latency_ms=round(total_time_ms, 2),
                    ttft_ms=round(total_time_ms, 2),
                    cost_usd=0.0,  # Cache hits incur zero LLM provider cost
                    cached=True,
                    status="200",
                )
            )

            if payload.stream:
                async def cached_stream() -> AsyncGenerator[str, None]:
                    chunk_id = f"chatcmpl-{request_id}"
                    # Simulate rapid SSE replay of cached response
                    words = cached_text.split(" ")
                    for idx, word in enumerate(words):
                        space = " " if idx < len(words) - 1 else ""
                        yield format_sse_chunk(chunk_id, word + space, payload.model)
                        await asyncio.sleep(0.005)
                    yield format_sse_chunk(chunk_id, "", payload.model, finish_reason="stop")
                    yield "data: [DONE]\n\n"

                return StreamingResponse(
                    cached_stream(),
                    media_type="text/event-stream",
                    headers={"X-Cache": "HIT", "X-Cache-Similarity": f"{similarity:.4f}"},
                )
            else:
                return JSONResponse(
                    content={
                        "id": f"chatcmpl-{request_id}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": payload.model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": cached_text},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "total_tokens": prompt_tokens + completion_tokens,
                        },
                    },
                    headers={"X-Cache": "HIT", "X-Cache-Similarity": f"{similarity:.4f}"},
                )

    # 3. Dynamic Latency-Weighted Routing
    try:
        provider = await provider_pool.select_provider()
    except AllProvidersUnavailableException:
        record_request_metric(tenant=tenant_id, provider="none", status="503", cached=False)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="All upstream inference providers are currently unavailable due to active circuit breakers.",
        )

    # 4. SSE Streaming Delivery with Disconnect Cancellation
    async def sse_event_streamer() -> AsyncGenerator[str, None]:
        chunk_id = f"chatcmpl-{request_id}"
        ACTIVE_STREAMS.labels(tenant=tenant_id, provider=provider.name).inc()

        start_time = time.monotonic()
        first_token_time: Optional[float] = None
        accumulated_tokens: List[str] = []
        is_first_chunk = True
        status_code_str = "200"
        current_provider = provider

        try:
            # Stream generator with fallback handling before first token
            stream_iter = current_provider.stream_completion(canonical_prompt, request)

            while True:
                # Intercept client SSE disconnect immediately
                if await request.is_disconnected():
                    logger.info(f"Client disconnected for request {request_id}. Aborting stream.")
                    status_code_str = "499"  # Client Closed Request
                    break

                try:
                    delta_text = await stream_iter.__anext__()
                except StopAsyncIteration:
                    break
                except Exception as stream_err:
                    await current_provider.circuit_breaker.record_failure(stream_err)
                    logger.error(f"Provider {current_provider.name} failed during streaming: {stream_err}")

                    # Fallback to secondary provider if failure happened before any tokens emitted
                    if is_first_chunk:
                        fallback = provider_pool.get_fallback_provider(current_provider.name)
                        if fallback:
                            logger.info(f"Failing over to {fallback.name} for request {request_id}")
                            current_provider = fallback
                            stream_iter = current_provider.stream_completion(canonical_prompt, request)
                            continue
                    status_code_str = "502"
                    break

                # First token received
                now = time.monotonic()
                if is_first_chunk:
                    is_first_chunk = False
                    first_token_time = now
                    ttft = first_token_time - start_time
                    record_ttft_metric(tenant=tenant_id, provider=current_provider.name, duration_seconds=ttft)

                accumulated_tokens.append(delta_text)
                yield format_sse_chunk(chunk_id, delta_text, payload.model)

            # Signal completion to client
            if status_code_str == "200":
                yield format_sse_chunk(chunk_id, "", payload.model, finish_reason="stop")
                yield "data: [DONE]\n\n"
                await current_provider.circuit_breaker.record_success()

        finally:
            ACTIVE_STREAMS.labels(tenant=tenant_id, provider=current_provider.name).dec()

            # Calculate metrics
            end_time = time.monotonic()
            total_duration_s = max(0.001, end_time - start_time)
            completion_text = "".join(accumulated_tokens)
            completion_tokens = max(1, len(completion_text.split())) if completion_text else 0
            ttft_ms = ((first_token_time - start_time) * 1000.0) if first_token_time else 0.0

            # Update rolling EWMA provider latency
            current_provider.update_ewma_latency(total_duration_s * 1000.0)

            # Record Prometheus Metrics
            record_request_metric(
                tenant=tenant_id,
                provider=current_provider.name,
                status=status_code_str,
                cached=False,
            )
            record_tps_metric(
                tenant=tenant_id,
                provider=current_provider.name,
                tokens=completion_tokens,
                duration_seconds=total_duration_s,
            )
            cost_usd = record_cost_metric(
                tenant=tenant_id,
                provider=current_provider.name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_per_1k_prompt=settings.COST_PER_1K_PROMPT_TOKENS,
                cost_per_1k_completion=settings.COST_PER_1K_COMPLETION_TOKENS,
            )

            # Asynchronously update semantic vector cache on successful generation
            if status_code_str == "200" and completion_text and semantic_cache:
                asyncio.create_task(
                    semantic_cache.set(tenant_id, canonical_prompt, completion_text)
                )

            # Enqueue structured audit log to Kafka
            kafka_audit_producer.enqueue(
                AuditEvent(
                    request_id=request_id,
                    tenant_id=tenant_id,
                    provider=current_provider.name,
                    model=payload.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                    latency_ms=round(total_duration_s * 1000.0, 2),
                    ttft_ms=round(ttft_ms, 2),
                    cost_usd=round(cost_usd, 6),
                    cached=False,
                    status=status_code_str,
                )
            )

    if payload.stream:
        return StreamingResponse(
            sse_event_streamer(),
            media_type="text/event-stream",
            headers={"X-Cache": "MISS", "X-Accel-Buffering": "no"},
        )
    else:
        # Non-streaming buffer path
        accumulated_text = []
        async for chunk_str in sse_event_streamer():
            if chunk_str.startswith("data: ") and not chunk_str.startswith("data: [DONE]"):
                try:
                    parsed = json.loads(chunk_str[6:].strip())
                    delta = parsed["choices"][0]["delta"].get("content", "")
                    if delta:
                        accumulated_text.append(delta)
                except Exception:
                    pass

        full_content = "".join(accumulated_text)
        return JSONResponse(
            content={
                "id": f"chatcmpl-{request_id}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": payload.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": full_content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": len(full_content.split()),
                    "total_tokens": prompt_tokens + len(full_content.split()),
                },
            },
            headers={"X-Cache": "MISS"},
        )
