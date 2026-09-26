import asyncio
import time
import logging
from enum import Enum
from typing import Optional

logger = logging.getLogger("neural_gateway.circuit_breaker")


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    HALF_OPEN = "HALF_OPEN"
    OPEN = "OPEN"


class CircuitBreakerOpenException(Exception):
    def __init__(self, provider_name: str, retry_after: float):
        self.provider_name = provider_name
        self.retry_after = retry_after
        super().__init__(
            f"Circuit breaker for provider '{provider_name}' is OPEN. Retry after {retry_after:.2f}s."
        )


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 4,
        recovery_timeout: float = 15.0,
        half_open_success_threshold: int = 2,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_success_threshold = half_open_success_threshold

        self.state: CircuitState = CircuitState.CLOSED
        self.failure_count: int = 0
        self.consecutive_successes: int = 0
        self.last_state_change: float = time.monotonic()
        self.last_failure_time: float = 0.0
        self._lock = asyncio.Lock()

    def get_state_int(self) -> int:
        if self.state == CircuitState.CLOSED:
            return 0
        elif self.state == CircuitState.HALF_OPEN:
            return 1
        return 2

    async def can_execute(self) -> bool:
        """Determines if a request can pass through the circuit breaker."""
        async with self._lock:
            now = time.monotonic()

            if self.state == CircuitState.CLOSED:
                return True

            if self.state == CircuitState.OPEN:
                elapsed = now - self.last_state_change
                if elapsed >= self.recovery_timeout:
                    logger.info(
                        f"[{self.name}] Recovery timeout reached ({elapsed:.1f}s >= {self.recovery_timeout}s). Transitioning to HALF_OPEN."
                    )
                    self.state = CircuitState.HALF_OPEN
                    self.consecutive_successes = 0
                    self.last_state_change = now
                    return True
                return False

            if self.state == CircuitState.HALF_OPEN:
                # In HALF_OPEN state, allow probe requests
                return True

            return False

    async def record_success(self) -> None:
        """Records a successful provider execution."""
        async with self._lock:
            now = time.monotonic()
            if self.state == CircuitState.HALF_OPEN:
                self.consecutive_successes += 1
                logger.info(
                    f"[{self.name}] HALF_OPEN probe success ({self.consecutive_successes}/{self.half_open_success_threshold})."
                )
                if self.consecutive_successes >= self.half_open_success_threshold:
                    logger.info(f"[{self.name}] Recovery criteria met. Transitioning to CLOSED.")
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    self.consecutive_successes = 0
                    self.last_state_change = now
            elif self.state == CircuitState.CLOSED:
                # Reset transient failure count on solid success
                self.failure_count = 0

    async def record_failure(self, error: Optional[Exception] = None) -> None:
        """Records a provider execution failure, tripping the breaker if threshold reached."""
        async with self._lock:
            now = time.monotonic()
            self.last_failure_time = now

            if self.state == CircuitState.HALF_OPEN:
                logger.warning(
                    f"[{self.name}] Probe failed in HALF_OPEN state ({error}). Re-tripping to OPEN."
                )
                self.state = CircuitState.OPEN
                self.last_state_change = now
                self.consecutive_successes = 0

            elif self.state == CircuitState.CLOSED:
                self.failure_count += 1
                logger.warning(
                    f"[{self.name}] Request failed ({error}). Failure count: {self.failure_count}/{self.failure_threshold}"
                )
                if self.failure_count >= self.failure_threshold:
                    logger.error(
                        f"[{self.name}] Failure threshold exceeded. Tripping circuit to OPEN."
                    )
                    self.state = CircuitState.OPEN
                    self.last_state_change = now

    def retry_after(self) -> float:
        """Remaining duration in seconds before the breaker shifts from OPEN to HALF_OPEN."""
        if self.state != CircuitState.OPEN:
            return 0.0
        now = time.monotonic()
        remaining = self.recovery_timeout - (now - self.last_state_change)
        return max(0.0, remaining)
