"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import type {
  FloodFeatureCollection,
  GeoJSONGeometry,
  LatLon,
  RouteComparePayload
} from "@/types/api";

interface RouteMapProps {
  bbox: [number, number, number, number];
  floodLayer: FloodFeatureCollection | null;
  origin: LatLon | null;
  destination: LatLon | null;
  routeData: RouteComparePayload | null;
  resilientMode: boolean;
  mapboxToken: string;
  onMapClick: (coord: LatLon) => void;
}

const VIEW_WIDTH = 1000;
const VIEW_HEIGHT = 750;
const PADDING_PX = 40;

function projectLonLat(
  lon: number,
  lat: number,
  bbox: [number, number, number, number]
): [number, number] {
  const minLon = bbox[0];
  const minLat = bbox[1];
  const maxLon = bbox[2];
  const maxLat = bbox[3];
  const lonSpan = Math.max(maxLon - minLon, 1 / 1000);
  const latSpan = Math.max(maxLat - minLat, 1 / 1000);
  const innerW = VIEW_WIDTH - PADDING_PX * 2;
  const innerH = VIEW_HEIGHT - PADDING_PX * 2;

  const x = PADDING_PX + ((lon - minLon) / lonSpan) * innerW;
  const y = VIEW_HEIGHT - PADDING_PX - ((lat - minLat) / latSpan) * innerH;
  return [x, y];
}

function unprojectXY(
  x: number,
  y: number,
  bbox: [number, number, number, number]
): LatLon {
  const minLon = bbox[0];
  const minLat = bbox[1];
  const maxLon = bbox[2];
  const maxLat = bbox[3];
  const innerW = VIEW_WIDTH - PADDING_PX * 2;
  const innerH = VIEW_HEIGHT - PADDING_PX * 2;

  const lonRatio = Math.min(Math.max((x - PADDING_PX) / innerW, 0.0), 1.0);
  const latRatio = Math.min(
    Math.max((VIEW_HEIGHT - PADDING_PX - y) / innerH, 0.0),
    1.0
  );
  return {
    lon: minLon + lonRatio * (maxLon - minLon),
    lat: minLat + latRatio * (maxLat - minLat)
  };
}

function ringToSvgPath(
  ring: number[][],
  bbox: [number, number, number, number]
): string {
  if (!ring || ring.length === 0) {
    return "";
  }
  const parts: string[] = [];
  for (let i = 0; i < ring.length; i += 1) {
    const pt = ring[i];
    const [x, y] = projectLonLat(pt[0], pt[1], bbox);
    parts.push(`${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`);
  }
  parts.push("Z");
  return parts.join(" ");
}

function geometryToSvgPath(
  geom: GeoJSONGeometry,
  bbox: [number, number, number, number]
): string {
  if (geom.type === "Polygon") {
    const rings = geom.coordinates as number[][][];
    return rings.map((r) => ringToSvgPath(r, bbox)).join(" ");
  }
  if (geom.type === "MultiPolygon") {
    const polys = geom.coordinates as number[][][][];
    return polys
      .map((rings) => rings.map((r) => ringToSvgPath(r, bbox)).join(" "))
      .join(" ");
  }
  return "";
}

