"""
City Road Network Sampler
-------------------------
Extracts the complete drivable road network of a target city from
OpenStreetMap (via OSMnx) and generates evenly spaced sample points
(default 100 m) along every road segment.

Although the default configuration targets "Delhi, India", every
parameter is exposed so the script can be reused for any city worldwide
without changing the source code (use the CLI flags).

Outputs produced (written into ``--output-dir``):
    * <city>_roads.geojson        - full road network
    * <city>_roads.shp            - same, as ESRI Shapefile
    * <city>_points.geojson       - sampled points
    * <city>_points.csv           - sampled points (tabular)
    * <city>_points.shp           - sampled points (Shapefile)
    * <city>_preview.png          - quick-look visualisation

Author: Senior GIS / Python Developer
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import osmnx as ox
import pandas as pd
from shapely.geometry import LineString, MultiLineString, Point


# ---------------------------------------------------------------------------
# Configuration & logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("road_sampler")


@dataclass
class SamplerConfig:
    """All knobs that make the pipeline portable across cities."""

    place: str = "Delhi, India"          # OSM Nominatim query
    network_type: str = "drive"          # drivable roads only
    interval_m: float = 100.0            # sampling distance in meters
    output_dir: str = "output"           # where to write artifacts
    target_crs: Optional[str] = None     # override projected CRS; auto if None
    simplify_graph: bool = True          # keep OSMnx graph compact

    @property
    def slug(self) -> str:
        """Filesystem-safe identifier derived from the place name."""
        token = self.place.split(",")[0].strip().lower()
        return re.sub(r"[^a-z0-9]+", "_", token).strip("_") or "city"


# ---------------------------------------------------------------------------
# Data acquisition
# ---------------------------------------------------------------------------

def download_road_network(cfg: SamplerConfig):
    """Pull the drivable graph for ``cfg.place`` from OpenStreetMap.

    OSMnx handles Nominatim geocoding, polygon retrieval and Overpass
    download internally; we only need to surface progress + errors so a
    very large city does not silently stall.
    """
    log.info("Downloading drivable road graph for '%s' ...", cfg.place)
    t0 = time.time()
    try:
        graph = ox.graph_from_place(
            cfg.place,
            network_type=cfg.network_type,
            simplify=cfg.simplify_graph,
            retain_all=False,
        )
    except Exception as exc:
        # Nominatim sometimes fails on ambiguous names; surface a clear hint
        raise RuntimeError(
            f"Failed to download network for '{cfg.place}'. "
            "Try a more specific query (e.g. 'New Delhi, Delhi, India')."
        ) from exc

    log.info(
        "Graph ready: %s nodes / %s edges in %.1fs",
        len(graph.nodes), len(graph.edges), time.time() - t0,
    )
    return graph


# ---------------------------------------------------------------------------
# Data processing
# ---------------------------------------------------------------------------

def graph_to_edges_gdf(graph) -> gpd.GeoDataFrame:
    """Convert the MultiDiGraph into an edge GeoDataFrame (EPSG:4326)."""
    edges = ox.graph_to_gdfs(graph, nodes=False, edges=True, fill_edge_geometry=True)
    edges = edges.reset_index()  # expose u, v, key columns
    edges = edges[edges.geometry.notna() & ~edges.geometry.is_empty].copy()
    log.info("Edge GeoDataFrame: %d valid road segments", len(edges))
    return edges


def pick_projected_crs(gdf: gpd.GeoDataFrame, override: Optional[str]) -> str:
    """Choose a metric CRS suitable for distance work.

    Prefers the user override, then a UTM zone derived from the data
    centroid, then EPSG:3857 as a last-resort fallback.
    """
    if override:
        return override
    try:
        return gdf.estimate_utm_crs().to_string()
    except Exception:
        log.warning("UTM estimation failed - falling back to EPSG:3857")
        return "EPSG:3857"


def reproject(gdf: gpd.GeoDataFrame, crs: str) -> gpd.GeoDataFrame:
    """Return ``gdf`` reprojected to ``crs`` (logs the transition)."""
    log.info("Reprojecting %d features %s -> %s", len(gdf), gdf.crs, crs)
    return gdf.to_crs(crs)


# ---------------------------------------------------------------------------
# Point generation
# ---------------------------------------------------------------------------

def _iter_linestrings(geom) -> Iterable[LineString]:
    """Yield each LineString contained in ``geom`` (handles Multi*)."""
    if geom is None or geom.is_empty:
        return
    if isinstance(geom, LineString):
        yield geom
    elif isinstance(geom, MultiLineString):
        yield from geom.geoms
    # silently ignore unexpected geometry types (Points / Polygons)


def sample_line(line: LineString, interval: float) -> List[Tuple[Point, float]]:
    """Return (point, distance_from_start) tuples along ``line``.

    Includes both endpoints. Uses ``interpolate`` which respects the
    line's underlying CRS units - so make sure the input is projected
    to meters before calling.
    """
    length = line.length
    if length <= 0:
        return []

    # np.arange can drift past length due to fp error; clamp explicitly
    distances = list(np.arange(0.0, length, interval))
    if distances[-1] < length:
        distances.append(length)

    return [(line.interpolate(d), float(d)) for d in distances]


def _clean(value, default="unknown"):
    """Normalise OSM attribute values which may be lists or NaN."""
    if isinstance(value, list):
        return value[0] if value else default
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return default
    return value


def generate_sample_points(
    edges_proj: gpd.GeoDataFrame,
    interval_m: float,
) -> gpd.GeoDataFrame:
    """Walk every projected edge and emit sample points at ``interval_m``.

    Designed to scale: iterates with ``itertuples`` (no per-row Series
    creation) and accumulates plain dicts before a single GeoDataFrame
    constructor call at the end.
    """
    log.info("Sampling points at %.1f m intervals ...", interval_m)
    records: list[dict] = []
    point_id = 0
    skipped = 0
    total = len(edges_proj)
    report_every = max(1, total // 20)  # log ~20 progress lines

    for idx, row in enumerate(edges_proj.itertuples(index=False), start=1):
        geom = getattr(row, "geometry", None)
        try:
            for line in _iter_linestrings(geom):
                osmid = _clean(getattr(row, "osmid", None))
                name = _clean(getattr(row, "name", None), default="unnamed")
                highway = _clean(getattr(row, "highway", None))

                for pt, dist in sample_line(line, interval_m):
                    records.append({
                        "point_id": point_id,
                        "road_name": name,
                        "osm_way_id": osmid,
                        "road_type": highway,
                        "distance_along_road": round(dist, 3),
                        "geometry": pt,
                    })
                    point_id += 1
        except Exception as exc:  # never abort the whole run for one bad row
            skipped += 1
            log.debug("Skipped segment %d: %s", idx, exc)

        if idx % report_every == 0:
            log.info("  processed %d / %d segments (%.0f%%)",
                     idx, total, 100 * idx / total)

    if skipped:
        log.warning("Skipped %d problematic segments", skipped)

    if not records:
        raise RuntimeError("No sample points generated - check input geometries.")

    points = gpd.GeoDataFrame(records, geometry="geometry", crs=edges_proj.crs)
    log.info("Generated %d sample points", len(points))
    return points


def attach_lat_lon(points_proj: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Add WGS84 latitude/longitude columns without losing the projected geom."""
    geographic = points_proj.to_crs("EPSG:4326").geometry
    out = points_proj.copy()
    out["longitude"] = geographic.x.round(7)
    out["latitude"] = geographic.y.round(7)
    return out


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _sanitize_for_shapefile(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Shapefiles cannot store list-typed columns; stringify them."""
    out = gdf.copy()
    for col in out.columns:
        if col == out.geometry.name:
            continue
        if out[col].apply(lambda v: isinstance(v, (list, dict))).any():
            out[col] = out[col].astype(str)
    return out


def save_outputs(
    edges_wgs84: gpd.GeoDataFrame,
    points_wgs84: gpd.GeoDataFrame,
    cfg: SamplerConfig,
) -> dict:
    """Write all deliverables. Returns a mapping of label -> path."""
    os.makedirs(cfg.output_dir, exist_ok=True)
    slug = cfg.slug
    paths = {
        "roads_geojson":  os.path.join(cfg.output_dir, f"{slug}_roads.geojson"),
        "roads_shp":      os.path.join(cfg.output_dir, f"{slug}_roads.shp"),
        "points_geojson": os.path.join(cfg.output_dir, f"{slug}_points.geojson"),
        "points_csv":     os.path.join(cfg.output_dir, f"{slug}_points.csv"),
        "points_shp":     os.path.join(cfg.output_dir, f"{slug}_points.shp"),
    }

    log.info("Writing road network GeoJSON ...")
    _sanitize_for_shapefile(edges_wgs84).to_file(paths["roads_geojson"], driver="GeoJSON")

    log.info("Writing road network Shapefile ...")
    _sanitize_for_shapefile(edges_wgs84).to_file(paths["roads_shp"], driver="ESRI Shapefile")

    log.info("Writing sample points GeoJSON ...")
    points_wgs84.to_file(paths["points_geojson"], driver="GeoJSON")

    log.info("Writing sample points CSV ...")
    csv_cols = ["point_id", "latitude", "longitude", "road_name",
                "osm_way_id", "road_type", "distance_along_road"]
    points_wgs84[csv_cols].to_csv(paths["points_csv"], index=False)

    log.info("Writing sample points Shapefile ...")
    _sanitize_for_shapefile(points_wgs84).to_file(paths["points_shp"], driver="ESRI Shapefile")

    return paths


# ---------------------------------------------------------------------------
# Statistics & visualization
# ---------------------------------------------------------------------------

def print_statistics(
    edges_proj: gpd.GeoDataFrame,
    points_proj: gpd.GeoDataFrame,
    cfg: SamplerConfig,
) -> None:
    """Log key network + sampling metrics."""
    total_length_m = float(edges_proj.geometry.length.sum())
    n_segments = len(edges_proj)
    n_points = len(points_proj)
    avg_spacing = total_length_m / max(n_points - n_segments, 1)

    print("\n" + "=" * 60)
    print(f" Road network summary for: {cfg.place}")
    print("=" * 60)
    print(f"  Total road length      : {total_length_m / 1000:,.2f} km")
    print(f"  Number of road segments: {n_segments:,}")
    print(f"  Number of sample points: {n_points:,}")
    print(f"  Requested spacing      : {cfg.interval_m:.1f} m")
    print(f"  Avg effective spacing  : {avg_spacing:,.2f} m")
    print("=" * 60 + "\n")


def make_preview(
    edges_wgs84: gpd.GeoDataFrame,
    points_wgs84: gpd.GeoDataFrame,
    cfg: SamplerConfig,
) -> str:
    """Render a static PNG showing roads + sampled points."""
    out_path = os.path.join(cfg.output_dir, f"{cfg.slug}_preview.png")
    log.info("Rendering quick-look map -> %s", out_path)

    fig, ax = plt.subplots(figsize=(11, 11))
    edges_wgs84.plot(ax=ax, linewidth=0.4, color="#444444", zorder=1)
    points_wgs84.plot(
        ax=ax, markersize=0.5, color="#E64A19", alpha=0.6, zorder=2,
    )
    ax.set_title(
        f"{cfg.place} - drivable network & {int(cfg.interval_m)} m sample points",
        fontsize=13,
    )
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal")
    fig.tight_layout()
    fig.savefig(out_path, dpi=200)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Pipeline + CLI
# ---------------------------------------------------------------------------

def run(cfg: SamplerConfig) -> None:
    """End-to-end pipeline. Each stage is logged for traceability."""
    graph = download_road_network(cfg)
    edges_wgs84 = graph_to_edges_gdf(graph)

    projected_crs = pick_projected_crs(edges_wgs84, cfg.target_crs)
    edges_proj = reproject(edges_wgs84, projected_crs)

    points_proj = generate_sample_points(edges_proj, cfg.interval_m)
    points_proj = attach_lat_lon(points_proj)
    points_wgs84 = points_proj.to_crs("EPSG:4326")

    paths = save_outputs(edges_wgs84, points_wgs84, cfg)
    print_statistics(edges_proj, points_proj, cfg)
    preview = make_preview(edges_wgs84, points_wgs84, cfg)

    log.info("Done. Artifacts:")
    for label, p in {**paths, "preview_png": preview}.items():
        log.info("  %-15s %s", label, p)


def parse_args(argv: Optional[List[str]] = None) -> SamplerConfig:
    parser = argparse.ArgumentParser(
        description="Sample a city's drivable road network at fixed intervals.",
    )
    parser.add_argument("--place", default="Delhi, India",
                        help="OSM place query (default: 'Delhi, India').")
    parser.add_argument("--network-type", default="drive",
                        choices=["drive", "drive_service", "all", "all_private",
                                 "bike", "walk"],
                        help="OSMnx network_type filter (default: drive).")
    parser.add_argument("--interval", type=float, default=100.0,
                        help="Sampling interval in meters (default: 100).")
    parser.add_argument("--output-dir", default="output",
                        help="Directory for output files (default: ./output).")
    parser.add_argument("--crs", default=None,
                        help="Override projected CRS (e.g. EPSG:32643).")
    parser.add_argument("--no-simplify", action="store_true",
                        help="Disable OSMnx graph simplification.")
    args = parser.parse_args(argv)

    return SamplerConfig(
        place=args.place,
        network_type=args.network_type,
        interval_m=args.interval,
        output_dir=args.output_dir,
        target_crs=args.crs,
        simplify_graph=not args.no_simplify,
    )


def main(argv: Optional[List[str]] = None) -> int:
    cfg = parse_args(argv)
    try:
        run(cfg)
    except KeyboardInterrupt:
        log.error("Interrupted by user.")
        return 130
    except Exception as exc:
        log.exception("Pipeline failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
