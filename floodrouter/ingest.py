"""Dataset ingestion, CRS projection, and geometry cleaning module."""

from __future__ import annotations

import io
import math
import struct
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import shapefile
import shapely
from shapely import Geometry
from shapely.geometry import shape

from floodrouter.config import AppConfig
from floodrouter.manifest import (
    ManifestEntry,
    compute_sha256,
    create_manifest_entry,
    read_manifest,
    upsert_manifest_entry,
    write_manifest,
)

EARTH_RADIUS_M = 6371000
DEG_IN_HALF_CIRCLE = 180
LON_OFFSET_DEG = 180
UTM_ZONE_WIDTH_DEG = 6


def estimate_utm_zone(lon: float, lat: float) -> dict[str, Any]:
    """Estimate UTM zone and hemisphere from centroid longitude and latitude."""
    zone = int(math.floor((lon + float(LON_OFFSET_DEG)) / float(UTM_ZONE_WIDTH_DEG))) + 1
    hemisphere = "N" if lat >= 0.0 else "S"
    return {
        "zone": zone,
        "hemisphere": hemisphere,
        "lon0": lon,
        "lat0": lat,
        "crs_name": f"UTM_{zone}{hemisphere}",
    }


def project_to_metric(geoms: Any, lon0: float, lat0: float) -> Any:
    """Project geometries from EPSG:4326 degrees to local metric coordinates in meters."""
    rad_per_deg = math.pi / float(DEG_IN_HALF_CIRCLE)
    scale_y = float(EARTH_RADIUS_M) * rad_per_deg
    scale_x = scale_y * math.cos(lat0 * rad_per_deg)

    def _forward(coords: np.ndarray) -> np.ndarray:
        out = np.empty_like(coords)
        out[:, 0] = (coords[:, 0] - lon0) * scale_x
        out[:, 1] = (coords[:, 1] - lat0) * scale_y
        return out

    return shapely.transform(geoms, _forward)


def project_to_storage(geoms: Any, lon0: float, lat0: float) -> Any:
    """Project geometries from local metric coordinates in meters back to EPSG:4326 degrees."""
    rad_per_deg = math.pi / float(DEG_IN_HALF_CIRCLE)
    scale_y = float(EARTH_RADIUS_M) * rad_per_deg
    scale_x = scale_y * math.cos(lat0 * rad_per_deg)

    def _inverse(coords: np.ndarray) -> np.ndarray:
        out = np.empty_like(coords)
        out[:, 0] = (coords[:, 0] / scale_x) + lon0
        out[:, 1] = (coords[:, 1] / scale_y) + lat0
        return out

    return shapely.transform(geoms, _inverse)


def _open_shapefile_bytes(spec_path: str | Path) -> tuple[bytes, bytes, bytes]:
    """Load (.shp, .shx, .dbf) raw bytes from a file path or nested zip!member path."""
    raw_spec = str(spec_path)
    parts = raw_spec.split("!")
    root_path = Path(parts[0])
    if not root_path.exists():
        raise FileNotFoundError(f"Dataset path not found: {root_path}")

    if len(parts) == 1 and root_path.suffix.lower() == ".shp":
        shp_b = root_path.read_bytes()
        shx_b = root_path.with_suffix(".shx").read_bytes()
        dbf_b = root_path.with_suffix(".dbf").read_bytes()
        return shp_b, shx_b, dbf_b

    current_bytes = root_path.read_bytes()
    member_parts = parts[1:]
    if not member_parts:
        with zipfile.ZipFile(io.BytesIO(current_bytes), "r") as zf:
            shp_candidates = sorted(n for n in zf.namelist() if n.lower().endswith(".shp"))
            if not shp_candidates:
                raise ValueError(f"No .shp found in archive: {root_path}")
            member_parts = [shp_candidates[0]]

    for idx, part in enumerate(member_parts):
        with zipfile.ZipFile(io.BytesIO(current_bytes), "r") as zf:
            if idx < len(member_parts) - 1:
                current_bytes = zf.read(part)
            else:
                namelist = zf.namelist()
                shp_name = next((n for n in namelist if n.lower() == part.lower()), None)
                if shp_name is None:
                    raise FileNotFoundError(f"Layer {part} not found in {raw_spec}")
                base = shp_name[:-4].lower()
                shx_name = next(n for n in namelist if n.lower() == f"{base}.shx")
                dbf_name = next(n for n in namelist if n.lower() == f"{base}.dbf")
                return zf.read(shp_name), zf.read(shx_name), zf.read(dbf_name)

    raise ValueError(f"Unable to resolve shapefile specification: {raw_spec}")


