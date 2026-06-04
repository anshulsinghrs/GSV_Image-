"""
Street View Metadata API client.

The Metadata endpoint is FREE and returns whether imagery exists for a
given lat/lon plus the panorama id, capture date, exact location, and
copyright string. We always probe metadata BEFORE downloading images so
that we never spend image-API quota on points without coverage.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

import requests

from .config import PipelineConfig
from .utils import JsonCache, RateLimiter, safe_key


@dataclass
class PanoMetadata:
    point_id: Any
    query_lat: float
    query_lon: float
    status: str
    pano_id: Optional[str] = None
    capture_date: Optional[str] = None
    pano_lat: Optional[float] = None
    pano_lon: Optional[float] = None
    copyright: Optional[str] = None
    source: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MetadataClient:
    def __init__(
        self,
        cfg: PipelineConfig,
        session: requests.Session,
        limiter: RateLimiter,
        cache: JsonCache,
        logger: logging.Logger,
    ):
        self.cfg = cfg
        self.session = session
        self.limiter = limiter
        self.cache = cache
        self.log = logger

    def fetch(self, point_id: Any, lat: float, lon: float) -> PanoMetadata:
        """Return metadata for the given coordinate, using cache when possible."""
        key = safe_key(point_id, lat, lon)
        cached = self.cache.get(key)
        if cached is not None:
            return self._parse(point_id, lat, lon, cached)

        params = {
            "location": f"{lat},{lon}",
            "radius": self.cfg.radius_m,
            "source": self.cfg.source,
            "key": self.cfg.api_key,
        }

        self.limiter.acquire()
        try:
            r = self.session.get(
                self.cfg.metadata_endpoint,
                params=params,
                timeout=self.cfg.request_timeout,
            )
            r.raise_for_status()
            payload = r.json()
        except requests.RequestException as exc:
            self.log.warning("metadata request failed for %s: %s", point_id, exc)
            return PanoMetadata(
                point_id=point_id,
                query_lat=lat,
                query_lon=lon,
                status="REQUEST_ERROR",
                error=str(exc),
            )

        self.cache.set(key, payload)
        return self._parse(point_id, lat, lon, payload)

    @staticmethod
    def _parse(
        point_id: Any, lat: float, lon: float, payload: Dict[str, Any]
    ) -> PanoMetadata:
        status = payload.get("status", "UNKNOWN")
        loc = payload.get("location") or {}
        return PanoMetadata(
            point_id=point_id,
            query_lat=lat,
            query_lon=lon,
            status=status,
            pano_id=payload.get("pano_id"),
            capture_date=payload.get("date"),
            pano_lat=loc.get("lat"),
            pano_lon=loc.get("lng"),
            copyright=payload.get("copyright"),
            source=payload.get("source") or payload.get("pano_source"),
        )
