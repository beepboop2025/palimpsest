"""Custom exceptions for the econscraper platform."""


class EconScraperError(Exception):
    """Base exception for all econscraper errors."""


class SourceDownError(EconScraperError):
    """Raised when a data source is unreachable."""

    def __init__(self, source: str, url: str = "", status_code: int = 0):
        self.source = source
        self.url = url
        self.status_code = status_code
        super().__init__(f"Source '{source}' is down (url={url}, status={status_code})")


class SchemaChangedError(EconScraperError):
    """Raised when a source's response schema has changed unexpectedly."""

    def __init__(self, source: str, expected: list[str], got: list[str]):
        self.source = source
        self.expected = expected
        self.got = got
        missing = set(expected) - set(got)
        super().__init__(f"Schema change in '{source}': missing columns {missing}")


class RateLimitError(EconScraperError):
    """Raised when a source's rate limit has been hit."""

    def __init__(self, source: str, retry_after: float = 0):
        self.source = source
        self.retry_after = retry_after
        super().__init__(f"Rate limited by '{source}', retry after {retry_after}s")


class ParseError(EconScraperError):
    """Raised when raw data cannot be parsed into the expected format."""

    def __init__(self, source: str, reason: str = ""):
        self.source = source
        self.reason = reason
        super().__init__(f"Parse error for '{source}': {reason}")


class StorageError(EconScraperError):
    """Raised when raw data cannot be stored."""

    def __init__(self, path: str, reason: str = ""):
        self.path = path
        super().__init__(f"Storage error at '{path}': {reason}")
