"""Unit tests for the hard-coding static checker script."""

from __future__ import annotations

from pathlib import Path

from scripts.check_no_hardcoding import (
    load_forbidden_literals,
    scan_repository,
    scan_text_for_violations,
)


def test_forbidden_literals_file_is_non_empty() -> None:
    """config/forbidden_literals.txt exists and contains entries."""
    literals = load_forbidden_literals("config/forbidden_literals.txt")
    assert len(literals) >= 30


def test_clean_source_passes() -> None:
    """Source code without forbidden strings, coordinate floats, or dates passes."""
    literals = load_forbidden_literals("config/forbidden_literals.txt")
    sample_code = (
        "def compute_weight(length_km: float, alpha: float, flood_frac: float) -> float:\n"
        "    return length_km * (1.0 + alpha * flood_frac)\n"
    )
    violations = scan_text_for_violations(sample_code, "floodrouter/sample.py", literals)
    assert violations == []


def test_detects_forbidden_place_or_crs_literal() -> None:
    """Source code containing a forbidden literal from forbidden_literals.txt fails."""
    literals = load_forbidden_literals("config/forbidden_literals.txt")
    target_literal = literals[0]
    bad_code = f' REGION_OVERRIDE = "{target_literal}"\n'
    violations = scan_text_for_violations(bad_code, "floodrouter/bad.py", literals)
    assert any(v.rule == "FORBIDDEN_LITERAL" for v in violations)


def test_detects_coordinate_like_float_literal() -> None:
    """Source code containing lat/lon-like float literals outside config/tests fails."""
    literals = load_forbidden_literals("config/forbidden_literals.txt")
    bad_code = " origin_lat = 6.4541\n origin_lon = 3.3947\n"
    violations = scan_text_for_violations(bad_code, "floodrouter/bad_coords.py", literals)
    float_violations = [v for v in violations if v.rule == "FLOAT_LITERAL"]
    assert len(float_violations) == 2


def test_detects_hardcoded_iso_date() -> None:
    """Source code containing a hardcoded YYYY-MM-DD date string fails."""
    literals = load_forbidden_literals("config/forbidden_literals.txt")
    bad_code = ' DEFAULT_DATE = "2022-10-15"\n'
    violations = scan_text_for_violations(bad_code, "floodrouter/bad_date.py", literals)
    assert any(v.rule == "HARDCODED_DATE" for v in violations)


def test_detects_em_and_en_dashes() -> None:
    """Files containing em dashes or en dashes are flagged."""
    literals = load_forbidden_literals("config/forbidden_literals.txt")
    em_dash = "\u2014"
    bad_code = f"# Comment with forbidden dash {em_dash} here\n"
    violations = scan_text_for_violations(bad_code, "floodrouter/bad_dash.py", literals)
    assert any(v.rule == "FORBIDDEN_DASH" for v in violations)


def test_current_repository_has_zero_violations() -> None:
    """The entire repository passes the hard-coding check cleanly."""
    repo_root = Path(__file__).resolve().parent.parent
    violations = scan_repository(repo_root)
    assert violations == []


def test_frontend_integer_coordinates_are_flagged() -> None:
    """Integer coordinate literals in TSX (missed by the float check) are flagged."""
    literals = load_forbidden_literals("config/forbidden_literals.txt")
    bad_tsx = (
        "const origin = { lat: 7, lon: 6 };\n"
        "map.setCenter(center);\n"
        "const view = { center: [6, 7], zoom: 8 };\n"
    )
    violations = scan_text_for_violations(bad_tsx, "web/src/bad.tsx", literals)
    coord = [v for v in violations if v.rule == "COORDINATE_LITERAL"]
    assert sorted(v.line for v in coord) == [1, 3]


def test_frontend_values_from_api_are_not_flagged() -> None:
    """Coordinates read from API responses or variables pass."""
    literals = load_forbidden_literals("config/forbidden_literals.txt")
    ok_tsx = (
        "const origin = { lat: point.lat, lon: point.lon };\n"
        "map.fitBounds(meta.bbox, { padding: 24 });\n"
        "const zoomStep = 1;\n"
    )
    assert scan_text_for_violations(ok_tsx, "web/src/ok.tsx", literals) == []


def test_scan_repository_covers_frontend_sources(tmp_path: Path) -> None:
    """scan_repository flags region names and coordinate literals inside web/ sources."""
    literals = load_forbidden_literals("config/forbidden_literals.txt")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "forbidden_literals.txt").write_text(
        "\n".join(literals), encoding="utf-8"
    )
    web_src = tmp_path / "web" / "src"
    web_src.mkdir(parents=True)
    region = literals[0]
    (web_src / "page.tsx").write_text(
        f'const title = "{region} dispatch";\nconst pin = {{ lat: 6.4541, lon: 3.3947 }};\n',
        encoding="utf-8",
    )
    (web_src / "util.ts").write_text("export const center = [7, 8];\n", encoding="utf-8")
    ignored = tmp_path / "web" / "node_modules" / "pkg"
    ignored.mkdir(parents=True)
    (ignored / "index.js").write_text(f'const x = "{region}";\n', encoding="utf-8")

    violations = scan_repository(tmp_path)
    rules_by_file: dict[str, set[str]] = {}
    for v in violations:
        rules_by_file.setdefault(v.file_path, set()).add(v.rule)
    assert {"FORBIDDEN_LITERAL", "FLOAT_LITERAL", "COORDINATE_LITERAL"} <= rules_by_file[
        "web/src/page.tsx"
    ]
    assert "COORDINATE_LITERAL" in rules_by_file["web/src/util.ts"]
    assert not any("node_modules" in f for f in rules_by_file)
