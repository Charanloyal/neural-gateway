import json
import math
import time
import uuid
import struct
import logging
import asyncio
from typing import List, Optional, Tuple, Dict, Any
import numpy as np
import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.config import settings

logger = logging.getLogger("neural_gateway.cache")


class EmbeddingEngine:
    """Computes L2-normalized dense embeddings with local transformer model or deterministic fallback."""

    def __init__(self, model_name: str = settings.EMBEDDING_MODEL_NAME, dimension: int = settings.EMBEDDING_DIMENSION):
        self.dimension = dimension
        self.model_name = model_name
        self._model = None
        self._initialize_model()

    def _initialize_model(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
            # Suppress noisy logs during local initialization
            logger.info(f"Loading SentenceTransformer: {self.model_name}...")
            self._model = SentenceTransformer(self.model_name)
            logger.info("SentenceTransformer loaded successfully.")
        except Exception as e:
            logger.warning(
                f"SentenceTransformer not available or failed to load ({e}). Using deterministic dense vector projection fallback."
            )
            self._model = None

    def encode(self, text: str) -> np.ndarray:
        """Generates a unit-normalized vector (L2 norm = 1.0)."""
        clean_text = text.strip().lower()
        if self._model is not None:
            try:
                emb = self._model.encode(clean_text, convert_to_numpy=True, normalize_embeddings=True)
                return emb.astype(np.float32)
            except Exception as e:
                logger.error(f"Error encoding with SentenceTransformer: {e}. Falling back to deterministic projection.")

        # Deterministic projection fallback based on hash tokenization and random projection
        return self._deterministic_projection(clean_text)

    def _deterministic_projection(self, text: str) -> np.ndarray:
        """Fast, seed-consistent deterministic n-gram vectorizer normalized to unit sphere."""
        vec = np.zeros(self.dimension, dtype=np.float32)
        tokens = text.split()
        if not tokens:
            vec[0] = 1.0
            return vec

        for idx, token in enumerate(tokens):
            h = hash(token)
            # Distribute across vector dimensions using token hash
            pos = abs(h) % self.dimension
            sign = 1.0 if (h >> 3) % 2 == 0 else -1.0
            weight = 1.0 / (1.0 + 0.1 * idx)  # Decay weight by position
            vec[pos] += sign * weight

        # Subword character 3-grams for semantic overlap
        for i in range(len(text) - 2):
            trigram = text[i : i + 3]
            h = hash(trigram)
            pos = abs(h) % self.dimension
            sign = 1.0 if (h >> 2) % 2 == 0 else -1.0
            vec[pos] += sign * 0.25

        norm = np.linalg.norm(vec)
        if norm > 1e-6:
            vec /= norm
        else:
            vec[0] = 1.0
        return vec


def vector_to_bytes(vector: np.ndarray) -> bytes:
    """Serializes float32 numpy array to compact binary bytes."""
    return vector.astype(np.float32).tobytes()


def bytes_to_vector(data: bytes) -> np.ndarray:
    """Deserializes binary bytes back to float32 numpy array."""
    return np.frombuffer(data, dtype=np.float32)


class SemanticCache:
    """Redis-backed Semantic Vector Cache using cosine similarity search."""

    def __init__(self, redis_client: aioredis.Redis, embedding_engine: Optional[EmbeddingEngine] = None):
        self.redis = redis_client
        self.embedder = embedding_engine or EmbeddingEngine()
        self.threshold = settings.SEMANTIC_CACHE_SIMILARITY_THRESHOLD
        self.ttl = settings.SEMANTIC_CACHE_TTL_SECONDS

    async def get(self, tenant_id: str, prompt: str) -> Optional[Tuple[str, float]]:
        """Searches for a semantically equivalent cached completion.
        
        Returns:
            Tuple of (cached_completion_text, similarity_score) if hit, else None.
        """
        if not settings.SEMANTIC_CACHE_ENABLED:
            return None

        query_vec = self.embedder.encode(prompt)
        index_key = f"semantic_cache:{tenant_id}:index"

        try:
            # Fetch all active cache IDs for tenant
            cache_ids = await self.redis.smembers(index_key)
            if not cache_ids:
                return None

            pipe = self.redis.pipeline(transaction=False)
            keys = [f"semantic_cache:{tenant_id}:entry:{cid.decode('utf-8') if isinstance(cid, bytes) else cid}" for cid in cache_ids]
            
            for key in keys:
                pipe.hgetall(key)

            entries = await pipe.execute()
            best_similarity = -1.0
            best_response: Optional[str] = None
            stale_keys = []

            for cid, data in zip(cache_ids, entries):
                cid_str = cid.decode("utf-8") if isinstance(cid, bytes) else cid
                if not data:
                    stale_keys.append(cid_str)
                    continue

                raw_vec = data.get(b"vector") or data.get("vector")
                raw_resp = data.get(b"response") or data.get("response")

                if not raw_vec or not raw_resp:
                    stale_keys.append(cid_str)
                    continue

                if isinstance(raw_vec, str):
                    raw_vec = raw_vec.encode("latin1")
                
                cached_vec = bytes_to_vector(raw_vec)
                # Cosine similarity for normalized vectors is direct dot product
                similarity = float(np.dot(query_vec, cached_vec))

                if similarity > best_similarity:
                    best_similarity = similarity
                    best_response = raw_resp.decode("utf-8") if isinstance(raw_resp, bytes) else raw_resp

            # Clean up stale references if any found
            if stale_keys:
                asyncio.create_task(self._cleanup_stale_keys(index_key, stale_keys))

            if best_similarity >= self.threshold and best_response is not None:
                logger.info(
                    f"Semantic cache HIT for tenant '{tenant_id}' (similarity: {best_similarity:.4f} >= {self.threshold})"
                )
                return best_response, best_similarity

            return None

        except RedisError as e:
            logger.error(f"Redis error during semantic cache lookup: {e}")
            return None

    async def set(self, tenant_id: str, prompt: str, completion: str) -> None:
        """Saves a prompt and its completion with embedded vector representation."""
        if not settings.SEMANTIC_CACHE_ENABLED:
            return

        entry_id = str(uuid.uuid4())
        index_key = f"semantic_cache:{tenant_id}:index"
        entry_key = f"semantic_cache:{tenant_id}:entry:{entry_id}"

        try:
            vec = self.embedder.encode(prompt)
            vec_bytes = vector_to_bytes(vec)

            pipe = self.redis.pipeline(transaction=True)
            pipe.sadd(index_key, entry_id)
            pipe.hset(
                entry_key,
                mapping={
                    "prompt": prompt,
                    "response": completion,
                    "vector": vec_bytes,
                    "created_at": str(time.time()),
                },
            )
            pipe.expire(entry_key, self.ttl)
            pipe.expire(index_key, self.ttl * 2)
            await pipe.execute()
            logger.debug(f"Saved semantic cache entry {entry_id} for tenant {tenant_id}")
        except RedisError as e:
            logger.error(f"Redis error during semantic cache write: {e}")

    async def _cleanup_stale_keys(self, index_key: str, stale_ids: List[str]) -> None:
        try:
            if stale_ids:
                await self.redis.srem(index_key, *stale_ids)
        except Exception as e:
            logger.warning(f"Failed to prune stale cache keys: {e}")
