"""Compute per-ADM1 total road km and 2012/2022 flood intersection shares."""

from __future__ import annotations

import argparse
import io
import math
import struct
import sys
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import shapefile
import shapely
from shapely import Geometry
from shapely.geometry import shape
from shapely.strtree import STRtree

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EARTH_RADIUS_M = 6371000
DEG_IN_HALF_CIRCLE = 180
METERS_PER_KM = 1000
TILE_SIZE_M = 5000


def _extract_roads_if_needed(zip_path: Path, extract_dir: Path) -> list[Path]:
    """Extract nested road shapefiles from national-roads-esri-shapefile.zip if needed."""
    existing = sorted(extract_dir.glob("*.shp"))
    if existing:
        return existing
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zr:
        for sub in zr.namelist():
            if sub.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(zr.read(sub)), "r") as zsub:
                    for inner in zsub.namelist():
                        if inner.endswith(".zip"):
                            with zipfile.ZipFile(io.BytesIO(zsub.read(inner)), "r") as zinner:
                                zinner.extractall(extract_dir)
    return sorted(extract_dir.glob("*.shp"))


def _read_shapefile_from_zip(
    zip_path: Path,
    shp_member: str,
) -> tuple[list[dict[str, Any]], list[Geometry]]:
    """Read records and shapely geometries from a shapefile inside a zip archive."""
    base = shp_member[:-4]
    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = zf.namelist()
        dbf_name = next(n for n in namelist if n.lower() == f"{base.lower()}.dbf")
        shx_name = next(n for n in namelist if n.lower() == f"{base.lower()}.shx")
        shp_bytes = io.BytesIO(zf.read(shp_member))
        dbf_bytes = io.BytesIO(zf.read(dbf_name))
        shx_bytes = io.BytesIO(zf.read(shx_name))
        with shapefile.Reader(shp=shp_bytes, dbf=dbf_bytes, shx=shx_bytes) as sf:
            field_names = [f[0] for f in sf.fields[1:]]
            records: list[dict[str, Any]] = []
            geoms: list[Geometry] = []
            for sr in sf.iterShapeRecords():
                rec_dict = dict(zip(field_names, sr.record))
                geom = shape(sr.shape.__geo_interface__)
                if not geom.is_valid:
                    geom = shapely.make_valid(geom)
                records.append(rec_dict)
                geoms.append(geom)
    return records, geoms


