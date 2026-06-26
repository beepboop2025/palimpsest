"""Abstract base class that ALL collectors inherit from.

Provides:
- Retry with exponential backoff
- Raw data immutable storage
- Health reporting to Redis
- Collection logging to PostgreSQL
- Alert on consecutive failures

Subclasses implement: collect(), parse(), validate()
"""

import asyncio
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import pandas as pd

from core.circuit_breaker import CircuitBreaker
from core.exceptions import (
    ParseError, RateLimitError, SchemaChangedError, SourceDownError,
)

logger = logging.getLogger(__name__)

RAW_DATA_DIR = os.getenv("RAW_DATA_DIR", "./data/raw")


class BaseCollector(ABC):
    """Abstract base class for all data source collectors.

    Lifecycle: run() → collect() → _store_raw() → parse() → validate() → upsert

    Subclasses MUST implement:
        collect()  — fetch raw data from the source
        parse()    — transform raw data into a standardized DataFrame
        validate() — check that the parsed DataFrame meets the expected schema
    """

    name: str = "base"
    source_type: str = "unknown"  # "api", "rss", "scraper", "file"

    def __init__(self, config: dict):
        self.config = config
        self.schedule = config.get("schedule", "0 * * * *")
        self.retry_count = config.get("retry_count", 3)
        self.retry_backoff = config.get("retry_backoff", 2.0)
        self.timeout = config.get("timeout", 30)
        self.rate_limit = config.get("rate_limit", 1.0)

        self._consecutive_failures = 0
        self._last_request_at = 0.0
        self._http = httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": "EconScraper/4.0"},
            follow_redirects=True,
        )
        self._circuit_breaker = CircuitBreaker(
            name=self.name,
            failure_threshold=config.get("circuit_breaker_threshold", 5),
            cooldown_seconds=config.get("circuit_breaker_cooldown", 300),
        )

    async def close(self):
        """Close the HTTP client."""
        await self._http.aclose()

    # ── Abstract methods (subclass MUST implement) ────────────

    @abstractmethod
    async def collect(self) -> list[dict]:
        """Fetch raw data from the source. Return list of raw records."""

    @abstractmethod
    async def parse(self, raw_data: list[dict]) -> pd.DataFrame:
        """Transform raw records into a standardized DataFrame."""

    @abstractmethod
    def validate(self, df: pd.DataFrame) -> bool:
        """Validate that the parsed DataFrame meets expected schema.

        Should raise SchemaChangedError if validation fails.
        """

    # ── Provided by base class (do NOT override) ──────────────

    async def run(self) -> dict:
        """Full collection cycle with retries, raw storage, and health reporting.

        Returns a summary dict with status, records_collected, duration.
        """
        if self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=self.timeout,
                headers={"User-Agent": "EconScraper/4.0"},
                follow_redirects=True,
            )

        start = time.monotonic()

        # Circuit breaker check — skip collection if circuit is open
        if not self._circuit_breaker.can_execute():
            logger.warning(
                f"[{self.name}] Circuit breaker OPEN — skipping collection "
                f"({self._circuit_breaker.failure_count} consecutive failures)"
            )
            self._report_health("circuit_open", "Circuit breaker is open")
            return self._result(
                "circuit_open", 0, time.monotonic() - start,
                f"Circuit breaker open after {self._circuit_breaker.failure_count} failures"
            )

        self._report_health("running")

        try:
            # 1. Collect raw data with retry
            raw_data = await self._retry_with_backoff(self.collect)
            if not raw_data:
                self._report_health("success", "No new data")
                return self._result("success", 0, time.monotonic() - start)

            # 2. Store raw data (immutable)
            raw_path = self._store_raw(raw_data)

            # 3. Parse into DataFrame
            try:
                df = await self.parse(raw_data)
            except Exception as e:
                raise ParseError(self.name, str(e))

            # 4. Validate schema
            self.validate(df)

            # 5. Upsert to database
            records = await self._upsert(df, raw_path)

            # 6. Success
            self._consecutive_failures = 0
            self._circuit_breaker.record_success()
            duration = time.monotonic() - start
            self._report_health("success", f"{records} records in {duration:.1f}s")
            self._log_collection("success", records, duration)

            logger.info(f"[{self.name}] Collected {records} records in {duration:.1f}s")
            return self._result("success", records, duration)

        except (SourceDownError, RateLimitError) as e:
            self._consecutive_failures += 1
            self._circuit_breaker.record_failure()
            duration = time.monotonic() - start
            self._report_health("failed", str(e))
            self._log_collection("failed", 0, duration, str(e))
            self._maybe_alert()
            logger.error(f"[{self.name}] Collection failed: {e}")
            return self._result("failed", 0, duration, str(e))

        except (SchemaChangedError, ParseError) as e:
            self._consecutive_failures += 1
            self._circuit_breaker.record_failure()
            duration = time.monotonic() - start
            self._report_health("failed", str(e))
            self._log_collection("failed", 0, duration, str(e))
            self._maybe_alert()
            logger.error(f"[{self.name}] Data error: {e}")
            return self._result("failed", 0, duration, str(e))

        except Exception as e:
            self._consecutive_failures += 1
            self._circuit_breaker.record_failure()
            duration = time.monotonic() - start
            self._report_health("failed", str(e))
            self._log_collection("failed", 0, duration, str(e))
            self._maybe_alert()
            logger.exception(f"[{self.name}] Unexpected error")
            return self._result("failed", 0, duration, str(e))

        finally:
            await self.close()

    async def _retry_with_backoff(self, func, *args):
        """Exponential backoff retry wrapper."""
        last_error = None
        for attempt in range(1, self.retry_count + 1):
            try:
                # Rate limiting
                now = time.monotonic()
                elapsed = now - self._last_request_at
                if elapsed < self.rate_limit:
                    await asyncio.sleep(self.rate_limit - elapsed)
                self._last_request_at = time.monotonic()

                return await func(*args)
            except Exception as e:
                last_error = e
                if attempt < self.retry_count:
                    delay = self.retry_backoff ** attempt
                    logger.warning(
                        "Collection attempt failed",
                        extra={
                            "source": self.name,
                            "error_type": type(e).__name__,
                            "error": str(e),
                            "retry": attempt,
                            "max_retries": self.retry_count,
                            "next_delay": round(delay, 1),
                        },
                    )
                    await asyncio.sleep(delay)
        raise last_error

    def _store_raw(self, data: list[dict]) -> str:
        """Store raw response to /data/raw/{source}/{date}/.

        Raw data is IMMUTABLE — never modified after storage.
        Returns the file path for reference.
        """
        now = datetime.now(timezone.utc)
        dir_path = Path(RAW_DATA_DIR) / self.name / now.strftime("%Y-%m-%d")
        dir_path.mkdir(parents=True, exist_ok=True)

        filename = f"{self.name}_{now.strftime('%H%M%S')}_{now.timestamp():.0f}.json"
        filepath = dir_path / filename

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str, ensure_ascii=False)

        logger.debug(f"[{self.name}] Stored raw data: {filepath}")
        return str(filepath)

    def _report_health(self, status: str, message: str = ""):
        """Write health status to Redis: health:{self.name}"""
        try:
            import redis
            r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
            health = {
                "source": self.name,
                "status": status,
                "message": message,
                "consecutive_failures": self._consecutive_failures,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            r.set(f"health:{self.name}", json.dumps(health), ex=7200)  # 2hr TTL
            r.close()
        except Exception as e:
            logger.warning(f"[{self.name}] Health report failed: {e}")

    def _log_collection(self, status: str, records: int, duration: float, error: str = ""):
        """Log collection result to database."""
        try:
            from storage.models import CollectionLog
            from api.database import SessionLocal

            db = SessionLocal()
            try:
                log = CollectionLog(
                    source=self.name,
                    status=status,
                    records_collected=records,
                    duration_seconds=round(duration, 2),
                    error_message=error[:1000] if error else None,
                    run_at=datetime.now(timezone.utc),
                )
                db.add(log)
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"[{self.name}] Collection logging failed: {e}")

    def _maybe_alert(self):
        """Send alert if consecutive failures >= 3."""
        if self._consecutive_failures >= 3:
            logger.critical(
                f"[{self.name}] ALERT: {self._consecutive_failures} consecutive failures"
            )
            try:
                import redis
                r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
                alert = {
                    "source": self.name,
                    "failures": self._consecutive_failures,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                r.lpush("alerts:collector_failures", json.dumps(alert))
                r.ltrim("alerts:collector_failures", 0, 99)
                r.close()
            except Exception:
                pass

    async def _upsert(self, df: pd.DataFrame, raw_path: str) -> int:
        """Upsert parsed DataFrame to PostgreSQL.

        Uses the source_type to decide which table to write to:
        - "structured" → economic_data table
        - "article"    → articles table
        """
        if df.empty:
            return 0

        try:
            from storage.models import EconomicData, Article
            from api.database import SessionLocal

            db = SessionLocal()
            try:
                count = 0
                if self.source_type in ("api", "file", "structured"):
                    for _, row in df.iterrows():
                        record = EconomicData(
                            source=self.name,
                            indicator=row.get("indicator", ""),
                            date=row.get("date", datetime.now(timezone.utc)),
                            value=row.get("value"),
                            unit=row.get("unit", ""),
                            extra_data=row.get("metadata", {}),
                            collected_at=datetime.now(timezone.utc),
                            raw_path=raw_path,
                        )
                        db.merge(record)
                        count += 1
                else:
                    for _, row in df.iterrows():
                        import hashlib
                        url = row.get("url", "")
                        url_hash = hashlib.sha256(url.encode()).hexdigest()[:32] if url else None

                        article = Article(
                            source=self.name,
                            source_type=self.source_type,
                            url=url,
                            url_hash=url_hash,
                            title=row.get("title", ""),
                            author=row.get("author", ""),
                            published_at=row.get("published_at", datetime.now(timezone.utc)),
                            collected_at=datetime.now(timezone.utc),
                            full_text=row.get("full_text", row.get("description", "")),
                            raw_path=raw_path,
                            category=row.get("category", ""),
                        )
                        db.merge(article)
                        count += 1
                db.commit()
                return count
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[{self.name}] Upsert failed: {e}")
            return 0

    @staticmethod
    def _result(status: str, records: int, duration: float, error: str = "") -> dict:
        return {
            "status": status,
            "records_collected": records,
            "duration_seconds": round(duration, 2),
            "error": error,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