def load_region_boundary(
    boundary_path: str | Path,
    region_name: str,
    name_column: str = "adm1_name",
) -> Geometry:
    """Load and dissolve the administrative boundary polygon for region_name."""
    if not region_name or not region_name.strip():
        raise ValueError("region_name must be a non-empty string.")
    target_name = region_name.strip().lower()

    shp_b, shx_b, dbf_b = _open_shapefile_bytes(boundary_path)
    matched_geoms: list[Geometry] = []
    available_names: list[str] = []

    with shapefile.Reader(
        shp=io.BytesIO(shp_b),
        shx=io.BytesIO(shx_b),
        dbf=io.BytesIO(dbf_b),
    ) as sf:
        field_names = [f[0] for f in sf.fields[1:]]
        if name_column not in field_names:
            raise ValueError(
                f"Column '{name_column}' not found in boundary dataset. "
                f"Available columns: {field_names}"
            )
        for sr in sf.iterShapeRecords():
            rec = dict(zip(field_names, sr.record))
            val = str(rec.get(name_column, "")).strip()
            available_names.append(val)
            if val.lower() == target_name:
                geom = shape(sr.shape.__geo_interface__)
                if not geom.is_valid:
                    geom = shapely.make_valid(geom)
                if not geom.is_empty:
                    matched_geoms.append(geom)

    if not matched_geoms:
        raise ValueError(
            f"Region '{region_name}' not found in column '{name_column}'. "
            f"Available regions: {sorted(set(available_names))}"
        )

    unioned = shapely.unary_union(matched_geoms)
    if not unioned.is_valid:
        unioned = shapely.make_valid(unioned)
    return unioned


def _fast_read_roads_within_bbox(
    shp_b: bytes,
    shx_b: bytes,
    dbf_b: bytes,
    bbox: tuple[float, float, float, float],
) -> tuple[list[dict[str, Any]], list[Geometry]]:
    """Read road records and geometries whose bounding boxes intersect bbox."""
    minx, miny, maxx, maxy = bbox
    n_records = (len(shx_b) - 100) // 8
    shx_idx = np.frombuffer(shx_b, dtype=">i4", offset=100).reshape(n_records, 2)
    byte_offsets = shx_idx[:, 0] * 2

    candidate_indices: list[int] = []
    candidate_geoms: list[Geometry] = []

    for i in range(n_records):
        off = int(byte_offsets[i])
        stype = struct.unpack_from("<i", shp_b, off + 8)[0]
        if stype not in (3, 13, 23):
            continue
        r_xmin, r_ymin, r_xmax, r_ymax, nparts, npts = struct.unpack_from(
            "<4d2i", shp_b, off + 12
        )
        if npts < 2:
            continue
        if r_xmax < minx or r_xmin > maxx or r_ymax < miny or r_ymin > maxy:
            continue

        pts_off = off + 52 + 4 * nparts
        pts = np.frombuffer(shp_b, dtype="<f8", count=npts * 2, offset=pts_off).reshape(npts, 2)
        if nparts == 1:
            geom = shapely.linestrings(pts)
        else:
            parts = np.frombuffer(shp_b, dtype="<i4", count=nparts, offset=off + 52)
            lines = []
            for p_idx in range(nparts):
                start = int(parts[p_idx])
                end = int(parts[p_idx + 1]) if p_idx + 1 < nparts else npts
                if end - start >= 2:
                    lines.append(shapely.linestrings(pts[start:end]))
            if not lines:
                continue
            geom = lines[0] if len(lines) == 1 else shapely.multilinestrings(lines)

        if geom is not None and not geom.is_empty:
            candidate_indices.append(i)
            candidate_geoms.append(geom)

    records: list[dict[str, Any]] = []
    with shapefile.Reader(dbf=io.BytesIO(dbf_b)) as sf:
        field_names = [f[0] for f in sf.fields[1:]]
        for rec_idx in candidate_indices:
            rec = sf.record(rec_idx)
            records.append(dict(zip(field_names, rec)))

    return records, candidate_geoms


