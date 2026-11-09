"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import RouteMap from "@/components/RouteMap";
import type {
  ApiErrorEnvelope,
  BatchPayload,
  DemoScenario,
  FloodFeatureCollection,
  LatLon,
  MetaPayload,
  RoadFeatureCollection,
  RouteComparePayload
} from "@/types/api";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const MAPBOX_TOKEN = process.env.NEXT_PUBLIC_MAPBOX_TOKEN || "";
const ERROR_MESSAGES: Record<string, string> = {
  INVALID_INPUT: "Check the selected endpoints and parameters.",
  UNKNOWN_PRESET: "That routing preset is not available.",
  BATCH_TOO_LARGE: "The submitted batch is larger than the configured limit.",
  OUT_OF_COVERAGE: "One endpoint is outside the mapped road coverage.",
  NO_PATH: "No connected road path was found between these endpoints.",
  RAINFALL_UNAVAILABLE: "The selected rainfall date is unavailable.",
  ROW_FAILED: "This row could not be routed.",
  NETWORK_ERROR: "The routing service could not be reached."
};

function apiError(payload: ApiErrorEnvelope, fallback: string): string {
  const code = payload.error?.code || "UNKNOWN_ERROR";
  return ERROR_MESSAGES[code] || fallback;
}

function coordText(point: LatLon | null): string {
  return point ? `${point.lat.toFixed(4)}, ${point.lon.toFixed(4)}` : "Not set";
}

function recommendationText(route: RouteComparePayload | null): string {
  if (!route) return "Choose a corridor to calculate a recommendation.";
  const rec = route.recommendation;
  if (rec.status === "clear") {
    return "Clear corridor. The standard route crosses no mapped flood zone.";
  }
  if (rec.status === "recommended") {
    return `Take the resilient route. Avoids ${rec.avoided_flood_km.toFixed(2)} km of historically flooded road for ${rec.extra_distance_km.toFixed(2)} km extra.`;
  }
  if (rec.status === "trade_off") {
    return `Trade-off. The resilient route avoids ${rec.avoided_flood_km.toFixed(2)} km but adds ${rec.extra_distance_km.toFixed(2)} km (${rec.extra_distance_pct.toFixed(2)}%). Your call.`;
  }
  return "No lower-flood route found in the road network. Flood exposure on this corridor is unavoidable in the mapped data.";
}

