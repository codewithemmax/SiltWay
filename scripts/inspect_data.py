"""Inspect raw geospatial datasets in data/raw/ and write docs/data_inspection.md."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd
import shapefile
import yaml
from shapely.geometry import shape

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from floodrouter.manifest import (  # noqa: E402
    ManifestEntry,
    compute_sha256,
    create_manifest_entry,
    write_manifest,
)

try:
    import geopandas as gpd
except ImportError:
    gpd = None  # type: ignore[assignment]

VECTOR_EXTENSIONS = {".shp", ".gpkg", ".geojson", ".parquet", ".zip"}

FIELD_TYPE_MAP = {
    "C": "str (Character)",
    "N": "float/int (Numeric)",
    "F": "float (Float)",
    "L": "bool (Logical)",
    "D": "date (Date)",
    "M": "str (Memo)",
}

SHAPE_TYPE_MAP = {
    0: "Null",
    1: "Point",
    3: "LineString",
    5: "Polygon",
    8: "MultiPoint",
    11: "PointZ",
    13: "LineStringZ",
    15: "PolygonZ",
    18: "MultiPointZ",
    21: "PointM",
    23: "LineStringM",
    25: "PolygonM",
    28: "MultiPointM",
    31: "MultiPatch",
}


def discover_datasets(raw_dir: Path) -> list[Path]:
    """Find geospatial dataset files under raw_dir."""
    if not raw_dir.exists():
        return []
    found: list[Path] = []
    for path in sorted(raw_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in VECTOR_EXTENSIONS:
            found.append(path)
    return found


def _parse_prj_crs(prj_text: str) -> str:
    """Extract a readable CRS summary from a WKT .prj string."""
    cleaned = prj_text.strip()
    if not cleaned:
        return "Unspecified"
    if "GCS_WGS_1984" in cleaned or "WGS_1984" in cleaned or "WGS 84" in cleaned:
        if "PROJCS" not in cleaned:
            return "EPSG:4326 (WGS 84 Geographic)"
    return cleaned[:120]


def _inspect_shapefile_reader(
    sf: shapefile.Reader,
    prj_text: str,
    rel_name: str,
    checksum: str,
) -> dict[str, Any]:
    """Extract metadata from an open pyshp Reader."""
    fields = sf.fields[1:]
    columns_dtypes: dict[str, str] = {}
    for field in fields:
        fname = str(field[0])
        ftype = str(field[1])
        columns_dtypes[fname] = FIELD_TYPE_MAP.get(ftype, ftype)
    columns_dtypes["geometry"] = "geometry"

    row_count = len(sf)
    geom_type = SHAPE_TYPE_MAP.get(sf.shapeType, f"ShapeType({sf.shapeType})")
    geom_counts = {geom_type: row_count} if row_count > 0 else {}
    bounds = [float(x) for x in sf.bbox] if row_count > 0 else []

    return {
        "file": rel_name,
        "checksum": checksum,
        "crs": _parse_prj_crs(prj_text),
        "row_count": row_count,
        "columns_dtypes": columns_dtypes,
        "geometry_types": geom_counts,
        "bounds": bounds,
    }


def _inspect_shp_file(file_path: Path, rel_name: str) -> dict[str, Any]:
    """Inspect an extracted .shp file using pyshp."""
    prj_path = file_path.with_suffix(".prj")
    prj_text = prj_path.read_text(encoding="utf-8", errors="replace") if prj_path.exists() else ""
    with shapefile.Reader(str(file_path)) as sf:
        return _inspect_shapefile_reader(
            sf=sf,
            prj_text=prj_text,
            rel_name=rel_name,
            checksum=compute_sha256(file_path),
        )


def _inspect_zip_archive(file_path: Path, rel_name: str) -> list[dict[str, Any]]:
    """Inspect all .shp layers or nested .csv tables inside a .zip archive."""
    checksum = compute_sha256(file_path)
    results: list[dict[str, Any]] = []
    with zipfile.ZipFile(file_path, "r") as zf:
        namelist = zf.namelist()
        shp_names = sorted(n for n in namelist if n.lower().endswith(".shp"))
        for shp_name in shp_names:
            base = shp_name[:-4]
            dbf_match = next((n for n in namelist if n.lower() == f"{base.lower()}.dbf"), None)
            shx_match = next((n for n in namelist if n.lower() == f"{base.lower()}.shx"), None)
            prj_match = next((n for n in namelist if n.lower() == f"{base.lower()}.prj"), None)
            prj_text = (
                zf.read(prj_match).decode("utf-8", errors="replace") if prj_match else ""
            )
            shp_bytes = io.BytesIO(zf.read(shp_name))
            dbf_bytes = io.BytesIO(zf.read(dbf_match)) if dbf_match else None
            shx_bytes = io.BytesIO(zf.read(shx_match)) if shx_match else None
            with shapefile.Reader(shp=shp_bytes, dbf=dbf_bytes, shx=shx_bytes) as sf:
                results.append(
                    _inspect_shapefile_reader(
                        sf=sf,
                        prj_text=prj_text,
                        rel_name=f"{rel_name}::{shp_name}",
                        checksum=checksum,
                    )
                )

        if not shp_names:
            nested_zips = sorted(n for n in namelist if n.lower().endswith(".zip"))
            for nz_name in nested_zips:
                with zipfile.ZipFile(io.BytesIO(zf.read(nz_name)), "r") as nzf:
                    for csv_name in sorted(n for n in nzf.namelist() if n.lower().endswith(".csv")):
                        with nzf.open(csv_name, "r") as raw_csv:
                            text_stream = io.TextIOWrapper(
                                raw_csv, encoding="utf-8", errors="replace"
                            )
                            reader = csv.reader(text_stream)
                            header = next(reader, [])
                            row_count = sum(1 for _ in reader)
                        results.append(
                            {
                                "file": f"{rel_name}::{nz_name}::{csv_name}",
                                "checksum": checksum,
                                "crs": "None (Tabular CSV without geometry)",
                                "row_count": row_count,
                                "columns_dtypes": {col: "str (CSV column)" for col in header},
                                "geometry_types": {"None": row_count},
                                "bounds": [],
                            }
                        )
    return results


def _inspect_geojson_fallback(file_path: Path, rel_name: str) -> dict[str, Any]:
    """Inspect a GeoJSON file using stdlib json and shapely."""
    raw = json.loads(file_path.read_text(encoding="utf-8"))
    features = raw.get("features", [])
    crs_info = raw.get("crs", {}).get("properties", {}).get("name", "EPSG:4326")
    rows = [f.get("properties", {}) or {} for f in features]
    geoms = [shape(f["geometry"]) for f in features if f.get("geometry")]
    df = pd.DataFrame(rows)
    columns_dtypes = {str(col): str(dtype) for col, dtype in df.dtypes.items()}
    columns_dtypes["geometry"] = "geometry"

    geom_counts: dict[str, int] = {}
    for geom in geoms:
        gtype = geom.geom_type
        geom_counts[gtype] = geom_counts.get(gtype, 0) + 1

    if geoms:
        b_list = [g.bounds for g in geoms]
        bounds = [
            float(min(b[0] for b in b_list)),
            float(min(b[1] for b in b_list)),
            float(max(b[2] for b in b_list)),
            float(max(b[3] for b in b_list)),
        ]
    else:
        bounds = []

    return {
        "file": rel_name,
        "checksum": compute_sha256(file_path),
        "crs": str(crs_info),
        "row_count": len(features),
        "columns_dtypes": columns_dtypes,
        "geometry_types": geom_counts,
        "bounds": bounds,
    }


def inspect_single_dataset(file_path: Path, raw_dir: Path) -> list[dict[str, Any]]:
    """Read a vector dataset and extract CRS, columns/dtypes, row count, geom types, bounds."""
    rel_name = file_path.relative_to(raw_dir).as_posix()
    suffix = file_path.suffix.lower()

    if suffix == ".geojson":
        return [_inspect_geojson_fallback(file_path, rel_name)]
    if suffix == ".shp":
        return [_inspect_shp_file(file_path, rel_name)]
    if suffix == ".zip":
        return _inspect_zip_archive(file_path, rel_name)

    if gpd is None:
        raise RuntimeError("geopandas is required to inspect this binary vector format.")

    gdf = gpd.read_parquet(file_path) if suffix == ".parquet" else gpd.read_file(str(file_path))
    geom_counts: dict[str, int] = {}
    if "geometry" in gdf and gdf.geometry is not None:
        vc = gdf.geometry.geom_type.value_counts(dropna=False)
        geom_counts = {str(k): int(v) for k, v in vc.items()}
        bounds = [float(x) for x in gdf.total_bounds] if len(gdf) > 0 else []
    else:
        bounds = []

    columns_dtypes = {str(col): str(dtype) for col, dtype in gdf.dtypes.items()}
    return [
        {
            "file": rel_name,
            "checksum": compute_sha256(file_path),
            "crs": str(gdf.crs) if gdf.crs is not None else "None",
            "row_count": int(len(gdf)),
            "columns_dtypes": columns_dtypes,
            "geometry_types": geom_counts,
            "bounds": bounds,
        }
    ]


def render_inspection_markdown(
    records: list[dict[str, Any]],
    expected_sources: dict[str, Any] | None = None,
) -> str:
    """Format dataset inspection results as Markdown."""
    lines: list[str] = [
        "# Raw Data Inspection Report",
        "",
    ]
    if not records:
        lines.append("No geospatial datasets were found in `data/raw/`.")
        lines.append("")
        if expected_sources:
            lines.append("## Expected Datasets (from `config/default.yaml`)")
            lines.append("")
            for key, info in expected_sources.items():
                if isinstance(info, dict) and "url" in info:
                    target_path = info.get("path", "data/raw/")
                    url = info.get("url", "")
                    lines.append(f"- **{key}**: place at `{target_path}` (source: {url})")
                    if info.get("fallback_url"):
                        lines.append(f"  - Fallback URL: {info['fallback_url']}")
            lines.append("")
        return "\n".join(lines)

    for rec in records:
        lines.append(f"## Dataset: `{rec['file']}`")
        lines.append("")
        lines.append(f"- **SHA-256 Checksum**: `{rec['checksum']}`")
        lines.append(f"- **CRS**: `{rec['crs']}`")
        lines.append(f"- **Row Count**: {rec['row_count']}")
        lines.append(f"- **Geometry Types**: `{rec['geometry_types']}`")
        lines.append(f"- **Bounds (minx, miny, maxx, maxy)**: `{rec['bounds']}`")
        lines.append("- **Columns and Dtypes**:")
        for col, dtype in rec["columns_dtypes"].items():
            lines.append(f"  - `{col}`: `{dtype}`")
        lines.append("")

    return "\n".join(lines)


def inspect_raw_directory(
    raw_dir: str | Path = "data/raw",
    output_md: str | Path = "docs/data_inspection.md",
    config_path: str | Path = "config/default.yaml",
    manifest_path: str | Path | None = None,
) -> str:
    """Inspect all datasets in raw_dir, write output_md, and optionally update manifest.json."""
    raw_path = Path(raw_dir)
    datasets = discover_datasets(raw_path)
    records: list[dict[str, Any]] = []
    manifest_entries: list[ManifestEntry] = []

    expected_sources: dict[str, Any] | None = None
    cfg_file = Path(config_path)
    if cfg_file.exists():
        with cfg_file.open("r", encoding="utf-8") as handle:
            raw_cfg = yaml.safe_load(handle)
            if isinstance(raw_cfg, dict):
                expected_sources = raw_cfg.get("datasets")

    for ds_path in datasets:
        try:
            sub_records = inspect_single_dataset(ds_path, raw_path)
            records.extend(sub_records)
            total_rows = sum(int(r.get("row_count", 0)) for r in sub_records)
            matched_key: str | None = None
            matched_url = str(ds_path.relative_to(raw_path).as_posix())
            matched_license = "CC BY-IGO"
            if expected_sources:
                for key, info in expected_sources.items():
                    if not isinstance(info, dict):
                        continue
                    cfg_target = str(info.get("path", "")).split("!")[0]
                    if cfg_target.endswith(ds_path.name) or (
                        key == "roads" and "road" in ds_path.name.lower()
                    ):
                        matched_key = key
                        raw_url = info.get("fallback_url") or info.get("url") or matched_url
                        matched_url = str(raw_url)
                        matched_license = str(info.get("license", matched_license))
                        break
            manifest_entries.append(
                create_manifest_entry(
                    file_path=ds_path,
                    row_count=total_rows,
                    source_url=matched_url,
                    license=matched_license,
                    dataset_key=matched_key,
                )
            )
        except Exception as exc:
            records.append(
                {
                    "file": ds_path.relative_to(raw_path).as_posix(),
                    "checksum": compute_sha256(ds_path),
                    "crs": f"ERROR: {exc}",
                    "row_count": 0,
                    "columns_dtypes": {},
                    "geometry_types": {},
                    "bounds": [],
                }
            )

    if manifest_path is not None and manifest_entries:
        write_manifest(manifest_entries, manifest_path)

    report = render_inspection_markdown(records, expected_sources=expected_sources)
    out_file = Path(output_md)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(report, encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for inspecting raw datasets."""
    parser = argparse.ArgumentParser(description="Inspect datasets in data/raw/.")
    parser.add_argument("--raw-dir", default="data/raw", help="Path to raw data directory.")
    parser.add_argument(
        "--output",
        default="docs/data_inspection.md",
        help="Output Markdown file path.",
    )
    parser.add_argument(
        "--config",
        default="config/default.yaml",
        help="Path to configuration YAML.",
    )
    parser.add_argument(
        "--manifest",
        default="data/processed/manifest.json",
        help="Path to output manifest JSON.",
    )
    args = parser.parse_args(argv)
    report = inspect_raw_directory(
        raw_dir=args.raw_dir,
        output_md=args.output,
        config_path=args.config,
        manifest_path=args.manifest,
    )
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
