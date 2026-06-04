"""
Orchestration layer.

Reads the sample-points CSV, fans out metadata + image requests across a
thread pool, deduplicates panoramas, writes metadata.csv / metadata.geojson /
panorama_index.csv, then renders the Folium maps and summary stats.

Resumable: existing metadata.csv (if any) and on-disk image files are
respected so a re-run only fills in gaps.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from tqdm import tqdm

from config import CONFIG, PipelineConfig
from downloader import ImageDownloader, ImageRecord
from metadata import MetadataClient, PanoMetadata
from utils import JsonCache, RateLimiter, build_session, setup_logger
from visualization import (
    build_panorama_map,
    build_sample_points_map,
    compute_summary,
)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
REQUIRED_COLS = {"point_id", "latitude", "longitude"}


def load_points(csv_path: Path, logger: logging.Logger) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(f"input CSV missing columns: {sorted(missing)}")
    df = df.drop_duplicates(subset=["point_id"]).reset_index(drop=True)
    logger.info("loaded %d sample points from %s", len(df), csv_path)
    return df


# ---------------------------------------------------------------------------
# Resume support
# ---------------------------------------------------------------------------
def load_existing_metadata(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except (pd.errors.EmptyDataError, OSError):
        return pd.DataFrame()


def select_pending(points: pd.DataFrame, existing: pd.DataFrame) -> pd.DataFrame:
    if existing.empty or "point_id" not in existing.columns:
        return points
    done_ids = set(existing["point_id"].tolist())
    pending = points[~points["point_id"].isin(done_ids)]
    return pending.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Concurrent stages
# ---------------------------------------------------------------------------
def run_metadata_stage(
    points: pd.DataFrame,
    client: MetadataClient,
    cfg: PipelineConfig,
) -> List[PanoMetadata]:
    """Probe every (lat,lon) for panorama metadata."""
    results: List[PanoMetadata] = []
    with ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
        futures = {
            ex.submit(client.fetch, row.point_id, row.latitude, row.longitude): row.point_id
            for row in points.itertuples(index=False)
        }
        for fut in tqdm(
            as_completed(futures),
            total=len(futures),
            desc="metadata",
            unit="pt",
        ):
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                pid = futures[fut]
                results.append(
                    PanoMetadata(
                        point_id=pid,
                        query_lat=float("nan"),
                        query_lon=float("nan"),
                        status="EXCEPTION",
                        error=repr(exc),
                    )
                )
    return results


def run_image_stage(
    pano_jobs: List[Tuple[Any, str]],
    downloader: ImageDownloader,
    cfg: PipelineConfig,
) -> List[ImageRecord]:
    """Download every heading for each (point_id, pano_id) pair."""
    records: List[ImageRecord] = []
    if not pano_jobs:
        return records

    with ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
        futures = {
            ex.submit(downloader.download_pano, pid, pano): (pid, pano)
            for (pid, pano) in pano_jobs
        }
        for fut in tqdm(
            as_completed(futures),
            total=len(futures),
            desc="images",
            unit="pano",
        ):
            try:
                records.extend(fut.result())
            except Exception as exc:  # noqa: BLE001
                pid, pano = futures[fut]
                records.append(
                    ImageRecord(
                        point_id=pid,
                        pano_id=pano,
                        heading=-1,
                        pitch=cfg.pitch,
                        fov=cfg.fov,
                        image_path="",
                        status="ERROR",
                        error=repr(exc),
                    )
                )
    return records


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
def write_metadata_outputs(
    metadata_df: pd.DataFrame,
    image_df: pd.DataFrame,
    cfg: PipelineConfig,
    logger: logging.Logger,
) -> pd.DataFrame:
    """Join metadata + images into the public metadata.csv schema."""
    if metadata_df.empty:
        logger.warning("no metadata to write")
        return pd.DataFrame()

    # Pick the canonical (OK) image row per point/pano. If no image yet
    # (e.g. metadata-only points), emit a placeholder row.
    if not image_df.empty:
        # Prefer the first heading's image as the canonical image_path.
        first = (
            image_df.sort_values(["point_id", "pano_id", "heading"])
            .groupby(["point_id", "pano_id"], as_index=False)
            .first()
        )
        joined = metadata_df.merge(
            first[["point_id", "pano_id", "heading", "pitch", "fov", "image_path"]],
            on=["point_id", "pano_id"],
            how="left",
        )
    else:
        joined = metadata_df.copy()
        for col, default in (
            ("heading", cfg.headings[0] if cfg.headings else 0),
            ("pitch", cfg.pitch),
            ("fov", cfg.fov),
            ("image_path", ""),
        ):
            joined[col] = default

    # Public schema as requested.
    public = pd.DataFrame({
        "point_id": joined["point_id"],
        "pano_id": joined["pano_id"],
        "capture_date": joined["capture_date"],
        "latitude": joined["pano_lat"].fillna(joined["query_lat"]),
        "longitude": joined["pano_lon"].fillna(joined["query_lon"]),
        "image_path": joined.get("image_path", ""),
        "heading": joined.get("heading", cfg.headings[0] if cfg.headings else 0),
        "pitch": joined.get("pitch", cfg.pitch),
        "fov": joined.get("fov", cfg.fov),
        "status": joined["status"],
        "copyright": joined.get("copyright"),
        "source": joined.get("source"),
    })

    public.to_csv(cfg.metadata_csv, index=False)
    logger.info("wrote %s (%d rows)", cfg.metadata_csv, len(public))

    # GeoJSON (one feature per point that has a coordinate).
    geo = public.dropna(subset=["latitude", "longitude"]).copy()
    if not geo.empty:
        gdf = gpd.GeoDataFrame(
            geo,
            geometry=[Point(xy) for xy in zip(geo["longitude"], geo["latitude"])],
            crs="EPSG:4326",
        )
        gdf.to_file(cfg.metadata_geojson, driver="GeoJSON")
        logger.info("wrote %s (%d features)", cfg.metadata_geojson, len(gdf))

    return public


def write_panorama_index(
    metadata_df: pd.DataFrame,
    image_df: pd.DataFrame,
    cfg: PipelineConfig,
    logger: logging.Logger,
) -> pd.DataFrame:
    """One row per (point_id, pano_id, heading); dedup pano_ids globally."""
    ok = metadata_df[metadata_df["status"] == "OK"].dropna(subset=["pano_id"])
    if ok.empty:
        empty = pd.DataFrame(
            columns=[
                "point_id", "pano_id", "heading", "pitch", "fov",
                "capture_date", "image_path", "status",
            ]
        )
        empty.to_csv(cfg.panorama_index_csv, index=False)
        return empty

    if image_df.empty:
        idx = ok.assign(
            heading=cfg.headings[0] if cfg.headings else 0,
            pitch=cfg.pitch,
            fov=cfg.fov,
            image_path="",
        )[["point_id", "pano_id", "heading", "pitch", "fov",
           "capture_date", "status"]]
        idx["image_path"] = ""
    else:
        idx = image_df.merge(
            ok[["point_id", "pano_id", "capture_date"]],
            on=["point_id", "pano_id"],
            how="inner",
        )[["point_id", "pano_id", "heading", "pitch", "fov",
           "capture_date", "image_path", "status"]]

    # Deduplicate identical (point_id, pano_id, heading) rows.
    idx = idx.drop_duplicates(
        subset=["point_id", "pano_id", "heading"]
    ).reset_index(drop=True)

    idx.to_csv(cfg.panorama_index_csv, index=False)
    logger.info(
        "wrote %s (%d rows, %d unique panos)",
        cfg.panorama_index_csv,
        len(idx),
        idx["pano_id"].nunique(),
    )
    return idx


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------
def run(cfg: PipelineConfig = CONFIG) -> Dict:
    cfg.ensure_dirs()
    logger = setup_logger(cfg.log_dir)
    if not cfg.api_key:
        raise RuntimeError(
            "GSV_API_KEY is not set. Export it or edit config.PipelineConfig."
        )

    logger.info("starting pipeline | input=%s output=%s", cfg.input_csv, cfg.output_dir)

    session = build_session(cfg.max_retries, cfg.backoff_factor)
    limiter = RateLimiter(cfg.rate_limit_qps)
    cache = JsonCache(cfg.cache_dir)

    meta_client = MetadataClient(cfg, session, limiter, cache, logger)
    downloader = ImageDownloader(cfg, session, limiter, logger)

    points = load_points(cfg.input_csv, logger)

    existing_meta = load_existing_metadata(cfg.metadata_csv)
    pending = select_pending(points, existing_meta)
    logger.info(
        "resume: %d/%d points already processed, %d pending",
        len(points) - len(pending), len(points), len(pending),
    )

    # --- Metadata stage in batches -----------------------------------------
    all_meta: List[PanoMetadata] = []
    for start in range(0, len(pending), cfg.batch_size):
        batch = pending.iloc[start : start + cfg.batch_size]
        logger.info(
            "metadata batch %d-%d", start, start + len(batch) - 1
        )
        all_meta.extend(run_metadata_stage(batch, meta_client, cfg))

    new_meta_df = pd.DataFrame([m.to_dict() for m in all_meta])

    # Combine with whatever was already saved (resume).
    if not existing_meta.empty:
        # The existing CSV uses the public schema; map columns back so we
        # can reuse it without re-fetching.
        existing_internal = pd.DataFrame({
            "point_id": existing_meta["point_id"],
            "query_lat": existing_meta["latitude"],
            "query_lon": existing_meta["longitude"],
            "status": existing_meta["status"],
            "pano_id": existing_meta.get("pano_id"),
            "capture_date": existing_meta.get("capture_date"),
            "pano_lat": existing_meta["latitude"],
            "pano_lon": existing_meta["longitude"],
            "copyright": existing_meta.get("copyright"),
            "source": existing_meta.get("source"),
            "error": None,
        })
        full_meta_df = pd.concat(
            [existing_internal, new_meta_df], ignore_index=True
        ).drop_duplicates(subset=["point_id"], keep="last")
    else:
        full_meta_df = new_meta_df

    # --- Image stage --------------------------------------------------------
    ok_meta = full_meta_df[
        (full_meta_df["status"] == "OK") & full_meta_df["pano_id"].notna()
    ]
    # Deduplicate by pano_id so we don't redownload the same panorama for
    # multiple nearby points; keep the first point_id we saw it for.
    unique_panos = ok_meta.drop_duplicates(subset=["pano_id"], keep="first")
    pano_jobs: List[Tuple[Any, str]] = list(
        zip(unique_panos["point_id"], unique_panos["pano_id"])
    )
    logger.info(
        "image stage: %d unique panoramas across %d covered points",
        len(pano_jobs), len(ok_meta),
    )

    image_records = run_image_stage(pano_jobs, downloader, cfg)
    image_df = pd.DataFrame([asdict(r) for r in image_records])

    # --- Outputs ------------------------------------------------------------
    public_df = write_metadata_outputs(full_meta_df, image_df, cfg, logger)
    write_panorama_index(full_meta_df, image_df, cfg, logger)

    # --- Visualizations + summary ------------------------------------------
    build_sample_points_map(points, cfg.output_dir / "map_sample_points.html")
    build_panorama_map(full_meta_df, cfg.output_dir / "map_panoramas.html")
    summary = compute_summary(
        points, full_meta_df, image_df, cfg.summary_json, logger
    )

    logger.info("pipeline finished")
    return summary