export default function HomePage() {
  const [meta, setMeta] = useState<MetaPayload | null>(null);
  const [floodLayer, setFloodLayer] = useState<FloodFeatureCollection | null>(null);
  const [roadsLayer, setRoadsLayer] = useState<RoadFeatureCollection | null>(null);
  const [scenarios, setScenarios] = useState<DemoScenario[]>([]);
  const [scenarioId, setScenarioId] = useState("");
  const [origin, setOrigin] = useState<LatLon | null>(null);
  const [destination, setDestination] = useState<LatLon | null>(null);
  const [nextTarget, setNextTarget] = useState<"origin" | "destination">("origin");
  const [asOf, setAsOf] = useState("");
  const [presetId, setPresetId] = useState("");
  const [alpha, setAlpha] = useState(0);
  const [beta, setBeta] = useState(0);
  const [advanced, setAdvanced] = useState(false);
  const [resilientMode, setResilientMode] = useState(true);
  const [route, setRoute] = useState<RouteComparePayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingRoute, setLoadingRoute] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [batchRows, setBatchRows] = useState<Record<string, number>[]>([]);
  const [batchResult, setBatchResult] = useState<BatchPayload | null>(null);
  const [batchLoading, setBatchLoading] = useState(false);
  const [currentPosition, setCurrentPosition] = useState<LatLon | null>(null);
  const [navigationActive, setNavigationActive] = useState(false);
  const [navigationError, setNavigationError] = useState<string | null>(null);

  const routeDistanceKm = (point: LatLon): number | null => {
    const coordinates = route?.resilient.geometry.coordinates || [];
    if (coordinates.length < 2) return null;
    return Math.min(
      ...coordinates.map(([lon, lat]) => {
        const latScale = 111;
        const lonScale = 111 * Math.cos((point.lat * Math.PI) / 180);
        const dx = (lon - point.lon) * lonScale;
        const dy = (lat - point.lat) * latScale;
        return Math.sqrt(dx * dx + dy * dy);
      })
    );
  };

  const startNavigation = () => {
    if (!navigator.geolocation) {
      setNavigationError("Browser location is not available.");
      return;
    }
    setNavigationError(null);
    setNavigationActive(true);
  };

  const stopNavigation = () => {
    setNavigationActive(false);
    setCurrentPosition(null);
    setNavigationError(null);
  };

  const requestRoute = useCallback(async (o: LatLon, d: LatLon) => {
    setLoadingRoute(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/route`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          origin: o,
          destination: d,
          as_of: asOf || undefined,
          preset_id: advanced ? undefined : presetId,
          alpha: advanced ? alpha : undefined,
          beta: advanced ? beta : undefined
        })
      });
      const payload = await response.json();
      if (!response.ok) {
        setRoute(null);
        setError(apiError(payload as ApiErrorEnvelope, "The route request failed."));
      } else {
        setRoute(payload as RouteComparePayload);
      }
    } catch {
      setRoute(null);
      setError(ERROR_MESSAGES.NETWORK_ERROR);
    } finally {
      setLoadingRoute(false);
    }
  }, [advanced, alpha, asOf, beta, presetId]);

  useEffect(() => {
    if (!navigationActive) return undefined;
    const watchId = navigator.geolocation.watchPosition(
      (position) => {
        const point = {
          lat: position.coords.latitude,
          lon: position.coords.longitude
        };
        setCurrentPosition(point);
        const distanceKm = routeDistanceKm(point);
        if (distanceKm !== null && distanceKm > 1) {
          setNavigationError("Current location is outside the mapped route. Rerouting from this position.");
          if (destination) void requestRoute(point, destination);
        } else {
          setNavigationError(null);
        }
      },
      () => setNavigationError("Location permission was denied or unavailable."),
      { enableHighAccuracy: true }
    );
    return () => navigator.geolocation.clearWatch(watchId);
  }, [destination, navigationActive, requestRoute, route]);

  const loadRoads = useCallback(async (bounds?: [number, number, number, number], zoom?: number) => {
    if (!bounds) return;
    const query = new URLSearchParams({
      min_lon: String(bounds[0]),
      min_lat: String(bounds[1]),
      max_lon: String(bounds[2]),
      max_lat: String(bounds[3]),
      ...(zoom === undefined ? {} : { zoom: String(zoom) })
    });
    try {
      const response = await fetch(`${API_BASE_URL}/roads-layer?${query}`);
      if (response.ok) setRoadsLayer((await response.json()) as RoadFeatureCollection);
    } catch {
      setError(ERROR_MESSAGES.NETWORK_ERROR);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [metaResponse, floodResponse] = await Promise.all([
          fetch(`${API_BASE_URL}/meta`),
          fetch(`${API_BASE_URL}/flood-layer`)
        ]);
        if (!metaResponse.ok || !floodResponse.ok) {
          throw new Error("The dispatch data could not be loaded.");
        }
        const nextMeta = (await metaResponse.json()) as MetaPayload;
        const nextFlood = (await floodResponse.json()) as FloodFeatureCollection;
        if (cancelled) return;
        setMeta(nextMeta);
        setFloodLayer(nextFlood);
        setAsOf(nextMeta.as_of_default);
        setPresetId(nextMeta.default_preset_id);
        const defaultPreset = nextMeta.presets.find((p) => p.id === nextMeta.default_preset_id);
        setAlpha(defaultPreset?.alpha ?? nextMeta.alpha);
        setBeta(defaultPreset?.beta ?? nextMeta.beta);
        const loaded = nextMeta.demo_scenarios || [];
        setScenarios(loaded);
        const shared = new URLSearchParams(window.location.search);
        const sharedOrigin = shared.get("origin");
        const sharedDestination = shared.get("destination");
        const parsePoint = (value: string | null): LatLon | null => {
          if (!value) return null;
          const [latText, lonText] = value.split(",");
          const lat = Number(latText);
          const lon = Number(lonText);
          return Number.isFinite(lat) && Number.isFinite(lon) ? { lat, lon } : null;
        };
        const sharedOriginPoint = parsePoint(sharedOrigin);
        const sharedDestinationPoint = parsePoint(sharedDestination);
        const sharedPreset = shared.get("preset");
        const sharedAsOf = shared.get("as_of");
        if (sharedOriginPoint && sharedDestinationPoint) {
          setOrigin(sharedOriginPoint);
          setDestination(sharedDestinationPoint);
          setScenarioId("");
        }
        if (sharedAsOf) setAsOf(sharedAsOf);
        if (sharedPreset && nextMeta.presets.some((item) => item.id === sharedPreset)) {
          setPresetId(sharedPreset);
          const preset = nextMeta.presets.find((item) => item.id === sharedPreset);
          if (preset) {
            setAlpha(preset.alpha);
            setBeta(preset.beta);
          }
        }
        if (loaded[0] && !sharedOriginPoint) {
          setScenarioId(loaded[0].id);
          setOrigin(loaded[0].origin);
          setDestination(loaded[0].destination);
        }
        await loadRoads(nextMeta.bbox);
      } catch {
        if (!cancelled) setError("The dispatch data could not be loaded.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [loadRoads]);

  useEffect(() => {
    if (origin && destination && meta && !route) void requestRoute(origin, destination);
  }, [destination, meta, origin, requestRoute, route]);

  const selectScenario = (event: React.ChangeEvent<HTMLSelectElement>) => {
    const found = scenarios.find((item) => item.id === event.target.value);
    setScenarioId(event.target.value);
    if (found) {
      setOrigin(found.origin);
      setDestination(found.destination);
      setNextTarget("origin");
      void requestRoute(found.origin, found.destination);
    }
  };

  const handleMapClick = (point: LatLon) => {
    setScenarioId("");
    if (nextTarget === "origin") {
      setOrigin(point);
      setDestination(null);
      setRoute(null);
      setNextTarget("destination");
    } else {
      setDestination(point);
      setNextTarget("origin");
      if (origin) void requestRoute(origin, point);
    }
  };

  const swap = () => {
    setOrigin(destination);
    setDestination(origin);
    if (destination && origin) void requestRoute(destination, origin);
  };

  const clear = () => {
    setOrigin(null);
    setDestination(null);
    setRoute(null);
    setScenarioId("");
    setNextTarget("origin");
  };

  const bbox: [number, number, number, number] = meta?.bbox || [0, 0, 1, 1];
  const dateOptions = meta?.as_of_options;
  const selectedPreset = useMemo(
    () => meta?.presets.find((item) => item.id === presetId),
    [meta, presetId]
  );

  const copyLink = async () => {
    const url = new URL(window.location.href);
    url.pathname = "/";
    url.search = "";
    if (origin && destination) {
      url.searchParams.set("origin", `${origin.lat},${origin.lon}`);
      url.searchParams.set("destination", `${destination.lat},${destination.lon}`);
    }
    if (presetId) url.searchParams.set("preset", presetId);
    if (asOf) url.searchParams.set("as_of", asOf);
    await navigator.clipboard.writeText(url.toString());
    setError("Shareable link copied.");
  };

  const parseBatchCsv = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const text = await file.text();
    const lines = text.split(/\r?\n/).filter((line) => line.trim().length > 0);
    if (lines.length < 2) {
      setError("CSV must include a header and at least one row.");
      return;
    }
    const headers = lines[0].split(",").map((value) => value.trim());
    const required = ["origin_lat", "origin_lon", "dest_lat", "dest_lon"];
    if (!required.every((name) => headers.includes(name))) {
      setError("CSV must contain origin_lat, origin_lon, dest_lat, and dest_lon.");
      return;
    }
    const rows = lines.slice(1).map((line) => {
      const values = line.split(",");
      return Object.fromEntries(headers.map((header, index) => [header, Number(values[index])]));
    });
    setBatchRows(rows);
    setBatchResult(null);
    setError(null);
  };

  const runBatch = async () => {
    setBatchLoading(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/route/batch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          rows: batchRows,
          as_of: asOf || undefined,
          preset_id: advanced ? undefined : presetId,
          alpha: advanced ? alpha : undefined,
          beta: advanced ? beta : undefined
        })
      });
      const body = await response.json();
      if (!response.ok) {
        setError(apiError(body as ApiErrorEnvelope, "The batch request failed."));
      } else {
        setBatchResult(body as BatchPayload);
      }
    } catch {
      setError(ERROR_MESSAGES.NETWORK_ERROR);
    } finally {
      setBatchLoading(false);
    }
  };

  const downloadBatch = async () => {
    const response = await fetch(`${API_BASE_URL}/route/batch/csv`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        rows: batchRows,
        as_of: asOf || undefined,
        preset_id: advanced ? undefined : presetId,
        alpha: advanced ? alpha : undefined,
        beta: advanced ? beta : undefined
      })
    });
    if (!response.ok) {
      setError("The batch CSV could not be downloaded.");
      return;
    }
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "route_batch.csv";
    link.click();
    URL.revokeObjectURL(link.href);
  };

  return (
    <main className="app-shell">
      <aside className="control-sidebar" aria-label="Dispatch controls">
        <header className="app-header">
          <h1>Logistics Dispatch Console</h1>
          <p>Compare a standard shortest route with a flood-resilient weighted route.</p>
        </header>
        <div className="honesty-strip">
          Based on 2012 satellite flood extents and a recent rainfall index. Avoids mapped flood zones. Not live flood conditions.
        </div>
        {error ? <div className="alert-box" role="alert">{error}</div> : null}
        <section className="recommendation-banner" aria-live="polite">
          <strong>{route ? `Recommendation: ${route.recommendation.status.replace("_", " ")}` : "Recommendation"}</strong>
          <span>{recommendationText(route)}</span>
        </section>
        <section className="panel-card">
          <h2>Sample dispatch corridors</h2>
          <select value={scenarioId} onChange={selectScenario} disabled={loading}>
            <option value="">Custom map selection</option>
            {scenarios.map((scenario) => (
              <option key={scenario.id} value={scenario.id}>
                {scenario.origin.lat.toFixed(2)} to {scenario.destination.lat.toFixed(2)}, {scenario.baseline_length_km.toFixed(1)} km, {scenario.baseline_flood_km_2012.toFixed(2)} km flood
              </option>
            ))}
          </select>
          <small>{meta?.demo_selection_rule || "Selected by the configured rule, not hand-picked."}</small>
          <div className="field-row">
            <span>Origin: {coordText(origin)}</span>
            <span>Destination: {coordText(destination)}</span>
          </div>
        </section>
        <section className="panel-card">
          <h2>Share and batch</h2>
          <button className="secondary-btn" type="button" onClick={copyLink} disabled={!origin || !destination}>Copy link</button>
          <label htmlFor="batch-file">Batch CSV</label>
          <input id="batch-file" type="file" accept=".csv,text/csv" onChange={parseBatchCsv} />
          <small>Columns: origin_lat, origin_lon, dest_lat, dest_lon</small>
          {batchRows.length > 0 ? <button className="primary-btn" type="button" onClick={runBatch} disabled={batchLoading}>{batchLoading ? "Running batch..." : `Run ${batchRows.length} rows`}</button> : null}
          {batchResult ? <div className="batch-summary">
            <strong>{batchResult.totals.succeeded_rows} succeeded, {batchResult.totals.failed_rows} failed</strong>
            <span>Standard: {batchResult.totals.baseline_length_km.toFixed(2)} km, {batchResult.totals.baseline_flood_km.toFixed(2)} km flooded</span>
            <span>Resilient: {batchResult.totals.resilient_length_km.toFixed(2)} km, {batchResult.totals.resilient_flood_km.toFixed(2)} km flooded</span>
            <button className="secondary-btn" type="button" onClick={downloadBatch}>Download CSV</button>
          </div> : null}
        </section>
        <form className="panel-card" onSubmit={(event) => {
          event.preventDefault();
          if (origin && destination) void requestRoute(origin, destination);
          else setError(ERROR_MESSAGES.INVALID_INPUT);
        }}>
          <h2>Route parameters</h2>
          <div className="preset-list" role="group" aria-label="Routing presets">
            {(meta?.presets || []).map((preset) => (
              <button type="button" className={presetId === preset.id && !advanced ? "preset active" : "preset"} key={preset.id} onClick={() => {
                setPresetId(preset.id);
                setAlpha(preset.alpha);
                setBeta(preset.beta);
                setAdvanced(false);
              }}>{preset.label}</button>
            ))}
          </div>
          <div className="date-actions">
            <button type="button" className="secondary-btn" title={dateOptions?.rule} onClick={() => dateOptions && setAsOf(dateOptions.wet.as_of)}>Wet-season date</button>
            <button type="button" className="secondary-btn" title={dateOptions?.rule} onClick={() => dateOptions && setAsOf(dateOptions.dry.as_of)}>Dry-season date</button>
          </div>
          <label htmlFor="as-of">Rainfall date</label>
          <input id="as-of" type="date" value={asOf} onChange={(event) => setAsOf(event.target.value)} />
          <button type="button" className="advanced-toggle" onClick={() => setAdvanced((value) => !value)} aria-expanded={advanced}>Advanced parameters</button>
          {advanced ? <div className="field-row">
            <label>alpha<input type="number" step="any" value={alpha} onChange={(event) => setAlpha(Number(event.target.value))} /></label>
            <label>beta<input type="number" step="any" value={beta} onChange={(event) => setBeta(Number(event.target.value))} /></label>
          </div> : <small>{selectedPreset?.label || "Config weights"}</small>}
          <button className="primary-btn" type="submit" disabled={loadingRoute}>{loadingRoute ? "Calculating routes..." : "Update route"}</button>
        </form>
        <section className="panel-card">
          <h2>Map controls</h2>
          <div className="button-grid">
            <button className="secondary-btn" type="button" onClick={() => window.dispatchEvent(new CustomEvent("map-zoom-in"))}>Zoom in</button>
            <button className="secondary-btn" type="button" onClick={() => window.dispatchEvent(new CustomEvent("map-zoom-out"))}>Zoom out</button>
            <button className="secondary-btn" type="button" onClick={() => window.dispatchEvent(new CustomEvent("map-reset"))}>Reset view</button>
            <button className="secondary-btn" type="button" onClick={clear}>Clear</button>
            <button className="secondary-btn" type="button" onClick={swap}>Swap origin and destination</button>
          </div>
          <button className="secondary-btn" type="button" onClick={() => setResilientMode((value) => !value)} aria-pressed={resilientMode}>
            {resilientMode ? "Emphasize resilient route" : "Emphasize standard route"}
          </button>
        </section>
        {route ? <section className="panel-card" aria-live="polite">
          <h2>Distances in km</h2>
          <div className="metrics-grid">
            <div className="metric-card card-baseline"><strong>Standard</strong><span>{route.baseline.length_km.toFixed(2)} km</span><span>{route.baseline.flood_km.toFixed(2)} km flooded</span></div>
            <div className="metric-card card-resilient"><strong>Resilient</strong><span>{route.resilient.length_km.toFixed(2)} km</span><span>{route.resilient.flood_km.toFixed(2)} km flooded</span></div>
          </div>
          <h2>Route guidance</h2>
          <button className="primary-btn" type="button" onClick={navigationActive ? stopNavigation : startNavigation}>
            {navigationActive ? "Stop navigation" : "Start navigation"}
          </button>
          {navigationError ? <div className="alert-box" role="alert">{navigationError}</div> : null}
          <ol className="route-steps">
            {(resilientMode ? (route.resilient.steps || []) : (route.baseline.steps || [])).slice(0, 12).map((step) => (
              <li key={step.step}>
                <strong>{step.instruction}</strong>
                <span>{step.road_label}: {step.length_km.toFixed(2)} km</span>
                <small>From {coordText(step.start)} to {coordText(step.end)}</small>
              </li>
            ))}
          </ol>
        </section> : null}
      </aside>
      <section className="map-stage">
        {loading ? <div className="loading-skeleton" aria-label="Loading map" /> : <RouteMap bbox={bbox} floodLayer={floodLayer} roadsLayer={roadsLayer} origin={origin} destination={destination} currentPosition={currentPosition} routeData={route} resilientMode={resilientMode} mapboxToken={MAPBOX_TOKEN} onMapClick={handleMapClick} onMove={loadRoads} />}
      </section>
    </main>
  );
}
