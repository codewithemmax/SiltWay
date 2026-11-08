"use client";

import React, { useCallback, useEffect, useState } from "react";
import RouteMap from "@/components/RouteMap";
import type {
  ApiErrorEnvelope,
  DemoScenario,
  FloodFeatureCollection,
  LatLon,
  MetaPayload,
  RouteComparePayload
} from "@/types/api";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const MAPBOX_TOKEN = process.env.NEXT_PUBLIC_MAPBOX_TOKEN || "";

function formatCoord(pt: LatLon | null): string {
  if (!pt) {
    return "Not set (click map)";
  }
  return `${pt.lat.toFixed(4)}, ${pt.lon.toFixed(4)}`;
}

function describeRouteStatus(routeData: RouteComparePayload | null): string {
  if (!routeData) {
    return "No active route calculated yet. Click twice on the map or choose a scenario.";
  }
  const baseFlood = routeData.baseline.flood_km;
  const resFlood = routeData.resilient.flood_km;
  const redPct = routeData.delta.flood_km_reduction_pct;
  const extraPct = routeData.delta.extra_distance_pct ?? 0.0;

  if (baseFlood <= 0.0) {
    return "Status: Baseline route is already clear of 2012 historical flood zones (0.00 km flood exposure).";
  }
  if (redPct !== null && redPct > 0.0) {
    return (
      `Status: Flood-Resilient route reduces flood-zone exposure by ${redPct.toFixed(2)}% ` +
      `(${baseFlood.toFixed(2)} km down to ${resFlood.toFixed(2)} km) ` +
      `with +${extraPct.toFixed(2)}% extra distance.`
    );
  }
  return (
    `Status: Both routes traverse ${baseFlood.toFixed(2)} km of flood-zone road ` +
    `(corridor crosses a single-bridge bottleneck or detour cost exceeds penalty threshold).`
  );
}

