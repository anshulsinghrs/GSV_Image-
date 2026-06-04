"""
Entry point.

Usage:
    export GSV_API_KEY=...
    python main.py --input data/road_points_delhi.csv --output output/

All flags are optional; defaults come from config.PipelineConfig.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from gsv_points import CONFIG, run


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Google Street View collection pipeline")
    p.add_argument("--input", type=Path, default=CONFIG.input_csv,
                   help="Input CSV with columns point_id, latitude, longitude")
    p.add_argument("--output", type=Path, default=CONFIG.output_dir,
                   help="Output directory")
    p.add_argument("--api-key", type=str, default=CONFIG.api_key,
                   help="Google API key (defaults to env GSV_API_KEY)")
    p.add_argument("--image-size", type=str, default=CONFIG.image_size)
    p.add_argument("--fov", type=int, default=CONFIG.fov)
    p.add_argument("--pitch", type=int, default=CONFIG.pitch)
    p.add_argument("--headings", type=str,
                   default=",".join(map(str, CONFIG.headings)),
                   help="Comma-separated heading list, e.g. 0,90,180,270")
    p.add_argument("--batch-size", type=int, default=CONFIG.batch_size)
    p.add_argument("--max-workers", type=int, default=CONFIG.max_workers)
    p.add_argument("--qps", type=float, default=CONFIG.rate_limit_qps)
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Mutate the singleton CONFIG so every module sees the same settings.
    CONFIG.input_csv = args.input
    CONFIG.output_dir = args.output
    CONFIG.points_dir = args.output / "gsv_points"
    CONFIG.images_dir = args.output / "gsv_images"
    CONFIG.cache_dir = CONFIG.points_dir / ".cache"
    CONFIG.log_dir = CONFIG.points_dir / "logs"
    CONFIG.metadata_csv = CONFIG.points_dir / "metadata.csv"
    CONFIG.metadata_geojson = CONFIG.points_dir / "metadata.geojson"
    CONFIG.panorama_index_csv = CONFIG.points_dir / "panorama_index.csv"
    CONFIG.summary_json = CONFIG.points_dir / "summary.json"
    CONFIG.api_key = args.api_key
    CONFIG.image_size = args.image_size
    CONFIG.fov = args.fov
    CONFIG.pitch = args.pitch
    CONFIG.headings = [int(h) for h in args.headings.split(",") if h.strip()]
    CONFIG.batch_size = args.batch_size
    CONFIG.max_workers = args.max_workers
    CONFIG.rate_limit_qps = args.qps

    summary = run(CONFIG)
    print("\n=== Summary ===")
    for k, v in summary.items():
        if k == "per_segment_coverage":
            print(f"{k}: {len(v)} segments")
        else:
            print(f"{k}: {v}")


if __name__ == "__main__":
    main()
