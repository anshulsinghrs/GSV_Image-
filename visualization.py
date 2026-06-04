"""
Lightweight Folium visualizations and summary statistics.

Produces two HTML maps (sample points and successful panoramas) plus a
JSON summary file with coverage stats. Optionally computes coverage per
road segment if the input CSV carries a `segment_id` column.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

import folium
import pandas as pd


def build_sample_points_map(
    points_df: pd.DataFrame, output_html: Path
) -> None:
    """All input points, colour-blind blue."""
    if points_df.empty:
        return
    center = [points_df["latitude"].mean(), points_df["longitude"].mean()]
    m = folium.Map(location=center, zoom_start=12, tiles="OpenStreetMap")
    for _, row in points_df.iterrows():
        folium.CircleMarker(
            location=[row["latitude"], row["longitude"]],
            radius=2,
            color="#1f77b4",
            fill=True,
            fill_opacity=0.7,
            popup=f"point_id: {row['point_id']}",
        ).add_to(m)
    m.save(str(output_html))


def build_panorama_map(
    metadata_df: pd.DataFrame, output_html: Path
) -> None:
    """Successful panoramas, green markers at the imagery location."""
    ok = metadata_df[metadata_df["status"] == "OK"].dropna(
        subset=["pano_lat", "pano_lon"]
    )
    if ok.empty:
        return
    center = [ok["pano_lat"].mean(), ok["pano_lon"].mean()]
    m = folium.Map(location=center, zoom_start=12, tiles="OpenStreetMap")
    for _, row in ok.iterrows():
        folium.CircleMarker(
            location=[row["pano_lat"], row["pano_lon"]],
            radius=3,
            color="#2ca02c",
            fill=True,
            fill_opacity=0.8,
            popup=(
                f"pano_id: {row['pano_id']}<br>"
                f"date: {row.get('capture_date', '')}"
            ),
        ).add_to(m)
    m.save(str(output_html))


def compute_summary(
    points_df: pd.DataFrame,
    metadata_df: pd.DataFrame,
    image_df: pd.DataFrame,
    output_json: Path,
    logger: logging.Logger,
) -> Dict:
    """Coverage stats; also per-segment if segment_id is present."""
    total = len(points_df)
    ok = metadata_df[metadata_df["status"] == "OK"]
    no_imagery = metadata_df[metadata_df["status"] == "ZERO_RESULTS"]
    errors = metadata_df[
        ~metadata_df["status"].isin(["OK", "ZERO_RESULTS"])
    ]

    summary = {
        "total_points": int(total),
        "points_with_imagery": int(len(ok)),
        "points_without_imagery": int(len(no_imagery)),
        "points_with_errors": int(len(errors)),
        "coverage_pct": round(100.0 * len(ok) / total, 2) if total else 0.0,
        "unique_panoramas": int(ok["pano_id"].nunique()) if not ok.empty else 0,
        "images_downloaded": int(
            (image_df["status"] == "OK").sum()
        ) if not image_df.empty else 0,
        "images_skipped": int(
            (image_df["status"] == "SKIPPED").sum()
        ) if not image_df.empty else 0,
        "image_errors": int(
            (image_df["status"] == "ERROR").sum()
        ) if not image_df.empty else 0,
    }

    if "segment_id" in points_df.columns:
        joined = points_df.merge(
            metadata_df[["point_id", "status"]], on="point_id", how="left"
        )
        per_segment = (
            joined.assign(_ok=(joined["status"] == "OK").astype(int))
            .groupby("segment_id")
            .agg(points=("point_id", "count"), with_imagery=("_ok", "sum"))
            .reset_index()
        )
        per_segment["coverage_pct"] = (
            100.0 * per_segment["with_imagery"] / per_segment["points"]
        ).round(2)
        summary["per_segment_coverage"] = per_segment.to_dict(orient="records")

    output_json.parent.mkdir(parents=True, exist_ok=True)
    with output_json.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    logger.info(
        "summary: %d/%d points covered (%.2f%%), %d unique panos",
        summary["points_with_imagery"],
        summary["total_points"],
        summary["coverage_pct"],
        summary["unique_panoramas"],
    )
    return summary
