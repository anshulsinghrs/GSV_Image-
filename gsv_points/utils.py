"""
Shared utilities: logging setup, throttled HTTP session with retries,
on-disk JSON cache, and a simple token-bucket rate limiter.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logger(log_dir: Path, name: str = "gsv") -> logging.Logger:
    """Configure a rotating file logger plus console output."""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_dir / "pipeline.log", maxBytes=5 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    return logger


# ---------------------------------------------------------------------------
# Rate limiter (token-bucket)
# ---------------------------------------------------------------------------
class RateLimiter:
    """Thread-safe token bucket. acquire() blocks until a token is free."""

    def __init__(self, qps: float):
        self.capacity = max(1.0, qps)
        self.tokens = self.capacity
        self.fill_rate = qps
        self.timestamp = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.timestamp
            self.tokens = min(self.capacity, self.tokens + elapsed * self.fill_rate)
            self.timestamp = now
            if self.tokens < 1.0:
                sleep_for = (1.0 - self.tokens) / self.fill_rate
                time.sleep(sleep_for)
                self.tokens = 0.0
            else:
                self.tokens -= 1.0


# ---------------------------------------------------------------------------
# HTTP session with retry/backoff
# ---------------------------------------------------------------------------
def build_session(max_retries: int, backoff_factor: float) -> requests.Session:
    """A session with built-in urllib3 retries for transient errors."""
    session = requests.Session()
    retry = Retry(
        total=max_retries,
        connect=max_retries,
        read=max_retries,
        status=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ---------------------------------------------------------------------------
# Simple disk-backed JSON cache for metadata responses
# ---------------------------------------------------------------------------
class JsonCache:
    """One file per key. Keys are hashed by the caller."""

    def __init__(self, cache_dir: Path):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            with p.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def set(self, key: str, value: Dict[str, Any]) -> None:
        tmp = self._path(key).with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(value, f)
        tmp.replace(self._path(key))


def safe_key(point_id: Any, lat: float, lon: float) -> str:
    """Filesystem-safe cache key for a sample point."""
    return f"pt_{point_id}_{lat:.6f}_{lon:.6f}".replace(".", "p").replace("-", "n")
