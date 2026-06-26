"""Circuit breaker pattern for collectors and scrapers.

Prevents cascading failures by tracking consecutive errors and
temporarily stopping requests to failing sources.

States:
- CLOSED: Normal operation, requests pass through
- OPEN: Source has failed too many times, all requests are rejected for a cooldown period
- HALF_OPEN: After cooldown, allow exactly 1 probe request to test recovery

State is persisted in Redis so it survives worker restarts.
Falls back to in-memory tracking if Redis is unavailable.
"""

import json
import logging
import os
import time
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-source circuit breaker with Redis-backed state persistence.

    Args:
        name: Source/collector identifier (e.g., "fred_api", "reddit")
        failure_threshold: Number of consecutive failures before opening (default: 5)
        cooldown_seconds: How long to stay open before allowing a probe (default: 300)
        redis_url: Redis URL for state persistence (default: from REDIS_URL env var)
    """

    REDIS_KEY_PREFIX = "circuit_breaker:"

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        cooldown_seconds: int = 300,
        redis_url: Optional[str] = None,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379")

        # In-memory fallback state
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at = 0.0
        self._half_open_in_progress = False

    def _redis_key(self) -> str:
        return f"{self.REDIS_KEY_PREFIX}{self.name}"

    def _load_state_from_redis(self) -> Optional[dict]:
        """Load circuit state from Redis. Returns None if unavailable."""
        try:
            import redis
            r = redis.from_url(self.redis_url, decode_responses=True)
            try:
                data = r.get(self._redis_key())
                if data:
                    return json.loads(data)
            finally:
                r.close()
        except Exception:
            pass
        return None

    def _save_state_to_redis(self):
        """Persist current circuit state to Redis."""
        try:
            import redis
            r = redis.from_url(self.redis_url, decode_responses=True)
            try:
                state_data = {
                    "state": self._state.value,
                    "failure_count": self._failure_count,
                    "opened_at": self._opened_at,
                    "half_open_in_progress": self._half_open_in_progress,
                    "updated_at": time.time(),
                }
                # TTL = 2x cooldown so stale states auto-expire
                r.set(self._redis_key(), json.dumps(state_data), ex=self.cooldown_seconds * 2)
            finally:
                r.close()
        except Exception as e:
            logger.debug(f"[CircuitBreaker:{self.name}] Redis save failed: {e}")

    def _sync_from_redis(self):
        """Sync in-memory state from Redis if available."""
        data = self._load_state_from_redis()
        if data:
            self._state = CircuitState(data.get("state", "closed"))
            self._failure_count = data.get("failure_count", 0)
            self._opened_at = data.get("opened_at", 0.0)
            self._half_open_in_progress = data.get("half_open_in_progress", False)

    def can_execute(self) -> bool:
        """Check if a request is allowed through the circuit.

        Returns True if the request should proceed, False if it should be rejected.
        """
        self._sync_from_redis()

        if self._state == CircuitState.CLOSED:
            return True

        if self._state == CircuitState.OPEN:
            elapsed = time.time() - self._opened_at
            if elapsed >= self.cooldown_seconds:
                # Transition to half-open: allow exactly 1 probe
                if not self._half_open_in_progress:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_in_progress = True
                    self._save_state_to_redis()
                    logger.info(
                        f"[CircuitBreaker:{self.name}] OPEN -> HALF_OPEN, allowing probe request"
                    )
                    return True
                # Another request while probe is in-flight: reject
                return False
            logger.debug(
                f"[CircuitBreaker:{self.name}] OPEN, {self.cooldown_seconds - elapsed:.0f}s remaining"
            )
            return False

        if self._state == CircuitState.HALF_OPEN:
            # Only allow the single probe request
            if not self._half_open_in_progress:
                self._half_open_in_progress = True
                self._save_state_to_redis()
                return True
            return False

        return True

    def record_success(self):
        """Record a successful request. Resets failure count and closes circuit."""
        self._failure_count = 0
        self._half_open_in_progress = False
        if self._state != CircuitState.CLOSED:
            logger.info(f"[CircuitBreaker:{self.name}] {self._state.value} -> CLOSED (success)")
        self._state = CircuitState.CLOSED
        self._save_state_to_redis()

    def record_failure(self):
        """Record a failed request. May open the circuit if threshold is reached."""
        self._failure_count += 1

        if self._state == CircuitState.HALF_OPEN:
            # Probe failed, go back to OPEN with fresh cooldown
            self._state = CircuitState.OPEN
            self._opened_at = time.time()
            self._half_open_in_progress = False
            logger.warning(
                f"[CircuitBreaker:{self.name}] HALF_OPEN -> OPEN (probe failed, "
                f"cooldown {self.cooldown_seconds}s)"
            )
            self._save_state_to_redis()
            return

        if self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.time()
            self._half_open_in_progress = False
            logger.warning(
                f"[CircuitBreaker:{self.name}] CLOSED -> OPEN "
                f"({self._failure_count} consecutive failures, "
                f"cooldown {self.cooldown_seconds}s)"
            )
        self._save_state_to_redis()

    @property
    def state(self) -> CircuitState:
        self._sync_from_redis()
        return self._state

    @property
    def failure_count(self) -> int:
        self._sync_from_redis()
        return self._failure_count

    def reset(self):
        """Manually reset the circuit breaker to CLOSED state."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at = 0.0
        self._half_open_in_progress = False
        self._save_state_to_redis()
        logger.info(f"[CircuitBreaker:{self.name}] Manually reset to CLOSED")
