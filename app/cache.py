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


SEMANTIC_STOPWORDS = {
    "what", "is", "are", "explain", "how", "does", "do", "can", "you", "tell",
    "me", "about", "in", "an", "the", "a", "works", "working", "work", "please",
    "describe", "overview", "of", "for", "to", "and", "with", "give", "i",
    "want", "need", "like", "understanding", "understand", "clarify", "briefly"
}


_embedder_instance: Optional["EmbeddingEngine"] = None


def get_embedding_engine(model_name: str = settings.EMBEDDING_MODEL_NAME, dimension: int = settings.EMBEDDING_DIMENSION) -> "EmbeddingEngine":
    global _embedder_instance
    if _embedder_instance is None:
        _embedder_instance = EmbeddingEngine(model_name=model_name, dimension=dimension)
    return _embedder_instance


class EmbeddingEngine:
    def __init__(self, model_name: str = settings.EMBEDDING_MODEL_NAME, dimension: int = settings.EMBEDDING_DIMENSION):
        self.dimension = dimension
        self.model_name = model_name
        self._model = None
        self.active_embedder: str = "deterministic-projection"
        self._initialize_model()

    def _initialize_model(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
            self.active_embedder = "sentence-transformers"
        except Exception:
            self._model = None
            self.active_embedder = "deterministic-projection"

    def encode(self, text: str) -> np.ndarray:
        """Generates a unit-normalized vector (L2 norm = 1.0)."""
        clean_text = text.strip().lower()
        if self._model is not None:
            try:
                emb = self._model.encode(clean_text, convert_to_numpy=True, normalize_embeddings=True)
                return emb.astype(np.float32)
            except Exception as e:
                logger.error(f"Error encoding with SentenceTransformer: {e}. Falling back to deterministic projection.")

        # Deterministic projection fallback based on semantic tokenization and stable hashing
        return self._deterministic_projection(clean_text)

    def _deterministic_projection(self, text: str) -> np.ndarray:
        """Fast, seed-consistent deterministic n-gram vectorizer normalized to unit sphere."""
        import re, hashlib
        clean = re.sub(r"[^\w\s]", "", text.lower())
        raw_tokens = clean.split()
        if not raw_tokens:
            vec = np.zeros(self.dimension, dtype=np.float32)
            vec[0] = 1.0
            return vec

        semantic_tokens = []
        for t in raw_tokens:
            if t in SEMANTIC_STOPWORDS:
                continue
            # Suffix stemming for semantic equivalence
            if t.endswith("ing") and len(t) > 5:
                t = t[:-3]
            elif t.endswith("tion") and len(t) > 6:
                t = t[:-4]
            elif t.endswith("ment") and len(t) > 6:
                t = t[:-4]
            elif t.endswith("ed") and len(t) > 4:
                t = t[:-2]
            elif t.endswith("s") and len(t) > 3 and not t.endswith("ss"):
                t = t[:-1]
            semantic_tokens.append(t)

        if not semantic_tokens:
            semantic_tokens = raw_tokens

        vec = np.zeros(self.dimension, dtype=np.float32)
        for idx, token in enumerate(semantic_tokens):
            h = int(hashlib.md5(token.encode("utf-8")).hexdigest()[:8], 16)
            pos = h % self.dimension
            sign = 1.0 if (h >> 4) % 2 == 0 else -1.0
            vec[pos] += sign * (1.0 + 0.15 * max(0, 5 - idx))

            # Subword character 3-grams for semantic n-gram overlap
            for i in range(len(token) - 2):
                sub_h = int(hashlib.md5(token[i : i + 3].encode("utf-8")).hexdigest()[:8], 16)
                sub_pos = sub_h % self.dimension
                sub_sign = 1.0 if (sub_h >> 3) % 2 == 0 else -1.0
                vec[sub_pos] += sub_sign * 0.25

        norm = np.linalg.norm(vec)
        if norm > 1e-6:
            vec /= norm
        else:
            vec[0] = 1.0
        return vec.astype(np.float32)



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

    async def clear(self, tenant_id: Optional[str] = None) -> None:
        """Clears cached vectors from Redis."""
        try:
            if tenant_id:
                index_key = f"neural:cache:idx:{tenant_id}"
                entry_ids = await self.redis.smembers(index_key)
                if entry_ids:
                    keys = [f"neural:cache:entry:{tenant_id}:{eid.decode() if isinstance(eid, bytes) else eid}" for eid in entry_ids]
                    keys.append(index_key)
                    await self.redis.delete(*keys)
            else:
                keys = [k async for k in self.redis.scan_iter("neural:cache:*")]
                if keys:
                    await self.redis.delete(*keys)
            logger.info("Semantic cache cleared.")
        except Exception as e:
            logger.warning(f"Failed to clear Redis semantic cache: {e}")


class InMemorySemanticCache:
    """In-memory semantic vector cache fallback for standalone execution."""

    def __init__(self, embedding_engine: Optional[EmbeddingEngine] = None):
        self.embedder = embedding_engine or EmbeddingEngine()
        self.threshold = settings.SEMANTIC_CACHE_SIMILARITY_THRESHOLD
        self._entries: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def get(self, tenant_id: str, prompt: str) -> Optional[Tuple[str, float]]:
        if not settings.SEMANTIC_CACHE_ENABLED:
            return None

        query_vec = self.embedder.encode(prompt)
        async with self._lock:
            items = self._entries.get(tenant_id, [])
            best_sim = -1.0
            best_resp: Optional[str] = None

            for item in items:
                sim = float(np.dot(query_vec, item["vector"]))
                if sim > best_sim:
                    best_sim = sim
                    best_resp = item["response"]

            if best_sim >= self.threshold and best_resp is not None:
                logger.info(
                    f"[InMemoryCache] HIT for tenant '{tenant_id}' (sim: {best_sim:.4f} >= {self.threshold})"
                )
                return best_resp, best_sim

            return None

    async def set(self, tenant_id: str, prompt: str, completion: str) -> None:
        if not settings.SEMANTIC_CACHE_ENABLED:
            return

        query_vec = self.embedder.encode(prompt)
        async with self._lock:
            if tenant_id not in self._entries:
                self._entries[tenant_id] = []
            self._entries[tenant_id].append({
                "prompt": prompt,
                "response": completion,
                "vector": query_vec,
                "created_at": time.time(),
            })

    async def clear(self, tenant_id: Optional[str] = None) -> None:
        """Clears in-memory semantic cache entries."""
        async with self._lock:
            if tenant_id:
                self._entries.pop(tenant_id, None)
            else:
                self._entries.clear()
            logger.info("In-memory semantic cache cleared.")


