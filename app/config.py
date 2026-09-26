import os
from typing import List
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # Server Settings
    APP_NAME: str = "NeuralGateway"
    APP_ENV: str = "production"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 4

    # Redis Configuration
    REDIS_URL: str = "redis://redis:6379/0"
    REDIS_MAX_CONNECTIONS: int = 50
    REDIS_SOCKET_TIMEOUT: float = 1.0
    REDIS_CONNECT_TIMEOUT: float = 1.0
    REDIS_RETRY_ATTEMPTS: int = 2
    REDIS_RETRY_DELAY: float = 0.2

    # Kafka Configuration
    KAFKA_BOOTSTRAP_SERVERS: str = "kafka:9092"
    KAFKA_TOPIC_AUDIT: str = "llm-gateway-audit"
    KAFKA_CLIENT_ID: str = "neural-gateway-core"
    KAFKA_RETRY_BACKOFF_MS: int = 500
    KAFKA_MAX_REQUEST_SIZE: int = 1048576

    # Token Bucket Rate Limiter
    RATE_LIMIT_TOKENS_PER_SECOND: float = 100.0
    RATE_LIMIT_BURST_CAPACITY: float = 200.0
    RATE_LIMIT_ENABLED: bool = True

    # Semantic Vector Cache
    SEMANTIC_CACHE_ENABLED: bool = True
    SEMANTIC_CACHE_SIMILARITY_THRESHOLD: float = 0.92
    SEMANTIC_CACHE_TTL_SECONDS: int = 86400  # 24 hours
    SEMANTIC_CACHE_MAX_ENTRIES: int = 50000
    EMBEDDING_DIMENSION: int = 384
    EMBEDDING_MODEL_NAME: str = "sentence-transformers/all-MiniLM-L6-v2"

    # Circuit Breaker & Provider Engine
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 4
    CIRCUIT_BREAKER_RECOVERY_TIMEOUT: float = 15.0
    CIRCUIT_BREAKER_HALF_OPEN_SUCCESSES: int = 2
    EWMA_DECAY_ALPHA: float = 0.25
    PROVIDER_TIMEOUT_SECONDS: float = 15.0

    # Pricing per 1k tokens (in USD)
    COST_PER_1K_PROMPT_TOKENS: float = 0.0015
    COST_PER_1K_COMPLETION_TOKENS: float = 0.0020


settings = Settings()
