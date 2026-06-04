"""
Street View Static Image downloader.

For each pano we download one JPEG per configured heading. Files are
saved as `{point_id}_{pano_id}_h{heading}.jpg` so we can detect what's
already on disk and skip re-downloading on resume.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

import requests

from .config import PipelineConfig
from .utils import RateLimiter


@dataclass
class ImageRecord:
    point_id: Any
    pano_id: str
    heading: int
    pitch: int
    fov: int
    image_path: str
    status: str            # "OK" | "SKIPPED" | "ERROR"
    error: Optional[str] = None


class ImageDownloader:
    def __init__(
        self,
        cfg: PipelineConfig,
        session: requests.Session,
        limiter: RateLimiter,
        logger: logging.Logger,
    ):
        self.cfg = cfg
        self.session = session
        self.limiter = limiter
        self.log = logger

    def _image_path(self, point_id: Any, pano_id: str, heading: int) -> Path:
        # Files live under images_dir; one image per heading to keep
        # filenames flat. Use a deterministic naming scheme for resume.
        fname = f"{point_id}_{pano_id}_h{heading}.jpg"
        return self.cfg.images_dir / fname

    def download_pano(self, point_id: Any, pano_id: str) -> List[ImageRecord]:
        """Download every configured heading for a single panorama."""
        records: List[ImageRecord] = []
        for heading in self.cfg.headings:
            records.append(self._download_one(point_id, pano_id, heading))
        return records

    def _download_one(
        self, point_id: Any, pano_id: str, heading: int
    ) -> ImageRecord:
        target = self._image_path(point_id, pano_id, heading)

        if self.cfg.skip_existing and target.exists() and target.stat().st_size > 0:
            return ImageRecord(
                point_id=point_id,
                pano_id=pano_id,
                heading=heading,
                pitch=self.cfg.pitch,
                fov=self.cfg.fov,
                image_path=str(target),
                status="SKIPPED",
            )

        # Address the imagery by pano id (more stable than lat/lon) so
        # repeated downloads always return the same panorama.
        params = {
            "size": self.cfg.image_size,
            "pano": pano_id,
            "heading": heading,
            "pitch": self.cfg.pitch,
            "fov": self.cfg.fov,
            "key": self.cfg.api_key,
        }

        self.limiter.acquire()
        try:
            r = self.session.get(
                self.cfg.image_endpoint,
                params=params,
                timeout=self.cfg.request_timeout,
                stream=True,
            )
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "")
            if "image" not in ctype:
                return ImageRecord(
                    point_id=point_id,
                    pano_id=pano_id,
                    heading=heading,
                    pitch=self.cfg.pitch,
                    fov=self.cfg.fov,
                    image_path="",
                    status="ERROR",
                    error=f"unexpected content-type: {ctype}",
                )
            tmp = target.with_suffix(".part")
            with tmp.open("wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
            tmp.replace(target)
            return ImageRecord(
                point_id=point_id,
                pano_id=pano_id,
                heading=heading,
                pitch=self.cfg.pitch,
                fov=self.cfg.fov,
                image_path=str(target),
                status="OK",
            )
        except requests.RequestException as exc:
            self.log.warning(
                "image download failed pano=%s heading=%d: %s",
                pano_id, heading, exc,
            )
            return ImageRecord(
                point_id=point_id,
                pano_id=pano_id,
                heading=heading,
                pitch=self.cfg.pitch,
                fov=self.cfg.fov,
                image_path="",
                status="ERROR",
                error=str(exc),
            )
