"""NASA POWER daily precipitation fetching, caching, and rainfall anomaly index R(e)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import networkx as nx
import numpy as np
import pandas as pd
import shapely
from scipy.spatial import cKDTree

from floodrouter.config import AppConfig, load_config
from floodrouter.ingest import project_to_metric
from floodrouter.manifest import create_manifest_entry, upsert_manifest_entry
from floodrouter.network import load_edges_parquet, load_graph_pickle

logger = logging.getLogger(__name__)

MAX_CONCURRENT_REQUESTS = 6
DEFAULT_HTTP_TIMEOUT_S = 30


def _parse_iso_date(date_str: str) -> date:
    """Parse an ISO YYYY-MM-DD string into a datetime.date object."""
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid ISO date '{date_str}'; expected YYYY-MM-DD.") from exc


def _safe_prior_year_date(ref_date: date, target_year: int) -> date:
    """Return ref_date shifted to target_year, mapping Feb 29 to Feb 28 in non-leap years."""
    try:
        return ref_date.replace(year=target_year)
    except ValueError:
        return date(target_year, ref_date.month, 28)


def generate_lattice_points(
    bbox: list[float] | tuple[float, float, float, float],
    step: float,
) -> list[tuple[float, float]]:
    """Generate a regular (lon, lat) grid covering bbox with spacing step in degrees."""
    if step <= 0.0:
        raise ValueError("Grid step must be positive.")
    min_lon, min_lat, max_lon, max_lat = (
        float(bbox[0]),
        float(bbox[1]),
        float(bbox[2]),
        float(bbox[3]),
    )
    if min_lon > max_lon or min_lat > max_lat:
        raise ValueError(f"Invalid bounding box: {bbox}")

    n_lon = max(1, int(np.ceil((max_lon - min_lon) / step)))
    n_lat = max(1, int(np.ceil((max_lat - min_lat) / step)))
    lons = [round(min_lon + i * step, 4) for i in range(n_lon + 1)]
    lats = [round(min_lat + j * step, 4) for j in range(n_lat + 1)]

    points: list[tuple[float, float]] = []
    for lat in lats:
        for lon in lons:
            points.append((lon, lat))
    return points


def compute_climatology_date_range(
    as_of: str,
    window_days: int,
    climatology_years: int,
) -> tuple[str, str]:
    """Return (start_iso, end_iso) covering full years from (year - climatology_years) to year."""
    ref = _parse_iso_date(as_of)
    earliest_year = ref.year - climatology_years
    start_dt = date(earliest_year, 1, 1) - timedelta(days=max(1, window_days))
    end_dt = ref
    return start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")


def _find_covering_cache_file(
    cache_dir: Path,
    lon: float,
    lat: float,
    start_compact: str,
    end_compact: str,
) -> Path | None:
    """Return an existing cache file for (lon, lat) whose date range covers [start, end]."""
    prefix = f"power_{lat:.4f}_{lon:.4f}_"
    if not cache_dir.exists():
        return None
    for cand in sorted(cache_dir.glob(f"{prefix}*.json")):
        stem_parts = cand.stem.split("_")
        if len(stem_parts) >= 5:
            c_start = stem_parts[-2]
            c_end = stem_parts[-1]
            if c_start <= start_compact and c_end >= end_compact:
                return cand
    return None


async def _fetch_single_point_async(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    lon: float,
    lat: float,
    start_compact: str,
    end_compact: str,
    cache_dir: Path,
    api_base_url: str,
    parameter: str,
    max_retries: int = 3,
) -> dict[str, Any]:
    """Fetch or load cached NASA POWER daily precipitation JSON for one (lon, lat) point."""
    existing = _find_covering_cache_file(cache_dir, lon, lat, start_compact, end_compact)
    if existing is not None:
        raw = json.loads(existing.read_text(encoding="utf-8"))
        series = raw.get("properties", {}).get("parameter", {}).get(parameter, {})
        return {"lon": lon, "lat": lat, "series": series, "cache_path": str(existing)}

    target_cache = cache_dir / f"power_{lat:.4f}_{lon:.4f}_{start_compact}_{end_compact}.json"
    params = {
        "parameters": parameter,
        "community": "AG",
        "longitude": lon,
        "latitude": lat,
        "start": start_compact,
        "end": end_compact,
        "format": "JSON",
    }

    async with semaphore:
        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                resp = await client.get(api_base_url, params=params)
                if resp.status_code == 429 or resp.status_code >= 500:
                    await asyncio.sleep(float(attempt + 1))
                    continue
                resp.raise_for_status()
                payload = resp.json()
                cache_dir.mkdir(parents=True, exist_ok=True)
                target_cache.write_text(json.dumps(payload), encoding="utf-8")
                series = payload.get("properties", {}).get("parameter", {}).get(parameter, {})
                return {"lon": lon, "lat": lat, "series": series, "cache_path": str(target_cache)}
            except Exception as exc:
                last_err = exc
                await asyncio.sleep(float(attempt + 1))
        raise RuntimeError(
            f"Failed to fetch NASA POWER data for ({lon}, {lat}): {last_err}"
        ) from last_err


def fetch_grid(
    bbox: list[float] | tuple[float, float, float, float],
    step: float,
    date_range: tuple[str, str],
    cache_dir: str | Path = "data/raw/power",
    api_base_url: str = "https://power.larc.nasa.gov/api/temporal/daily/point",
    parameter: str = "PRECTOTCORR",
    max_retries: int = 3,
) -> list[dict[str, Any]]:
    """Sample lattice points over bbox, fetch NASA POWER daily precipitation, and cache JSON."""
    start_dt = _parse_iso_date(date_range[0])
    end_dt = _parse_iso_date(date_range[1])
    if start_dt > end_dt:
        raise ValueError(f"date_range start {date_range[0]} is after end {date_range[1]}.")

    start_compact = start_dt.strftime("%Y%m%d")
    end_compact = end_dt.strftime("%Y%m%d")
    points = generate_lattice_points(bbox, step)
    c_dir = Path(cache_dir)

    async def _run_all() -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        timeout = httpx.Timeout(DEFAULT_HTTP_TIMEOUT_S)
        async with httpx.AsyncClient(timeout=timeout) as client:
            tasks = [
                _fetch_single_point_async(
                    client=client,
                    semaphore=semaphore,
                    lon=lon,
                    lat=lat,
                    start_compact=start_compact,
                    end_compact=end_compact,
                    cache_dir=c_dir,
                    api_base_url=api_base_url,
                    parameter=parameter,
                    max_retries=max_retries,
                )
                for lon, lat in points
            ]
            return await asyncio.gather(*tasks)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        results: list[dict[str, Any]] = []
        for lon, lat in points:
            existing = _find_covering_cache_file(c_dir, lon, lat, start_compact, end_compact)
            if existing is not None:
                raw = json.loads(existing.read_text(encoding="utf-8"))
                series = raw.get("properties", {}).get("parameter", {}).get(parameter, {})
                results.append(
                    {"lon": lon, "lat": lat, "series": series, "cache_path": str(existing)}
                )
                continue
            target_cache = c_dir / f"power_{lat:.4f}_{lon:.4f}_{start_compact}_{end_compact}.json"
            params = {
                "parameters": parameter,
                "community": "AG",
                "longitude": lon,
                "latitude": lat,
                "start": start_compact,
                "end": end_compact,
                "format": "JSON",
            }
            for attempt in range(max_retries):
                resp = httpx.get(api_base_url, params=params, timeout=DEFAULT_HTTP_TIMEOUT_S)
                if resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(float(attempt + 1))
                    continue
                resp.raise_for_status()
                payload = resp.json()
                c_dir.mkdir(parents=True, exist_ok=True)
                target_cache.write_text(json.dumps(payload), encoding="utf-8")
                series = payload.get("properties", {}).get("parameter", {}).get(parameter, {})
                results.append(
                    {"lon": lon, "lat": lat, "series": series, "cache_path": str(target_cache)}
                )
                break
        return results

    return asyncio.run(_run_all())


def _window_sum(series: dict[str, Any], end_date: date, window_days: int) -> float | None:
    """Compute sum of valid non-negative daily precipitation over window_days ending at end_date."""
    vals: list[float] = []
    for offset in range(window_days):
        d_str = (end_date - timedelta(days=offset)).strftime("%Y%m%d")
        if d_str not in series:
            continue
        val = float(series[d_str])
        if np.isfinite(val) and val >= 0.0:
            vals.append(val)
    if not vals:
        return None
    return float(np.sum(vals))


def compute_cell_anomaly(
    series: dict[str, Any],
    as_of_date: date,
    window_days: int,
    climatology_years: int,
    scale_percentile: float,
    eps: float,
) -> dict[str, float]:
    """Compute (recent - clim_median) / max(clim_p95 - clim_median, eps) clipped to [0, 1]."""
    recent = _window_sum(series, as_of_date, window_days)
    if recent is None:
        return {"recent_mm": 0.0, "clim_median_mm": 0.0, "clim_p95_mm": 0.0, "rain_idx": 0.0}

    clim_sums: list[float] = []
    for yr_offset in range(1, climatology_years + 1):
        prior_year = as_of_date.year - yr_offset
        prior_end = _safe_prior_year_date(as_of_date, prior_year)
        w_sum = _window_sum(series, prior_end, window_days)
        if w_sum is not None:
            clim_sums.append(w_sum)

    if not clim_sums:
        return {
            "recent_mm": recent,
            "clim_median_mm": recent,
            "clim_p95_mm": recent,
            "rain_idx": 0.0,
        }

    clim_arr = np.array(clim_sums, dtype=np.float64)
    clim_median = float(np.median(clim_arr))
    clim_p95 = float(np.percentile(clim_arr, scale_percentile))
    denom = max(clim_p95 - clim_median, eps)
    raw_anomaly = (recent - clim_median) / denom
    rain_idx = float(np.clip(raw_anomaly, 0.0, 1.0))
    return {
        "recent_mm": recent,
        "clim_median_mm": clim_median,
        "clim_p95_mm": clim_p95,
        "rain_idx": rain_idx,
    }


def rain_index(
    grid: list[dict[str, Any]],
    as_of: str,
    window_days: int,
    climatology_years: int,
    edges: pd.DataFrame | None = None,
    scale_percentile: float = 100.0,
    eps: float = 1.0,
    lon0: float | None = None,
    lat0: float | None = None,
) -> pd.DataFrame:
    """Compute rainfall anomaly index in [0, 1] per grid cell and assign to nearest road edges."""
    if not grid:
        raise ValueError("Grid cannot be empty.")
    if window_days <= 0 or climatology_years <= 0:
        raise ValueError("window_days and climatology_years must be positive.")

    as_of_dt = _parse_iso_date(as_of)
    cell_rows: list[dict[str, Any]] = []
    for cell in grid:
        lon = float(cell["lon"])
        lat = float(cell["lat"])
        series = cell.get("series", {})
        stats = compute_cell_anomaly(
            series=series,
            as_of_date=as_of_dt,
            window_days=window_days,
            climatology_years=climatology_years,
            scale_percentile=scale_percentile,
            eps=eps,
        )
        cell_rows.append(
            {
                "lon": lon,
                "lat": lat,
                "recent_mm": stats["recent_mm"],
                "clim_median_mm": stats["clim_median_mm"],
                "clim_p95_mm": stats["clim_p95_mm"],
                "rain_idx": stats["rain_idx"],
            }
        )

    cells_df = pd.DataFrame(cell_rows)
    if edges is None:
        return cells_df

    if len(edges) == 0:
        return pd.DataFrame(columns=["edge_id", "rain_idx"])

    edge_geoms = edges["geometry"].to_numpy()
    if lon0 is None or lat0 is None:
        bounds_arr = shapely.bounds(edge_geoms)
        lon0 = float(np.min(bounds_arr[:, 0]) + np.max(bounds_arr[:, 2])) * 0.5
        lat0 = float(np.min(bounds_arr[:, 1]) + np.max(bounds_arr[:, 3])) * 0.5

    midpoints_storage = [g.interpolate(0.5, normalized=True) for g in edge_geoms]
    midpoints_metric = project_to_metric(np.array(midpoints_storage, dtype=object), lon0, lat0)
    mid_xy = shapely.get_coordinates(midpoints_metric)

    cell_pts_storage = shapely.points(cells_df[["lon", "lat"]].to_numpy(dtype=np.float64))
    cell_pts_metric = project_to_metric(cell_pts_storage, lon0, lat0)
    cell_xy = shapely.get_coordinates(cell_pts_metric)

    tree = cKDTree(cell_xy)
    _, nearest_idx = tree.query(mid_xy, k=1)
    cell_rain_vals = cells_df["rain_idx"].to_numpy(dtype=np.float64)
    assigned_rain = np.clip(cell_rain_vals[nearest_idx], 0.0, 1.0)

    return pd.DataFrame(
        {
            "edge_id": edges["edge_id"].to_numpy(dtype=np.int64),
            "rain_idx": assigned_rain,
        }
    )


def validate_rain_index(rain_df: pd.DataFrame) -> dict[str, Any]:
    """Validate 0 <= rain_idx <= 1 and finite values for all edges."""
    if len(rain_df) == 0:
        raise ValueError("Rain index DataFrame is empty.")
    if "rain_idx" not in rain_df.columns:
        raise ValueError("Missing 'rain_idx' column in rain_df.")

    vals = rain_df["rain_idx"].to_numpy(dtype=np.float64)
    if not np.all(np.isfinite(vals)):
        raise ValueError("Non-finite values found in rain_idx.")
    if np.any(vals < 0.0) or np.any(vals > 1.0):
        raise ValueError("rain_idx values outside [0, 1] found.")

    return {
        "n_edges": len(vals),
        "min_rain_idx": float(np.min(vals)),
        "max_rain_idx": float(np.max(vals)),
        "mean_rain_idx": float(np.mean(vals)),
        "median_rain_idx": float(np.median(vals)),
        "positive_rain_edges": int(np.sum(vals > 0.0)),
    }


def edges_bbox(edges_df: pd.DataFrame) -> list[float]:
    """Return [min_lon, min_lat, max_lon, max_lat] of edge geometries (rainfall grid extent)."""
    bounds_arr = shapely.bounds(edges_df["geometry"].to_numpy())
    return [
        float(np.min(bounds_arr[:, 0])),
        float(np.min(bounds_arr[:, 1])),
        float(np.max(bounds_arr[:, 2])),
        float(np.max(bounds_arr[:, 3])),
    ]


def get_or_compute_rain_index(
    config: AppConfig,
    edges_df: pd.DataFrame,
    as_of: str | None = None,
    lon0: float | None = None,
    lat0: float | None = None,
) -> pd.DataFrame:
    """Load cached data/processed/rain_<as_of>.parquet or compute and cache it."""
    resolved_as_of = as_of or config.rainfall.as_of_default
    if not resolved_as_of:
        raise ValueError("as_of date is required when rainfall.as_of_default is not set.")
    _parse_iso_date(resolved_as_of)

    processed_dir = Path(config.paths.processed_dir)
    cache_parquet = processed_dir / f"rain_{resolved_as_of}.parquet"
    if cache_parquet.exists():
        cached_df = pd.read_parquet(cache_parquet)
        if len(cached_df) == len(edges_df):
            validate_rain_index(cached_df)
            return cached_df

    bbox = edges_bbox(edges_df)
    date_range = compute_climatology_date_range(
        as_of=resolved_as_of,
        window_days=config.rainfall.window_days,
        climatology_years=config.rainfall.climatology_years,
    )
    grid = fetch_grid(
        bbox=bbox,
        step=config.rainfall.grid_step_deg,
        date_range=date_range,
        cache_dir=config.datasets.rainfall.raw_cache_dir,
        api_base_url=config.datasets.rainfall.api_base_url,
        parameter=config.datasets.rainfall.parameter,
    )
    rain_df = rain_index(
        grid=grid,
        as_of=resolved_as_of,
        window_days=config.rainfall.window_days,
        climatology_years=config.rainfall.climatology_years,
        edges=edges_df,
        scale_percentile=config.rainfall.scale_percentile,
        eps=config.rainfall.eps,
        lon0=lon0,
        lat0=lat0,
    )
    validate_rain_index(rain_df)
    processed_dir.mkdir(parents=True, exist_ok=True)
    rain_df.to_parquet(cache_parquet, index=False)
    return rain_df


SUPPORTED_AS_OF_RULES: dict[str, str] = {
    "regional_mean_rain_idx_extremes": "rain_idx",
    "regional_mean_recent_mm_extremes": "recent_mm",
}


def candidate_as_of_dates(start_date: str, end_date: str, step_days: int) -> list[str]:
    """Return ISO dates from start_date to end_date inclusive, stepped by step_days."""
    if step_days <= 0:
        raise ValueError("step_days must be positive.")
    start_dt = _parse_iso_date(start_date)
    end_dt = _parse_iso_date(end_date)
    if start_dt > end_dt:
        raise ValueError(f"start_date {start_date} is after end_date {end_date}.")
    out: list[str] = []
    cur = start_dt
    while cur <= end_dt:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=step_days)
    return out


def select_wet_dry_dates(
    grid: list[dict[str, Any]],
    candidates: list[str],
    window_days: int,
    climatology_years: int,
    scale_percentile: float,
    eps: float,
    metric: str = "rain_idx",
) -> dict[str, Any]:
    """Pick the candidates with the highest and lowest regional mean of metric.

    metric is a per-cell column of rain_index output ("rain_idx" or "recent_mm").
    Regional mean is the unweighted mean over grid cells. Ties go to the earliest
    candidate date. Both regional means are reported for each picked date.
    """
    if not candidates:
        raise ValueError("At least one candidate date is required.")
    scored: list[dict[str, Any]] = []
    for cand in sorted(candidates):
        cells = rain_index(
            grid=grid,
            as_of=cand,
            window_days=window_days,
            climatology_years=climatology_years,
            scale_percentile=scale_percentile,
            eps=eps,
        )
        if metric not in cells.columns:
            raise ValueError(f"Unknown rainfall metric '{metric}'.")
        scored.append(
            {
                "as_of": cand,
                "score": float(cells[metric].mean()),
                "mean_rain_idx": float(cells["rain_idx"].mean()),
                "mean_recent_mm": float(cells["recent_mm"].mean()),
            }
        )

    wet = scored[0]
    dry = scored[0]
    for row in scored[1:]:
        if row["score"] > wet["score"]:
            wet = row
        if row["score"] < dry["score"]:
            dry = row
    keys = ("as_of", "mean_rain_idx", "mean_recent_mm")
    return {
        "metric": metric,
        "wet": {k: wet[k] for k in keys},
        "dry": {k: dry[k] for k in keys},
        "n_candidates": len(scored),
    }


def select_as_of_options(
    config: AppConfig,
    bbox: list[float],
) -> dict[str, Any]:
    """Apply config.ui.as_of_selection to the cached rainfall grid and return wet and dry dates."""
    rule = config.ui.as_of_selection
    if rule.rule_id not in SUPPORTED_AS_OF_RULES:
        raise ValueError(
            f"Unsupported as_of selection rule '{rule.rule_id}'; "
            f"supported: {list(SUPPORTED_AS_OF_RULES)}."
        )
    metric = SUPPORTED_AS_OF_RULES[rule.rule_id]
    candidates = candidate_as_of_dates(rule.start_date, rule.end_date, rule.step_days)
    earliest_start, _ = compute_climatology_date_range(
        as_of=candidates[0],
        window_days=config.rainfall.window_days,
        climatology_years=config.rainfall.climatology_years,
    )
    grid = fetch_grid(
        bbox=bbox,
        step=config.rainfall.grid_step_deg,
        date_range=(earliest_start, candidates[-1]),
        cache_dir=config.datasets.rainfall.raw_cache_dir,
        api_base_url=config.datasets.rainfall.api_base_url,
        parameter=config.datasets.rainfall.parameter,
    )
    picked = select_wet_dry_dates(
        grid=grid,
        candidates=candidates,
        window_days=config.rainfall.window_days,
        climatology_years=config.rainfall.climatology_years,
        scale_percentile=config.rainfall.scale_percentile,
        eps=config.rainfall.eps,
        metric=metric,
    )
    return {
        "rule_id": rule.rule_id,
        "rule": rule.description,
        "start_date": rule.start_date,
        "end_date": rule.end_date,
        "step_days": rule.step_days,
        **picked,
    }


def attach_rain_index_to_graph(graph: nx.Graph, rain_df: pd.DataFrame) -> nx.Graph:
    """Attach rain_idx values from rain_df to edges in graph."""
    rain_map = dict(
        zip(
            rain_df["edge_id"].to_numpy(dtype=np.int64),
            rain_df["rain_idx"].to_numpy(dtype=np.float64),
        )
    )
    for u, v, data in graph.edges(data=True):
        eid = int(data.get("edge_id", -1))
        data["rain_idx"] = float(rain_map.get(eid, 0.0))
    return graph


def build_rainfall_from_config(
    config_path: str | Path = "config/default.yaml",
    as_of: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch POWER grid, compute and cache rain_<as_of>.parquet, and return validation stats."""
    cfg = load_config(config_path, overrides=overrides)
    edges_df = load_edges_parquet(cfg.paths.edges_path)
    graph = load_graph_pickle(cfg.paths.graph_path)
    crs_info = graph.graph.get("crs", {})

    resolved_as_of = as_of or cfg.rainfall.as_of_default
    if not resolved_as_of:
        raise ValueError("as_of must be provided in CLI or config.rainfall.as_of_default.")

    rain_df = get_or_compute_rain_index(
        config=cfg,
        edges_df=edges_df,
        as_of=resolved_as_of,
        lon0=crs_info.get("lon0"),
        lat0=crs_info.get("lat0"),
    )
    stats = validate_rain_index(rain_df)
    raw_power_dir = Path(cfg.datasets.rainfall.raw_cache_dir)
    if raw_power_dir.exists():
        n_cached_pts = len(list(raw_power_dir.glob("*.json")))
        if n_cached_pts > 0:
            entry = create_manifest_entry(
                file_path=raw_power_dir,
                row_count=n_cached_pts,
                source_url=cfg.datasets.rainfall.docs_url,
                license="NASA Open Data Policy",
                dataset_key="rainfall",
            )
            upsert_manifest_entry(entry, cfg.paths.manifest_path)

    stats["as_of"] = resolved_as_of
    stats["parquet_path"] = str(Path(cfg.paths.processed_dir) / f"rain_{resolved_as_of}.parquet")
    logger.info(
        "Rainfall index R(e) for as_of=%s: %d edges, min=%.4f, median=%.4f, mean=%.4f, max=%.4f.",
        resolved_as_of,
        stats["n_edges"],
        stats["min_rain_idx"],
        stats["median_rain_idx"],
        stats["mean_rain_idx"],
        stats["max_rain_idx"],
    )
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    summary = build_rainfall_from_config()
    print(summary)