function lineCoordsToSvgPoints(
  coords: [number, number][],
  bbox: [number, number, number, number]
): string {
  return coords
    .map((pt) => {
      const [x, y] = projectLonLat(pt[0], pt[1], bbox);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

export default function RouteMap({
  bbox,
  floodLayer,
  origin,
  destination,
  routeData,
  resilientMode,
  mapboxToken,
  onMapClick
}: RouteMapProps) {
  const mapContainerRef = useRef<HTMLDivElement | null>(null);
  const mapInstanceRef = useRef<unknown>(null);
  const onMapClickRef = useRef(onMapClick);
  onMapClickRef.current = onMapClick;

  const [mapboxReady, setMapboxReady] = useState<boolean>(false);
  const [mapboxError, setMapboxError] = useState<string | null>(null);

  useEffect(() => {
    if (!mapboxToken || !mapContainerRef.current) {
      setMapboxReady(false);
      return;
    }

    let cancelled = false;
    async function initMapbox() {
      try {
        const mapboxgl = (await import("mapbox-gl")).default;
        mapboxgl.accessToken = mapboxToken;
        if (cancelled || !mapContainerRef.current) {
          return;
        }
        const map = new mapboxgl.Map({
          container: mapContainerRef.current,
          style: "mapbox://styles/mapbox/light-v11",
          bounds: [
            [bbox[0], bbox[1]],
            [bbox[2], bbox[3]]
          ],
          fitBoundsOptions: { padding: 32 }
        });

        map.on("load", () => {
          if (cancelled) {
            return;
          }
          setMapboxReady(true);
        });

        map.on("error", () => {
          if (!cancelled) {
            setMapboxError("Mapbox tile load failed; using built-in vector canvas.");
            setMapboxReady(false);
          }
        });

        map.on("click", (e) => {
          onMapClickRef.current({
            lat: e.lngLat.lat,
            lon: e.lngLat.lng
          });
        });

        mapInstanceRef.current = map;
      } catch {
        if (!cancelled) {
          setMapboxError("Mapbox initialization unavailable; using built-in vector canvas.");
          setMapboxReady(false);
        }
      }
    }

    initMapbox();
    return () => {
      cancelled = true;
      const existing = mapInstanceRef.current as { remove?: () => void } | null;
      if (existing && typeof existing.remove === "function") {
        existing.remove();
      }
      mapInstanceRef.current = null;
    };
  }, [mapboxToken, bbox]);

  useEffect(() => {
    if (!mapboxReady || !mapInstanceRef.current) {
      return;
    }
    const map = mapInstanceRef.current as {
      getSource: (id: string) => unknown;
      addSource: (id: string, spec: unknown) => void;
      addLayer: (spec: unknown) => void;
      setPaintProperty: (layer: string, prop: string, val: unknown) => void;
      getLayer: (id: string) => unknown;
    };

    if (floodLayer && !map.getSource("flood-source")) {
      map.addSource("flood-source", {
        type: "geojson",
        data: floodLayer
      });
      map.addLayer({
        id: "flood-fill",
        type: "fill",
        source: "flood-source",
        paint: {
          "fill-color": "#0284c7",
          "fill-opacity": 1 / 2
        }
      });
    }

    const baseGeojson = routeData
      ? {
          type: "Feature",
          properties: {},
          geometry: routeData.baseline.geometry
        }
      : { type: "FeatureCollection", features: [] };

    const resGeojson = routeData
      ? {
          type: "Feature",
          properties: {},
          geometry: routeData.resilient.geometry
        }
      : { type: "FeatureCollection", features: [] };

    const baseSource = map.getSource("baseline-route") as {
      setData?: (d: unknown) => void;
    } | null;
    if (!baseSource) {
      map.addSource("baseline-route", { type: "geojson", data: baseGeojson });
      map.addLayer({
        id: "baseline-line",
        type: "line",
        source: "baseline-route",
        paint: {
          "line-color": "#dc2626",
          "line-width": resilientMode ? 3 : 6,
          "line-opacity": resilientMode ? 1 / 2 : 1.0
        }
      });
    } else if (baseSource.setData) {
      baseSource.setData(baseGeojson);
    }

    const resSource = map.getSource("resilient-route") as {
      setData?: (d: unknown) => void;
    } | null;
    if (!resSource) {
      map.addSource("resilient-route", { type: "geojson", data: resGeojson });
      map.addLayer({
        id: "resilient-line",
        type: "line",
        source: "resilient-route",
        paint: {
          "line-color": "#0d9488",
          "line-width": resilientMode ? 6 : 3,
          "line-opacity": resilientMode ? 1.0 : 1 / 2
        }
      });
    } else if (resSource.setData) {
      resSource.setData(resGeojson);
    }

    if (map.getLayer("baseline-line")) {
      map.setPaintProperty("baseline-line", "line-width", resilientMode ? 3 : 6);
      map.setPaintProperty(
        "baseline-line",
        "line-opacity",
        resilientMode ? 1 / 2 : 1.0
      );
    }
    if (map.getLayer("resilient-line")) {
      map.setPaintProperty("resilient-line", "line-width", resilientMode ? 6 : 3);
      map.setPaintProperty(
        "resilient-line",
        "line-opacity",
        resilientMode ? 1.0 : 1 / 2
      );
    }
  }, [mapboxReady, floodLayer, routeData, resilientMode]);

  const floodPaths = useMemo(() => {
    if (!floodLayer || !floodLayer.features) {
      return [];
    }
    return floodLayer.features
      .map((f, idx) => ({
        key: f.id ?? idx,
        d: geometryToSvgPath(f.geometry, bbox)
      }))
      .filter((item) => item.d.length > 0);
  }, [floodLayer, bbox]);

  const baselinePoints = useMemo(() => {
    if (!routeData?.baseline?.geometry?.coordinates) {
      return "";
    }
    return lineCoordsToSvgPoints(routeData.baseline.geometry.coordinates, bbox);
  }, [routeData, bbox]);

  const resilientPoints = useMemo(() => {
    if (!routeData?.resilient?.geometry?.coordinates) {
      return "";
    }
    return lineCoordsToSvgPoints(routeData.resilient.geometry.coordinates, bbox);
  }, [routeData, bbox]);

  const originXY = useMemo(() => {
    if (!origin) {
      return null;
    }
    return projectLonLat(origin.lon, origin.lat, bbox);
  }, [origin, bbox]);

  const destinationXY = useMemo(() => {
    if (!destination) {
      return null;
    }
    return projectLonLat(destination.lon, destination.lat, bbox);
  }, [destination, bbox]);

  const handleSvgClick = (event: React.MouseEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const scaleX = VIEW_WIDTH / Math.max(rect.width, 1.0);
    const scaleY = VIEW_HEIGHT / Math.max(rect.height, 1.0);
    const svgX = (event.clientX - rect.left) * scaleX;
    const svgY = (event.clientY - rect.top) * scaleY;
    const coord = unprojectXY(svgX, svgY, bbox);
    onMapClick(coord);
  };

  const baselineOpacity = resilientMode ? 1 / 2 : 1.0;
  const baselineWidth = resilientMode ? 3 : 6;
  const resilientOpacity = resilientMode ? 1.0 : 1 / 2;
  const resilientWidth = resilientMode ? 6 : 3;

  return (
    <div className="map-wrapper">
      {mapboxToken ? (
        <div
          ref={mapContainerRef}
          className="mapbox-canvas"
          style={{ display: mapboxReady ? "block" : "none" }}
        />
      ) : null}

      {!mapboxReady ? (
        <svg
          viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
          className="vector-map-svg"
          role="application"
          aria-label="Interactive regional flood and route comparison map. Click once to set origin, click a second time to set destination."
          onClick={handleSvgClick}
        >
          <rect
            x={0}
            y={0}
            width={VIEW_WIDTH}
            height={VIEW_HEIGHT}
            fill="#0f172a"
          />

          <rect
            x={PADDING_PX}
            y={PADDING_PX}
            width={VIEW_WIDTH - PADDING_PX * 2}
            height={VIEW_HEIGHT - PADDING_PX * 2}
            fill="#1e293b"
            stroke="#334155"
            strokeWidth={2}
            rx={8}
          />

          <g aria-label="Historical flood extent polygons">
            {floodPaths.map((fp) => (
              <path
                key={fp.key}
                d={fp.d}
                fill="#38bdf8"
                fillOpacity={1 / 2}
                stroke="#0284c7"
                strokeWidth={1}
              />
            ))}
          </g>

          {resilientMode ? (
            <>
              {baselinePoints ? (
                <polyline
                  points={baselinePoints}
                  fill="none"
                  stroke="#ef4444"
                  strokeWidth={baselineWidth}
                  strokeOpacity={baselineOpacity}
                  strokeDasharray="8 4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              ) : null}
              {resilientPoints ? (
                <polyline
                  points={resilientPoints}
                  fill="none"
                  stroke="#14b8a6"
                  strokeWidth={resilientWidth}
                  strokeOpacity={resilientOpacity}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              ) : null}
            </>
          ) : (
            <>
              {resilientPoints ? (
                <polyline
                  points={resilientPoints}
                  fill="none"
                  stroke="#14b8a6"
                  strokeWidth={resilientWidth}
                  strokeOpacity={resilientOpacity}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              ) : null}
              {baselinePoints ? (
                <polyline
                  points={baselinePoints}
                  fill="none"
                  stroke="#ef4444"
                  strokeWidth={baselineWidth}
                  strokeOpacity={baselineOpacity}
                  strokeDasharray="8 4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              ) : null}
            </>
          )}

          {originXY ? (
            <g transform={`translate(${originXY[0]}, ${originXY[1]})`}>
              <circle r={9} fill="#f8fafc" stroke="#0f172a" strokeWidth={3} />
              <text
                y={4}
                textAnchor="middle"
                fontSize={11}
                fontWeight="bold"
                fill="#0f172a"
              >
                O
              </text>
            </g>
          ) : null}

          {destinationXY ? (
            <g transform={`translate(${destinationXY[0]}, ${destinationXY[1]})`}>
              <circle r={9} fill="#facc15" stroke="#0f172a" strokeWidth={3} />
              <text
                y={4}
                textAnchor="middle"
                fontSize={11}
                fontWeight="bold"
                fill="#0f172a"
              >
                D
              </text>
            </g>
          ) : null}
        </svg>
      ) : null}

      <div className="map-legend" aria-label="Map Legend">
        <div className="legend-title">Map Legend</div>
        <div className="legend-item">
          <span className="legend-swatch swatch-baseline" />
          <span>
            Baseline Route (Shortest Distance, Red Dashed)
            {!resilientMode ? " [Emphasized]" : " [De-emphasized]"}
          </span>
        </div>
        <div className="legend-item">
          <span className="legend-swatch swatch-resilient" />
          <span>
            Flood-Resilient Route (Weighted Cost, Teal Solid)
            {resilientMode ? " [Emphasized]" : " [De-emphasized]"}
          </span>
        </div>
        <div className="legend-item">
          <span className="legend-swatch swatch-flood" />
          <span>2012 Historical Flood Extent (MODIS Water Detection)</span>
        </div>
        <div className="legend-item">
          <span className="legend-marker">O / D</span>
          <span>Origin (O) and Destination (D) Endpoints</span>
        </div>
        {mapboxError ? <div className="legend-note">{mapboxError}</div> : null}
      </div>
    </div>
  );
}
