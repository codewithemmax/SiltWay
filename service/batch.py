"""Batch routing helpers: per-row parsing, totals, and CSV export."""

from __future__ import annotations

import csv
import io
from typing import Any

from pydantic import ValidationError

from service.schemas import BatchRowResult, BatchTotals, LatLon

RECOMMENDATION_STATUSES = ("clear", "recommended", "trade_off", "no_better_route")
SUM_FIELDS = (
    "baseline_length_km",
    "baseline_flood_km",
    "resilient_length_km",
    "resilient_flood_km",
    "avoided_flood_km",
    "extra_distance_km",
)
CSV_COLUMNS = (
    "row",
    "status",
    "origin_lat",
    "origin_lon",
    "dest_lat",
    "dest_lon",
    *SUM_FIELDS,
    "extra_distance_pct",
    "recommendation",
    "error_code",
    "error_message",
)


class BatchRowError(Exception):
    """Raised when a single batch row cannot be parsed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_batch_row(raw: Any) -> tuple[LatLon, LatLon]:
    """Parse a nested or flat batch row into validated origin and destination points."""
    if not isinstance(raw, dict):
        raise BatchRowError("INVALID_INPUT", "Row must be an object.")
    try:
        if "origin" in raw or "destination" in raw:
            origin = LatLon.model_validate(raw.get("origin"))
            dest = LatLon.model_validate(raw.get("destination"))
        else:
            origin = LatLon.model_validate(
                {"lat": raw.get("origin_lat"), "lon": raw.get("origin_lon")}
            )
            dest = LatLon.model_validate({"lat": raw.get("dest_lat"), "lon": raw.get("dest_lon")})
    except ValidationError as exc:
        parts = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", []))
            parts.append(f"{loc}: {err.get('msg', 'invalid')}" if loc else str(err.get("msg")))
        raise BatchRowError("INVALID_INPUT", "; ".join(parts) or "Invalid coordinates.") from exc
    return origin, dest


def summarize_batch(rows: list[BatchRowResult]) -> BatchTotals:
    """Sum metric fields over succeeded rows and count statuses."""
    ok_rows = [r for r in rows if r.status == "ok"]
    sums = {f: float(sum(float(getattr(r, f) or 0.0) for r in ok_rows)) for f in SUM_FIELDS}
    base_len = sums["baseline_length_km"]
    extra_pct = (sums["extra_distance_km"] / base_len) * 100.0 if base_len > 0.0 else None
    counts = {s: 0 for s in RECOMMENDATION_STATUSES}
    for r in ok_rows:
        if r.recommendation is not None:
            counts[r.recommendation] = counts.get(r.recommendation, 0) + 1
    return BatchTotals(
        total_rows=len(rows),
        succeeded_rows=len(ok_rows),
        failed_rows=len(rows) - len(ok_rows),
        extra_distance_pct=extra_pct,
        status_counts=counts,
        **sums,
    )


def batch_to_csv(rows: list[BatchRowResult], totals: BatchTotals) -> str:
    """Render batch rows plus a TOTAL line as CSV text."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        o = r.origin or {}
        d = r.destination or {}
        writer.writerow(
            [
                r.row,
                r.status,
                o.get("lat", ""),
                o.get("lon", ""),
                d.get("lat", ""),
                d.get("lon", ""),
                *["" if getattr(r, f) is None else getattr(r, f) for f in SUM_FIELDS],
                "" if r.extra_distance_pct is None else r.extra_distance_pct,
                r.recommendation or "",
                r.error_code or "",
                r.error_message or "",
            ]
        )
    writer.writerow(
        [
            "TOTAL",
            f"{totals.succeeded_rows} ok / {totals.failed_rows} failed",
            "",
            "",
            "",
            "",
            *[getattr(totals, f) for f in SUM_FIELDS],
            "" if totals.extra_distance_pct is None else totals.extra_distance_pct,
            "",
            "",
            "",
        ]
    )
    return buf.getvalue()
