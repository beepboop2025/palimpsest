"""Abstract base class for all NLP/ML processors.

Processors are Celery tasks that:
1. Read unprocessed articles from PostgreSQL
2. Apply transformations (NLP, embeddings, etc.)
3. Write results back to PostgreSQL
4. Mark articles as processed
"""

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class BaseProcessor(ABC):
    """Abstract processor. Subclasses implement process_one() or process_batch()."""

    name: str = "base_processor"
    batch_size: int = 50

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.batch_size = self.config.get("batch_size", self.batch_size)

    @abstractmethod
    def process_one(self, article: dict) -> dict:
        """Process a single article. Return the analysis result dict.

        The result will be stored in the corresponding analysis table.
        """

    def process_batch(self, articles: list[dict]) -> list[dict]:
        """Process a batch of articles. Default: call process_one in loop.

        Override for GPU-batched operations (e.g., embedding, FinBERT).
        """
        results = []
        for article in articles:
            try:
                result = self.process_one(article)
                results.append(result)
            except Exception as e:
                logger.warning(f"[{self.name}] Failed on article {article.get('id')}: {e}")
                results.append({"error": str(e), "article_id": article.get("id")})
        return results

    def run(self) -> dict:
        """Fetch unprocessed articles and run the processor."""
        start = time.monotonic()

        try:
            from storage.models import Article
            from api.database import SessionLocal

            db = SessionLocal()
            try:
                articles = (
                    db.query(Article)
                    .filter(Article.is_processed == False)
                    .limit(self.batch_size)
                    .all()
                )

                if not articles:
                    return {"status": "no_work", "processed": 0}

                article_dicts = [
                    {
                        "id": a.id,
                        "title": a.title,
                        "full_text": a.full_text or "",
                        "url": a.url,
                        "source": a.source,
                        "category": a.category,
                        "published_at": a.published_at.isoformat() if a.published_at else None,
                    }
                    for a in articles
                ]

                results = self.process_batch(article_dicts)
                self._store_results(results, db)

                duration = time.monotonic() - start
                logger.info(f"[{self.name}] Processed {len(results)} articles in {duration:.1f}s")
                return {
                    "status": "success",
                    "processed": len(results),
                    "duration_seconds": round(duration, 2),
                }
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[{self.name}] Run failed: {e}")
            return {"status": "error", "error": str(e)}

    def _store_results(self, results: list[dict], db):
        """Store processing results. Subclasses can override for custom storage."""
        pass