export default function HomePage() {
  const [meta, setMeta] = useState<MetaPayload | null>(null);
  const [floodLayer, setFloodLayer] = useState<FloodFeatureCollection | null>(
    null
  );
  const [scenarios, setScenarios] = useState<DemoScenario[]>([]);
  const [selectedScenarioId, setSelectedScenarioId] = useState<string>("");

  const [origin, setOrigin] = useState<LatLon | null>(null);
  const [destination, setDestination] = useState<LatLon | null>(null);
  const [nextClickTarget, setNextClickTarget] = useState<
    "origin" | "destination"
  >("origin");

  const [asOf, setAsOf] = useState<string>("");
  const [alpha, setAlpha] = useState<number>(1.0);
  const [beta, setBeta] = useState<number>(0.5);
  const [resilientMode, setResilientMode] = useState<boolean>(true);

  const [routeData, setRouteData] = useState<RouteComparePayload | null>(null);
  const [loadingMeta, setLoadingMeta] = useState<boolean>(true);
  const [loadingRoute, setLoadingRoute] = useState<boolean>(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const requestRoute = useCallback(
    async (
      orig: LatLon,
      dest: LatLon,
      asOfVal: string,
      alphaVal: number,
      betaVal: number
    ) => {
      setLoadingRoute(true);
      setErrorMessage(null);
      try {
        const response = await fetch(`${API_BASE_URL}/route`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            origin: orig,
            destination: dest,
            as_of: asOfVal || undefined,
            alpha: alphaVal,
            beta: betaVal
          })
        });

        const payload = await response.json();
        if (!response.ok) {
          const errEnv = payload as ApiErrorEnvelope;
          const code = errEnv.error?.code || `HTTP_${response.status}`;
          const msg =
            errEnv.error?.message || "Routing request failed on the server.";
          setErrorMessage(`[${code}] ${msg}`);
          setRouteData(null);
          return;
        }
        setRouteData(payload as RouteComparePayload);
      } catch (err) {
        const detail =
          err instanceof Error ? err.message : "Network connection failed.";
        setErrorMessage(`[NETWORK_ERROR] Unable to reach ${API_BASE_URL}: ${detail}`);
        setRouteData(null);
      } finally {
        setLoadingRoute(false);
      }
    },
    []
  );

  useEffect(() => {
    let cancelled = false;
    async function loadInitialData() {
      setLoadingMeta(true);
      setErrorMessage(null);
      try {
        const [metaRes, floodRes] = await Promise.all([
          fetch(`${API_BASE_URL}/meta`),
          fetch(`${API_BASE_URL}/flood-layer`)
        ]);

        if (!metaRes.ok) {
          throw new Error(`GET /meta returned status ${metaRes.status}`);
        }
        if (!floodRes.ok) {
          throw new Error(`GET /flood-layer returned status ${floodRes.status}`);
        }

        const metaJson = (await metaRes.json()) as MetaPayload;
        const floodJson = (await floodRes.json()) as FloodFeatureCollection;
        if (cancelled) {
          return;
        }

        setMeta(metaJson);
        setFloodLayer(floodJson);
        setAlpha(metaJson.alpha);
        setBeta(metaJson.beta);
        setAsOf(metaJson.as_of_default);

        const loadedScenarios = metaJson.demo_scenarios || [];
        setScenarios(loadedScenarios);

        if (loadedScenarios.length > 0) {
          const defaultScen =
            loadedScenarios.find(
              (s) =>
                s.flood_km_reduction_pct_2012 !== null &&
                s.flood_km_reduction_pct_2012 > 0.0
            ) || loadedScenarios[0];
          setSelectedScenarioId(defaultScen.id);
          setOrigin(defaultScen.origin);
          setDestination(defaultScen.destination);
          await requestRoute(
            defaultScen.origin,
            defaultScen.destination,
            defaultScen.as_of || metaJson.as_of_default,
            metaJson.alpha,
            metaJson.beta
          );
        }
      } catch (err) {
        if (!cancelled) {
          const detail =
            err instanceof Error ? err.message : "Failed to load metadata.";
          setErrorMessage(
            `[STARTUP_ERROR] Could not load /meta or /flood-layer from ${API_BASE_URL}: ${detail}`
          );
        }
      } finally {
        if (!cancelled) {
          setLoadingMeta(false);
        }
      }
    }

    loadInitialData();
    return () => {
      cancelled = true;
    };
  }, [requestRoute]);

  const handleScenarioChange = async (
    event: React.ChangeEvent<HTMLSelectElement>
  ) => {
    const scenId = event.target.value;
    setSelectedScenarioId(scenId);
    const found = scenarios.find((s) => s.id === scenId);
    if (!found) {
      return;
    }
    const nextAsOf = found.as_of || asOf;
    setOrigin(found.origin);
    setDestination(found.destination);
    setAsOf(nextAsOf);
    setNextClickTarget("origin");
    await requestRoute(found.origin, found.destination, nextAsOf, alpha, beta);
  };

  const handleMapClick = async (coord: LatLon) => {
    setSelectedScenarioId("");
    if (nextClickTarget === "origin") {
      setOrigin(coord);
      setDestination(null);
      setRouteData(null);
      setErrorMessage(null);
      setNextClickTarget("destination");
    } else {
      setDestination(coord);
      setNextClickTarget("origin");
      if (origin) {
        await requestRoute(origin, coord, asOf, alpha, beta);
      }
    }
  };

  const handleRecalculate = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!origin || !destination) {
      setErrorMessage(
        "[INVALID_INPUT] Please click twice on the map or select a scenario to set Origin and Destination."
      );
      return;
    }
    await requestRoute(origin, destination, asOf, alpha, beta);
  };

  const bbox: [number, number, number, number] = meta?.bbox || [
    0.0, 0.0, 1.0, 1.0
  ];

  return (
    <main className="app-shell">
      <aside className="control-sidebar" aria-label="Routing Controls and Metrics">
        <header className="app-header">
          <h1>Flood-Resilient Router ({meta?.region || "Loading..."})</h1>
          <p>
            Weighted road network routing combining 2012 MODIS flood exposure
            F(e) and NASA POWER rainfall anomaly R(e).
          </p>
        </header>

        {errorMessage ? (
          <div className="alert-box" role="alert" aria-live="assertive">
            <strong>Routing Error:</strong> {errorMessage}
          </div>
        ) : null}

        <section className="panel-card" aria-label="Route Display Mode">
          <h2>Display Emphasis Mode</h2>
          <div className="toggle-row">
            <span
              className={`mode-badge ${
                resilientMode ? "badge-resilient" : "badge-baseline"
              }`}
            >
              {resilientMode
                ? "Active: Flood-Resilient Mode (Teal)"
                : "Active: Baseline Mode (Red)"}
            </span>
            <button
              type="button"
              className="secondary-btn"
              onClick={() => setResilientMode((prev) => !prev)}
              aria-pressed={resilientMode}
            >
              Toggle Flood-Resilient Mode
            </button>
          </div>
          <p style={{ fontSize: "12px", color: "#94a3b8" }}>
            Both routes remain visible on the map; toggling switches which route
            is emphasized at full opacity.
          </p>
        </section>

        <section className="panel-card" aria-label="Scenario and Endpoint Selection">
          <h2>Scenario &amp; Endpoints</h2>
          <div className="field-group">
            <label htmlFor="scenario-select">
              Seeded Demo Scenarios (from config/demo_scenarios.json)
            </label>
            <select
              id="scenario-select"
              value={selectedScenarioId}
              onChange={handleScenarioChange}
              disabled={loadingMeta || scenarios.length === 0}
            >
              <option value="">-- Custom Map Click Selection --</option>
              {scenarios.map((scen) => (
                <option key={scen.id} value={scen.id}>
                  {scen.label}
                </option>
              ))}
            </select>
          </div>

          <div className="field-row">
            <div className="field-group">
              <label>Origin (O)</label>
              <div style={{ fontSize: "12px", color: "#cbd5e1" }}>
                {formatCoord(origin)}
              </div>
            </div>
            <div className="field-group">
              <label>Destination (D)</label>
              <div style={{ fontSize: "12px", color: "#cbd5e1" }}>
                {formatCoord(destination)}
              </div>
            </div>
          </div>
          <div style={{ fontSize: "12px", color: "#38bdf8" }}>
            Next map click sets:{" "}
            <strong>
              {nextClickTarget === "origin" ? "Origin (1st click)" : "Destination (2nd click)"}
            </strong>
          </div>
        </section>

        <form
          className="panel-card"
          onSubmit={handleRecalculate}
          aria-label="Rainfall Date and Weight Parameters"
        >
          <h2>Rainfall Date &amp; Weights</h2>
          <div className="field-group">
            <label htmlFor="as-of-input">Rainfall Date (as_of)</label>
            <input
              id="as-of-input"
              type="date"
              value={asOf}
              onChange={(e) => setAsOf(e.target.value)}
            />
          </div>

          <div className="field-row">
            <div className="field-group">
              <label htmlFor="alpha-input">Flood Penalty (alpha)</label>
              <input
                id="alpha-input"
                type="number"
                step="any"
                min={0}
                max={100}
                value={alpha}
                onChange={(e) => setAlpha(Number(e.target.value))}
              />
            </div>
            <div className="field-group">
              <label htmlFor="beta-input">Rain Penalty (beta)</label>
              <input
                id="beta-input"
                type="number"
                step="any"
                min={0}
                max={100}
                value={beta}
                onChange={(e) => setBeta(Number(e.target.value))}
              />
            </div>
          </div>

          <button
            type="submit"
            className="primary-btn"
            disabled={loadingRoute || loadingMeta}
          >
            {loadingRoute ? "Calculating Routes..." : "Update Route Comparison"}
          </button>
        </form>

        <section
          className="panel-card"
          aria-label="Route Comparison Metrics"
          aria-live="polite"
        >
          <h2>Route Comparison Metrics</h2>
          <div className="status-box">{describeRouteStatus(routeData)}</div>

          {routeData ? (
            <>
              <div className="metrics-grid">
                <div className="metric-card card-baseline">
                  <div className="metric-title">
                    Baseline Route (Red Dashed)
                  </div>
                  <div className="metric-line">
                    Distance:{" "}
                    <strong>{routeData.baseline.length_km.toFixed(2)} km</strong>
                  </div>
                  <div className="metric-line">
                    Flood Exposure:{" "}
                    <strong>{routeData.baseline.flood_km.toFixed(3)} km</strong>
                  </div>
                </div>

                <div className="metric-card card-resilient">
                  <div className="metric-title">
                    Resilient Route (Teal Solid)
                  </div>
                  <div className="metric-line">
                    Distance:{" "}
                    <strong>{routeData.resilient.length_km.toFixed(2)} km</strong>
                  </div>
                  <div className="metric-line">
                    Flood Exposure:{" "}
                    <strong>{routeData.resilient.flood_km.toFixed(3)} km</strong>
                  </div>
                </div>
              </div>

              <div className="delta-summary">
                <div>
                  <strong>Flood-Zone km Reduction:</strong>{" "}
                  {routeData.delta.flood_km_reduction_pct !== null
                    ? `${routeData.delta.flood_km_reduction_pct.toFixed(2)}%`
                    : "N/A (0.00 km baseline flood exposure)"}
                </div>
                <div>
                  <strong>Extra Distance vs Baseline:</strong>{" "}
                  {routeData.delta.extra_distance_pct !== null
                    ? `+${routeData.delta.extra_distance_pct.toFixed(2)}%`
                    : "N/A"}
                </div>
                <div>
                  <strong>Parameters Used:</strong> alpha=
                  {routeData.params.alpha}, beta={routeData.params.beta}, as_of=
                  {routeData.params.as_of}
                </div>
                <div>
                  <strong>Endpoint Snap Distance:</strong> Origin{" "}
                  {routeData.snap.origin_m.toFixed(0)} m, Destination{" "}
                  {routeData.snap.destination_m.toFixed(0)} m
                </div>
              </div>
            </>
          ) : null}
        </section>
      </aside>

      <section className="map-stage" aria-label="Interactive Route Map">
        <RouteMap
          bbox={bbox}
          floodLayer={floodLayer}
          origin={origin}
          destination={destination}
          routeData={routeData}
          resilientMode={resilientMode}
          mapboxToken={MAPBOX_TOKEN}
          onMapClick={handleMapClick}
        />
      </section>
    </main>
  );
}
