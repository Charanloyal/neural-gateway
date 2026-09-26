import json
import asyncio
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional
from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaError

from app.config import settings

logger = logging.getLogger("neural_gateway.kafka")


@dataclass
class AuditEvent:
    request_id: str
    tenant_id: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float
    ttft_ms: float
    cost_usd: float
    cached: bool
    status: str
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")


class KafkaAuditProducer:
    """Asynchronous, non-blocking Kafka audit producer with resilient queueing and retries."""

    def __init__(self):
        self.producer: Optional[AIOKafkaProducer] = None
        self.topic = settings.KAFKA_TOPIC_AUDIT
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue(maxsize=10000)
        self._worker_task: Optional[asyncio.Task] = None
        self._is_running = False

    async def start(self) -> None:
        """Starts Kafka producer and background worker with connection resilience."""
        logger.info(f"Connecting to Kafka brokers at: {settings.KAFKA_BOOTSTRAP_SERVERS}")
        self.producer = AIOKafkaProducer(
            bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
            client_id=settings.KAFKA_CLIENT_ID,
            retry_backoff_ms=settings.KAFKA_RETRY_BACKOFF_MS,
            request_timeout_ms=5000,
            acks="all",
        )

        # Retry loop for Kafka availability during container orchestration startup
        max_attempts = 10
        for attempt in range(1, max_attempts + 1):
            try:
                await self.producer.start()
                logger.info("AIOKafkaProducer started successfully.")
                break
            except KafkaError as e:
                logger.warning(
                    f"Kafka connection attempt {attempt}/{max_attempts} failed: {e}. Retrying in 2s..."
                )
                if attempt == max_attempts:
                    logger.error("Failed to connect to Kafka. Audit events will be logged locally as fallback.")
                    # Keep producer as None; worker will gracefully handle this
                    break
                await asyncio.sleep(2.0)

        self._is_running = True
        self._worker_task = asyncio.create_task(self._process_queue())

    async def stop(self) -> None:
        """Drains pending audit events and gracefully terminates connections."""
        self._is_running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        if self.producer:
            try:
                logger.info("Flushing and shutting down Kafka producer...")
                await self.producer.stop()
                logger.info("Kafka producer stopped.")
            except Exception as e:
                logger.error(f"Error closing Kafka producer: {e}")

    def enqueue(self, event: AuditEvent) -> None:
        """Non-blocking enqueue for audit events."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.error("Kafka audit queue is full. Dropping oldest event to preserve gateway stability.")
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except Exception:
                pass

    async def _process_queue(self) -> None:
        """Continuous background worker ensuring gateway latency is never affected by Kafka I/O."""
        while self._is_running:
            try:
                event = await self._queue.get()
                if self.producer:
                    try:
                        await self.producer.send_and_wait(
                            topic=self.topic,
                            key=event.tenant_id.encode("utf-8"),
                            value=event.to_bytes(),
                        )
                        logger.debug(f"Audit event for request {event.request_id} emitted to Kafka.")
                    except KafkaError as ke:
                        logger.warning(f"Kafka write failed for request {event.request_id}: {ke}")
                else:
                    # Fallback log output when Kafka is offline
                    logger.info(f"[AUDIT LOG FALLBACK] {json.dumps(asdict(event))}")
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in Kafka worker loop: {e}")
                await asyncio.sleep(0.5)


kafka_audit_producer = KafkaAuditProducer()