def load_roads(
    path: str | Path,
    region: Geometry | AppConfig,
    road_class_column: str | None = "road_class",
    unknown_class_value: str = "unknown",
    include_unknown_roads: bool = True,
) -> pd.DataFrame:
    """Read roads, clip to region boundary, drop empty/invalid geometries, and explode lines."""
    if isinstance(region, AppConfig):
        cfg = region
        region_poly = load_region_boundary(
            boundary_path=cfg.region.boundary_file,
            region_name=cfg.region.name,
            name_column=cfg.region.name_column or "adm1_name",
        )
        road_class_column = cfg.datasets.roads.road_class_column
        unknown_class_value = cfg.datasets.roads.unknown_class_value
        include_unknown_roads = cfg.network.include_unknown_roads
    else:
        region_poly = region

    if region_poly is None or region_poly.is_empty:
        raise ValueError("Region boundary polygon cannot be empty.")

    raw_spec = str(path)
    extracted_candidate = Path("data/raw/extracted_roads/u_fe_road.shp")
    if "u_fe_road.shp" in raw_spec and extracted_candidate.exists():
        shp_b, shx_b, dbf_b = _open_shapefile_bytes(extracted_candidate)
    else:
        shp_b, shx_b, dbf_b = _open_shapefile_bytes(path)

    records, geoms = _fast_read_roads_within_bbox(
        shp_b=shp_b,
        shx_b=shx_b,
        dbf_b=dbf_b,
        bbox=region_poly.bounds,
    )

    if not geoms:
        return pd.DataFrame(columns=["road_id", "road_class", "geometry"])

    geom_arr = np.array(geoms, dtype=object)
    shapely.prepare(region_poly)
    intersects_mask = shapely.intersects(region_poly, geom_arr)

    exploded_rows: list[dict[str, Any]] = []
    unknown_norm = unknown_class_value.strip().lower()

    for idx, is_hit in enumerate(intersects_mask):
        if not is_hit:
            continue
        rec = records[idx]
        raw_geom = geom_arr[idx]
        if raw_geom is None or raw_geom.is_empty:
            continue
        if not raw_geom.is_valid:
            raw_geom = shapely.make_valid(raw_geom)

        if road_class_column and road_class_column in rec:
            r_class = str(rec.get(road_class_column) or "").strip()
        else:
            r_class = str(rec.get("road_class") or rec.get("osm_class") or "").strip()

        if not include_unknown_roads:
            if not r_class or r_class.lower() == unknown_norm:
                continue

        clipped = (
            raw_geom
            if shapely.contains(region_poly, raw_geom)
            else shapely.intersection(raw_geom, region_poly)
        )
        if clipped is None or clipped.is_empty:
            continue

        for part in shapely.get_parts(clipped):
            if part is None or part.is_empty:
                continue
            if part.geom_type != "LineString":
                continue
            if len(part.coords) < 2 or part.length <= 0.0:
                continue
            exploded_rows.append(
                {
                    "road_id": str(rec.get("global_id") or f"road_{idx}"),
                    "road_class": r_class if r_class else unknown_class_value,
                    "osm_class": str(rec.get("osm_class") or ""),
                    "source": str(rec.get("source") or ""),
                    "geometry": part,
                }
            )

    df = pd.DataFrame(exploded_rows)
    df.attrs["crs"] = "EPSG:4326"
    df.attrs["region_bounds"] = region_poly.bounds
    return df


__all__ = [
    "ManifestEntry",
    "compute_sha256",
    "create_manifest_entry",
    "estimate_utm_zone",
    "load_region_boundary",
    "load_roads",
    "project_to_metric",
    "project_to_storage",
    "read_manifest",
    "upsert_manifest_entry",
    "write_manifest",
]