def _fast_read_polylines_and_bboxes(shp_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Read all PolyLine geometries and their bounding boxes from an uncompressed .shp file."""
    shx_path = shp_path.with_suffix(".shx")
    shp_buf = shp_path.read_bytes()
    shx_buf = shx_path.read_bytes()
    n_records = (len(shx_buf) - 100) // 8
    shx_idx = np.frombuffer(shx_buf, dtype=">i4", offset=100).reshape(n_records, 2)
    byte_offsets = shx_idx[:, 0] * 2

    bboxes = np.empty((n_records, 4), dtype=np.float64)
    geoms = np.empty(n_records, dtype=object)
    valid_mask = np.zeros(n_records, dtype=bool)

    for i in range(n_records):
        off = int(byte_offsets[i])
        stype = struct.unpack_from("<i", shp_buf, off + 8)[0]
        if stype not in (3, 13, 23):
            continue
        xmin, ymin, xmax, ymax, nparts, npts = struct.unpack_from("<4d2i", shp_buf, off + 12)
        if npts < 2:
            continue
        bboxes[i] = (xmin, ymin, xmax, ymax)
        pts_off = off + 52 + 4 * nparts
        pts = np.frombuffer(shp_buf, dtype="<f8", count=npts * 2, offset=pts_off).reshape(npts, 2)
        if nparts == 1:
            geoms[i] = shapely.linestrings(pts)
        else:
            parts = np.frombuffer(shp_buf, dtype="<i4", count=nparts, offset=off + 52)
            lines = []
            for p_idx in range(nparts):
                start = int(parts[p_idx])
                end = int(parts[p_idx + 1]) if p_idx + 1 < nparts else npts
                if end - start >= 2:
                    lines.append(shapely.linestrings(pts[start:end]))
            if not lines:
                continue
            geoms[i] = lines[0] if len(lines) == 1 else shapely.multilinestrings(lines)
        valid_mask[i] = True

    return bboxes[valid_mask], geoms[valid_mask]


def _project_to_local_meters(geoms: np.ndarray | Geometry, lon0: float, lat0: float) -> Any:
    """Transform geometries from EPSG:4326 degrees to local metric coordinates."""
    rad_per_deg = math.pi / float(DEG_IN_HALF_CIRCLE)
    scale_y = float(EARTH_RADIUS_M) * rad_per_deg
    scale_x = scale_y * math.cos(lat0 * rad_per_deg)

    def _transform_coords(coords: np.ndarray) -> np.ndarray:
        out = np.empty_like(coords)
        out[:, 0] = (coords[:, 0] - lon0) * scale_x
        out[:, 1] = (coords[:, 1] - lat0) * scale_y
        return out

    return shapely.transform(geoms, _transform_coords)


def _tile_polygon_metric(poly_metric: Geometry, tile_size_m: int = TILE_SIZE_M) -> np.ndarray:
    """Subdivide a metric polygon/multipolygon into grid tiles for fast spatial indexing."""
    if poly_metric is None or poly_metric.is_empty:
        return np.array([], dtype=object)

    parts = shapely.get_parts(poly_metric)
    tiles: list[Geometry] = []
    step = float(tile_size_m)
    for part in parts:
        if part.is_empty:
            continue
        minx, miny, maxx, maxy = part.bounds
        if (maxx - minx) <= step and (maxy - miny) <= step:
            tiles.append(part)
            continue
        x = minx
        while x < maxx:
            y = miny
            x_next = min(x + step, maxx)
            while y < maxy:
                y_next = min(y + step, maxy)
                clipped = shapely.clip_by_rect(part, x, y, x_next, y_next)
                if not clipped.is_empty:
                    if not clipped.is_valid:
                        clipped = shapely.make_valid(clipped)
                    tiles.append(clipped)
                y = y_next
            x = x_next
    return np.array(tiles, dtype=object)


def _compute_flood_intersection_km(roads_metric: np.ndarray, flood_tiles: np.ndarray) -> float:
    """Compute total length in km of roads_metric intersecting disjoint flood_tiles."""
    if len(roads_metric) == 0 or len(flood_tiles) == 0:
        return 0.0
    tree = STRtree(flood_tiles)
    pairs = tree.query(roads_metric, predicate="intersects")
    if pairs.shape[1] == 0:
        return 0.0
    road_idx = pairs[0]
    tile_idx = pairs[1]
    intersections = shapely.intersection(roads_metric[road_idx], flood_tiles[tile_idx])
    lengths_m = shapely.length(intersections)
    return float(np.sum(lengths_m)) / float(METERS_PER_KM)


def evaluate_all_adm1(
    raw_dir: Path = Path("data/raw"),
    output_md: Path = Path("docs/region_candidates.md"),
) -> list[dict[str, Any]]:
    """Compute road km and 2012/2022 flood intersection shares for every ADM1 in Nigeria."""
    adm_records, adm_geoms = _read_shapefile_from_zip(
        raw_dir / "nga_admin_boundaries.shp.zip",
        "nga_admin1.shp",
    )
    _, flood_2012_geoms = _read_shapefile_from_zip(
        raw_dir / "nga-floods-2012-shp.zip",
        "NGA_Flood_Modis_250_nasa.shp",
    )
    _, flood_2022_geoms = _read_shapefile_from_zip(
        raw_dir / "FL20221019NGA_SHP.zip",
        "FL20221019NGA_SHP/VIIRS_20221001_20221025_MaximumFloodWaterExtent_Nigeria.shp",
    )

    f2012_parts = np.concatenate([shapely.get_parts(g) for g in flood_2012_geoms])
    f2022_parts = np.concatenate([shapely.get_parts(g) for g in flood_2022_geoms])
    f2012_tree = STRtree(f2012_parts)
    f2022_tree = STRtree(f2022_parts)

    shp_files = _extract_roads_if_needed(
        raw_dir / "national-roads-esri-shapefile.zip",
        raw_dir / "extracted_roads",
    )
    u_fe_road_shp = next(p for p in shp_files if p.name == "u_fe_road.shp")
    road_bboxes, road_geoms = _fast_read_polylines_and_bboxes(u_fe_road_shp)

    results: list[dict[str, Any]] = []
    for rec, adm_poly in zip(adm_records, adm_geoms):
        adm_name = str(rec["adm1_name"])
        adm_pcode = str(rec["adm1_pcode"])
        minx, miny, maxx, maxy = adm_poly.bounds
        lon0 = (minx + maxx) * 0.5
        lat0 = (miny + maxy) * 0.5

        bbox_mask = (
            (road_bboxes[:, 2] >= minx)
            & (road_bboxes[:, 0] <= maxx)
            & (road_bboxes[:, 3] >= miny)
            & (road_bboxes[:, 1] <= maxy)
        )
        cand_roads = road_geoms[bbox_mask]
        if len(cand_roads) > 0:
            shapely.prepare(adm_poly)
            within_mask = shapely.contains(adm_poly, cand_roads)
            inside_roads = cand_roads[within_mask]
            border_cands = cand_roads[~within_mask]
            inter_mask = shapely.intersects(adm_poly, border_cands)
            border_roads = shapely.intersection(border_cands[inter_mask], adm_poly)
            state_roads = np.concatenate([inside_roads, border_roads])
        else:
            state_roads = np.array([], dtype=object)

        roads_metric = _project_to_local_meters(state_roads, lon0, lat0)
        total_road_km = float(np.sum(shapely.length(roads_metric))) / float(METERS_PER_KM)

        idx_2012 = f2012_tree.query(adm_poly, predicate="intersects")
        if len(idx_2012) > 0:
            clipped_2012 = shapely.intersection(f2012_parts[idx_2012], adm_poly)
            union_2012 = shapely.unary_union(clipped_2012)
            metric_2012 = _project_to_local_meters(union_2012, lon0, lat0)
            tiles_2012 = _tile_polygon_metric(metric_2012)
        else:
            tiles_2012 = np.array([], dtype=object)

        idx_2022 = f2022_tree.query(adm_poly, predicate="intersects")
        if len(idx_2022) > 0:
            clipped_2022 = shapely.intersection(f2022_parts[idx_2022], adm_poly)
            union_2022 = shapely.unary_union(clipped_2022)
            metric_2022 = _project_to_local_meters(union_2022, lon0, lat0)
            tiles_2022 = _tile_polygon_metric(metric_2022)
        else:
            tiles_2022 = np.array([], dtype=object)

        flood_2012_km = _compute_flood_intersection_km(roads_metric, tiles_2012)
        flood_2022_km = _compute_flood_intersection_km(roads_metric, tiles_2022)

        share_2012_pct = (
            (flood_2012_km / total_road_km) * 100.0 if total_road_km > 0.0 else 0.0
        )
        share_2022_pct = (
            (flood_2022_km / total_road_km) * 100.0 if total_road_km > 0.0 else 0.0
        )

        results.append(
            {
                "adm1_name": adm_name,
                "adm1_pcode": adm_pcode,
                "n_segments": int(len(roads_metric)),
                "total_road_km": total_road_km,
                "flood_2012_km": flood_2012_km,
                "share_2012_pct": share_2012_pct,
                "flood_2022_km": flood_2022_km,
                "share_2022_pct": share_2022_pct,
                "min_share_both_pct": min(share_2012_pct, share_2022_pct),
                "mean_share_both_pct": (share_2012_pct + share_2022_pct) * 0.5,
            }
        )

    results_sorted = sorted(
        results,
        key=lambda r: (r["min_share_both_pct"], r["mean_share_both_pct"]),
        reverse=True,
    )

    header_std = (
        "| Rank | ADM1 (`adm1_name`) | Code | Total Road km | "
        "2012 Flood km | 2012 Share (%) | 2022 Flood km | 2022 Share (%) |"
    )
    lines: list[str] = [
        "# ADM1 Pilot Region Candidates: Road Length and Flood Extent Overlap (2012 vs 2022)",
        "",
        "## Exact `adm1_name` Values in `nga_admin1.shp` (37 Total)",
        "",
        ", ".join(f"`{r['adm1_name']}`" for r in sorted(results, key=lambda x: x["adm1_name"])),
        "",
        "## Top 10 ADM1 by 2012 Flood Share (%)",
        "",
        header_std,
        "|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    top_2012 = sorted(results, key=lambda r: r["share_2012_pct"], reverse=True)[:10]
    for rank, r in enumerate(top_2012, start=1):
        lines.append(
            f"| {rank} | {r['adm1_name']} | {r['adm1_pcode']} | "
            f"{r['total_road_km']:.2f} | {r['flood_2012_km']:.2f} | {r['share_2012_pct']:.2f}% | "
            f"{r['flood_2022_km']:.2f} | {r['share_2022_pct']:.2f}% |"
        )

    lines.extend(
        [
            "",
            "## Top 10 ADM1 by 2022 Flood Share (%)",
            "",
            header_std,
            "|---:|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    top_2022 = sorted(results, key=lambda r: r["share_2022_pct"], reverse=True)[:10]
    for rank, r in enumerate(top_2022, start=1):
        lines.append(
            f"| {rank} | {r['adm1_name']} | {r['adm1_pcode']} | "
            f"{r['total_road_km']:.2f} | {r['flood_2012_km']:.2f} | {r['share_2012_pct']:.2f}% | "
            f"{r['flood_2022_km']:.2f} | {r['share_2022_pct']:.2f}% |"
        )

    header_both = (
        "| Rank | ADM1 (`adm1_name`) | Code | Total Road km | "
        "2012 Flood km | 2012 Share (%) | 2022 Flood km | 2022 Share (%) | Min Share (%) |"
    )
    lines.extend(
        [
            "",
            "## Top 10 ADM1 by Overlap in Both Years (Sorted by `min(2012_share, 2022_share)`)",
            "",
            header_both,
            "|---:|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for rank, r in enumerate(results_sorted[:10], start=1):
        lines.append(
            f"| {rank} | {r['adm1_name']} | {r['adm1_pcode']} | "
            f"{r['total_road_km']:.2f} | {r['flood_2012_km']:.2f} | {r['share_2012_pct']:.2f}% | "
            f"{r['flood_2022_km']:.2f} | {r['share_2022_pct']:.2f}% | "
            f"{r['min_share_both_pct']:.2f}% |"
        )

    header_full = (
        "| ADM1 (`adm1_name`) | Code | Segments | Total Road km | "
        "2012 Flood km | 2012 Share (%) | 2022 Flood km | 2022 Share (%) |"
    )
    lines.extend(
        [
            "",
            "## Full Table: All 37 ADM1 Regions (Alphabetical)",
            "",
            header_full,
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for r in sorted(results, key=lambda x: x["adm1_name"]):
        lines.append(
            f"| {r['adm1_name']} | {r['adm1_pcode']} | {r['n_segments']} | "
            f"{r['total_road_km']:.2f} | {r['flood_2012_km']:.2f} | {r['share_2012_pct']:.2f}% | "
            f"{r['flood_2022_km']:.2f} | {r['share_2022_pct']:.2f}% |"
        )
    lines.append("")

    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text("\n".join(lines), encoding="utf-8")
    return results_sorted


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for ADM1 candidate evaluation."""
    parser = argparse.ArgumentParser(description="Evaluate ADM1 pilot region candidates.")
    parser.add_argument("--raw-dir", default="data/raw", help="Raw data directory.")
    parser.add_argument(
        "--output",
        default="docs/region_candidates.md",
        help="Output Markdown path.",
    )
    args = parser.parse_args(argv)
    evaluate_all_adm1(raw_dir=Path(args.raw_dir), output_md=Path(args.output))
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
