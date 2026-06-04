# Google Street View Collection Pipeline

Production-ready Python pipeline for collecting Google Street View
metadata and panorama imagery for road sample points (e.g. 100 m-spaced
points across Delhi).

## Features

- Street View **Metadata API** probe per point (free; used to avoid
  wasted image-API quota)
- **Static Image API** download with configurable size / heading / pitch / FOV
- Concurrent fan-out via `ThreadPoolExecutor` + client-side QPS throttle
- Built-in retries with exponential backoff for `429/5xx`
- On-disk JSON cache for metadata + image skip-if-exists for resume
- Outputs `metadata.csv`, `metadata.geojson`, `panorama_index.csv`,
  `summary.json`
- Folium HTML maps for sample points and successful panoramas
- Per-segment coverage stats if the input CSV has `segment_id`

## Layout

```
config.py          # all tunable parameters
utils.py           # logging, retrying session, JSON cache, rate limiter
metadata.py        # Street View Metadata API client
downloader.py      # Static Image API downloader
visualization.py   # Folium maps + summary stats
pipeline.py        # orchestration (metadata → images → outputs)
main.py            # CLI entry point
requirements.txt
```

## Input format

CSV with at minimum these columns:

```
point_id,latitude,longitude
1,28.6139,77.2090
2,28.6140,77.2095
...
```

Optional column `segment_id` enables per-segment coverage stats.

## Quick start

```bash
pip install -r requirements.txt
export GSV_API_KEY="YOUR_GOOGLE_API_KEY"

python main.py \
    --input data/road_points_delhi.csv \
    --output output/ \
    --headings 0,90,180,270 \
    --fov 90 --pitch 0 \
    --image-size 640x640 \
    --batch-size 100 --max-workers 8 --qps 25
```

Re-runs are resumable: existing `metadata.csv` rows are kept and
already-downloaded images on disk are skipped.

## Outputs

| File | Description |
|------|-------------|
| `output/metadata.csv` | One row per input point (status, pano_id, capture_date, lat/lon, image_path, heading, pitch, fov, copyright, source) |
| `output/metadata.geojson` | Same data as point features (EPSG:4326) |
| `output/panorama_index.csv` | One row per (point_id, pano_id, heading) |
| `output/images/{point_id}_{pano_id}_h{heading}.jpg` | Downloaded JPEGs |
| `output/map_sample_points.html` | Folium map of all input points |
| `output/map_panoramas.html` | Folium map of successful panoramas |
| `output/summary.json` | Coverage stats (+ per-segment if available) |
| `output/logs/pipeline.log` | Rotating log file |
| `output/.cache/*.json` | Cached metadata responses |

## Configuration

Edit `config.PipelineConfig` or pass CLI flags. Important knobs:

- `rate_limit_qps` — client-side throttle (Street View quota is typically
  much higher than 25 QPS but keep this conservative).
- `max_workers` — concurrent HTTP threads.
- `headings` — list of compass headings to capture per panorama.
- `radius_m` — metadata search radius (Google snaps to nearest pano).
- `source` — `"outdoor"` excludes user-contributed/indoor panoramas.

## Notes

- The Metadata API is **free**; the Static Image API is billed per
  request. The pipeline always probes metadata first and only downloads
  images for `status == "OK"` panoramas.
- Panoramas are deduplicated globally before image download so a single
  pano shared by multiple nearby points is fetched once.
- For very large jobs, increase `batch_size` and consider sharding the
  input CSV across multiple worker machines.
