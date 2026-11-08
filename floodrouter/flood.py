"""Historical flood exposure calculation, validation, and sanity-check plotting."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import matplotlib
import networkx as nx
import numpy as np
import pandas as pd
import shapefile
import shapely
from shapely import Geometry
from shapely.geometry import shape
from shapely.strtree import STRtree

from floodrouter.config import load_config
from floodrouter.ingest import (
    _open_shapefile_bytes,
    load_region_boundary,
    project_to_metric,
)
from floodrouter.network import (
    load_edges_parquet,
    load_graph_pickle,
    save_network_artifacts,
)

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

logger = logging.getLogger(__name__)

TILE_SIZE_M = 5000
METERS_PER_KM = 1000


def load_flood_polygons(
    flood_path: str | Path,
    region_boundary: Geometry | None = None,
) -> list[Geometry]:
    """Read flood extent polygons from flood_path and optionally clip to region_boundary."""
    import io

    shp_b, shx_b, dbf_b = _open_shapefile_bytes(flood_path)
    raw_geoms: list[Geometry] = []
    with shapefile.Reader(
        shp=io.BytesIO(shp_b),
        shx=io.BytesIO(shx_b),
        dbf=io.BytesIO(dbf_b),
    ) as sf:
        for s in sf.iterShapes():
            geom = shape(s.__geo_interface__)
            if geom is None or geom.is_empty:
                continue
            if not geom.is_valid:
                geom = shapely.make_valid(geom)
            raw_geoms.extend(shapely.get_parts(geom))

    if not raw_geoms:
        return []

    if region_boundary is None:
        return [g for g in raw_geoms if not g.is_empty]

    parts_arr = np.array(raw_geoms, dtype=object)
    tree = STRtree(parts_arr)
    hit_idx = tree.query(region_boundary, predicate="intersects")
    if len(hit_idx) == 0:
        return []

    clipped = shapely.intersection(parts_arr[hit_idx], region_boundary)
    valid_clipped: list[Geometry] = []
    for g in clipped:
        if g is None or g.is_empty:
            continue
        if not g.is_valid:
            g = shapely.make_valid(g)
        for part in shapely.get_parts(g):
            if part is None or part.is_empty:
                continue
            if part.geom_type in ("Polygon", "MultiPolygon"):
                valid_clipped.append(part)
    return valid_clipped


def _tile_metric_polygon(poly_metric: Geometry, tile_size_m: int = TILE_SIZE_M) -> np.ndarray:
    """Partition a dissolved metric polygon/multipolygon into disjoint rectangular tiles."""
    if poly_metric is None or poly_metric.is_empty:
        return np.array([], dtype=object)

    parts = shapely.get_parts(poly_metric)
    tiles: list[Geometry] = []
    step = float(tile_size_m)
    for part in parts:
        if part is None or part.is_empty:
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


def edge_flood_fraction(
    edges: pd.DataFrame,
    flood_polys: list[Geometry] | np.ndarray | Geometry,
    lon0: float | None = None,
    lat0: float | None = None,
) -> np.ndarray:
    """Compute per-edge flood exposure fraction F(e) in [0, 1] using a spatial index."""
    n_edges = len(edges)
    if n_edges == 0:
        return np.array([], dtype=np.float64)

    if isinstance(flood_polys, Geometry):
        poly_list = [flood_polys] if not flood_polys.is_empty else []
    else:
        poly_list = [g for g in flood_polys if g is not None and not g.is_empty]

    if not poly_list:
        return np.zeros(n_edges, dtype=np.float64)

    edge_geoms = edges["geometry"].to_numpy()
    if lon0 is None or lat0 is None:
        bounds_arr = shapely.bounds(edge_geoms)
        lon0 = float(np.min(bounds_arr[:, 0]) + np.max(bounds_arr[:, 2])) * 0.5
        lat0 = float(np.min(bounds_arr[:, 1]) + np.max(bounds_arr[:, 3])) * 0.5

    edges_metric = project_to_metric(edge_geoms, lon0, lat0)
    edge_lengths_m = shapely.length(edges_metric)

    polys_metric = project_to_metric(np.array(poly_list, dtype=object), lon0, lat0)
    dissolved_metric = shapely.unary_union(polys_metric)
    if not dissolved_metric.is_valid:
        dissolved_metric = shapely.make_valid(dissolved_metric)

    flood_tiles = _tile_metric_polygon(dissolved_metric)
    if len(flood_tiles) == 0:
        return np.zeros(n_edges, dtype=np.float64)

    tree = STRtree(flood_tiles)
    pairs = tree.query(edges_metric, predicate="intersects")
    if pairs.shape[1] == 0:
        return np.zeros(n_edges, dtype=np.float64)

    edge_idx = pairs[0]
    tile_idx = pairs[1]
    intersections = shapely.intersection(edges_metric[edge_idx], flood_tiles[tile_idx])
    inter_lengths_m = shapely.length(intersections)

    summed_inter_m = np.zeros(n_edges, dtype=np.float64)
    np.add.at(summed_inter_m, edge_idx, inter_lengths_m)

    safe_lengths_m = np.where(edge_lengths_m > 0.0, edge_lengths_m, 1.0)
    raw_frac = np.where(edge_lengths_m > 0.0, summed_inter_m / safe_lengths_m, 0.0)
    return np.clip(raw_frac, 0.0, 1.0)


def validate_flood_exposure(
    edges_df: pd.DataFrame,
    column: str = "flood_frac",
) -> dict[str, Any]:
    """Validate 0 <= flood_frac <= 1 and report flood-edge share and total flood-zone km."""
    if len(edges_df) == 0:
        raise ValueError("Cannot validate flood exposure on empty edges DataFrame.")
    if column not in edges_df.columns:
        raise ValueError(f"Column '{column}' missing from edges DataFrame.")

    fracs = edges_df[column].to_numpy(dtype=np.float64)
    if not np.all(np.isfinite(fracs)):
        raise ValueError(f"Non-finite values found in {column}.")
    if np.any(fracs < 0.0) or np.any(fracs > 1.0):
        raise ValueError(f"Values outside [0, 1] found in {column}.")

    lengths_km = edges_df["length_m"].to_numpy(dtype=np.float64) / float(METERS_PER_KM)
    total_road_km = float(np.sum(lengths_km))
    flood_km = float(np.sum(lengths_km * fracs))
    n_edges = len(fracs)
    n_flooded_edges = int(np.sum(fracs > 0.0))
    flood_edge_share = float(n_flooded_edges) / float(n_edges)

    return {
        "n_edges": n_edges,
        "n_flooded_edges": n_flooded_edges,
        "flood_edge_share": flood_edge_share,
        "flood_edge_pct": flood_edge_share * 100.0,
        "total_road_km": total_road_km,
        "total_flood_km": flood_km,
        "flood_km_pct": (flood_km / total_road_km) * 100.0 if total_road_km > 0.0 else 0.0,
        "min_flood_frac": float(np.min(fracs)),
        "max_flood_frac": float(np.max(fracs)),
    }


def plot_flood_sanity_map(
    edges_df: pd.DataFrame,
    flood_polys: list[Geometry],
    region_boundary: Geometry,
    output_path: str | Path,
    region_label: str = "",
) -> Path:
    """Render a sanity-check map of road edges and 2012 flood polygons."""
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 8), dpi=150)

    for part in shapely.get_parts(region_boundary):
        x, y = part.exterior.xy
        ax.plot(x, y, color="#333333", linewidth=1.0, label="_nolegend_")

    dissolved_flood = shapely.unary_union(flood_polys) if flood_polys else None
    if dissolved_flood is not None and not dissolved_flood.is_empty:
        for part in shapely.get_parts(dissolved_flood):
            if part.geom_type == "Polygon":
                fx, fy = part.exterior.xy
                ax.fill(fx, fy, color="#6baed6", alpha=0.5, label="_nolegend_")

    fracs = edges_df["flood_frac"].to_numpy(dtype=np.float64)
    geoms = edges_df["geometry"].to_numpy()

    for geom, f_val in zip(geoms, fracs):
        if f_val <= 0.0:
            coords = np.array(geom.coords)
            ax.plot(coords[:, 0], coords[:, 1], color="#bdbdbd", linewidth=0.5, alpha=0.5)

    for geom, f_val in zip(geoms, fracs):
        if f_val > 0.0:
            coords = np.array(geom.coords)
            ax.plot(coords[:, 0], coords[:, 1], color="#de2d26", linewidth=1.0, alpha=1.0)

    ax.plot([], [], color="#6baed6", linewidth=6, alpha=0.5, label="2012 Flood Extent")
    ax.plot([], [], color="#bdbdbd", linewidth=1.0, label="Road Edge (F(e) = 0)")
    ax.plot([], [], color="#de2d26", linewidth=2.0, label="Flood-Exposed Edge (F(e) > 0)")

    title_suffix = f" ({region_label})" if region_label else ""
    ax.set_title(f"Road Network and 2012 Historical Flood Exposure F(e){title_suffix}")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.legend(loc="lower right")
    ax.set_aspect("equal")
    fig.tight_layout()
    fig.savefig(out_file)
    plt.close(fig)
    return out_file


def attach_flood_exposure_to_graph(
    graph: nx.Graph,
    edges_df: pd.DataFrame,
    flood_fracs: np.ndarray,
    flood_polys: list[Geometry] | None = None,
) -> tuple[nx.Graph, pd.DataFrame]:
    """Update edges_df and graph edges in-place with flood_fracs (0 to 1)."""
    updated_df = edges_df.copy()
    updated_df["flood_frac"] = flood_fracs.astype(np.float64)

    for row in updated_df.itertuples(index=False):
        u = int(row.u)
        v = int(row.v)
        f_val = float(row.flood_frac)
        if graph.has_edge(u, v):
            graph.edges[u, v]["flood_frac"] = f_val

    if flood_polys is not None:
        dissolved = (
            shapely.unary_union(flood_polys)
            if flood_polys
            else shapely.GeometryCollection()
        )
        graph.graph["flood_2012_geom"] = dissolved
    return graph, updated_df


def build_flood_from_config(
    config_path: str | Path = "config/default.yaml",
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute 2012 flood exposure F(e) for the prebuilt graph, save artifacts, and plot map."""
    cfg = load_config(config_path, overrides=overrides)
    boundary = load_region_boundary(
        boundary_path=cfg.region.boundary_file,
        region_name=cfg.region.name,
        name_column=cfg.region.name_column or "adm1_name",
    )
    edges_df = load_edges_parquet(cfg.paths.edges_path)
    graph = load_graph_pickle(cfg.paths.graph_path)
    existing_geom = graph.graph.get("flood_2012_geom")
    if existing_geom is not None and not existing_geom.is_empty:
        flood_polys_2012 = list(shapely.get_parts(existing_geom))
    else:
        flood_polys_2012 = load_flood_polygons(cfg.datasets.flood_2012.path, boundary)
    crs_info = graph.graph.get("crs", {})
    lon0 = crs_info.get("lon0")
    lat0 = crs_info.get("lat0")

    fracs_2012 = edge_flood_fraction(edges_df, flood_polys_2012, lon0=lon0, lat0=lat0)
    graph, edges_df = attach_flood_exposure_to_graph(
        graph=graph,
        edges_df=edges_df,
        flood_fracs=fracs_2012,
        flood_polys=flood_polys_2012,
    )

    stats_2012 = validate_flood_exposure(edges_df, column="flood_frac")
    if stats_2012["flood_edge_share"] <= 0.0 or stats_2012["flood_edge_share"] >= 1.0:
        raise ValueError(
            f"Suspicious flood_edge_share={stats_2012['flood_edge_share']:.4f}; "
            "inspect CRS handling."
        )

    graph.graph.setdefault("stats", {}).update({"flood_2012": stats_2012})
    save_network_artifacts(
        graph=graph,
        edges_df=edges_df,
        edges_path=cfg.paths.edges_path,
        graph_path=cfg.paths.graph_path,
    )

    map_path = Path(cfg.paths.results_dir) / "flood_sanity_map.png"
    plot_flood_sanity_map(
        edges_df=edges_df,
        flood_polys=flood_polys_2012,
        region_boundary=boundary,
        output_path=map_path,
        region_label=cfg.region.name,
    )
    stats_2012["sanity_map_path"] = str(map_path)
    logger.info(
        "2012 Flood exposure F(e): %d/%d edges flooded (%.2f%%), "
        "total flood-zone road length = %.2f km / %.2f km (%.2f%%).",
        stats_2012["n_flooded_edges"],
        stats_2012["n_edges"],
        stats_2012["flood_edge_pct"],
        stats_2012["total_flood_km"],
        stats_2012["total_road_km"],
        stats_2012["flood_km_pct"],
    )
    return stats_2012


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    summary = build_flood_from_config()
    print(summary)
