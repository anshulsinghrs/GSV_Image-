"""
Configuration for the Google Street View collection pipeline.

All tunable parameters live here. Override via environment variables
(GSV_API_KEY in particular) or by editing the dataclass defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class PipelineConfig:
    # --- API access ---------------------------------------------------------
    api_key: str = os.getenv("GSV_API_KEY", "")
    metadata_endpoint: str = "https://maps.googleapis.com/maps/api/streetview/metadata"
    image_endpoint: str = "https://maps.googleapis.com/maps/api/streetview"

    # --- Input / Output -----------------------------------------------------
    # Two top-level folders:
    #   gsv_points/  -> CSVs, GeoJSON, summary, maps, cache, logs
    #   gsv_images/  -> downloaded JPEG panoramas
    input_csv: Path = Path("data/road_points_delhi.csv")
    output_dir: Path = Path("output")
    points_dir: Path = Path("output/gsv_points")
    images_dir: Path = Path("output/gsv_images")
    cache_dir: Path = Path("output/gsv_points/.cache")
    log_dir: Path = Path("output/gsv_points/logs")

    metadata_csv: Path = Path("output/gsv_points/metadata.csv")
    metadata_geojson: Path = Path("output/gsv_points/metadata.geojson")
    panorama_index_csv: Path = Path("output/gsv_points/panorama_index.csv")
    summary_json: Path = Path("output/gsv_points/summary.json")

    # --- Imaging parameters -------------------------------------------------
    image_size: str = "640x640"          # max free tier is 640x640
    headings: List[int] = field(default_factory=lambda: [0, 90, 180, 270])
    pitch: int = 0
    fov: int = 90
    radius_m: int = 50                   # metadata search radius
    source: str = "outdoor"              # "default" or "outdoor"

    # --- Runtime ------------------------------------------------------------
    batch_size: int = 100
    max_workers: int = 8                 # concurrent HTTP workers
    request_timeout: int = 20
    max_retries: int = 5
    backoff_factor: float = 1.5          # exponential backoff base
    rate_limit_qps: float = 25.0         # client-side throttle

    # --- Behaviour ----------------------------------------------------------
    skip_existing: bool = True
    save_no_imagery: bool = True         # log points without panoramas

    def ensure_dirs(self) -> None:
        for d in (
            self.output_dir,
            self.points_dir,
            self.images_dir,
            self.cache_dir,
            self.log_dir,
        ):
            Path(d).mkdir(parents=True, exist_ok=True)


CONFIG = PipelineConfig()
