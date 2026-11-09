"use client";

import React, { useEffect, useState } from "react";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type JsonRecord = Record<string, unknown>;

interface EvidencePayload {
  available: boolean;
  message?: string;
  results?: JsonRecord;
  figures?: string[];
}

function record(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

function numberText(value: unknown): string {
  return typeof value === "number" ? value.toFixed(2) : "Not reported";
}

function label(value: string): string {
  return value.replaceAll("_", " ");
}

function MetricsTable({ title, data }: { title: string; data: JsonRecord }) {
  const rows = Object.entries(data).filter(([, value]) => typeof value === "number");
  return (
    <div className="evidence-table-wrap">
      <h3>{title}</h3>
      <table className="evidence-table">
        <tbody>
          {rows.map(([key, value]) => (
            <tr key={key}><th>{label(key)}</th><td>{numberText(value)}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function EvidencePage() {
  const [payload, setPayload] = useState<EvidencePayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API_BASE_URL}/evidence-data`)
      .then(async (response) => {
        const body = (await response.json()) as EvidencePayload;
        if (!response.ok) throw new Error("Evidence could not be loaded.");
        setPayload(body);
      })
      .catch(() => setError("Evidence could not be loaded."));
  }, []);

  if (error) return <main className="evidence-page"><div className="alert-box">{error}</div></main>;
  if (!payload) return <main className="evidence-page"><div className="loading-skeleton" /></main>;
  if (!payload.available || !payload.results) {
    return <main className="evidence-page"><h1>Evaluation evidence</h1><p>Run make reproduce</p></main>;
  }

  const results = payload.results;
  const methods = record(results.methods);
  const connector = record(results.connector_road_comparison);
  const figures = payload.figures || [];

  return (
    <main className="evidence-page">
      <header className="evidence-header">
        <a href="/">Back to dispatch console</a>
        <h1>Evaluation evidence</h1>
        <p>{String(results.temporal_validation_note || "")}</p>
      </header>

      <section className="evidence-card">
        <h2>Headline results</h2>
        <div className="evidence-methods">
          {Object.entries(methods).map(([methodName, methodValue]) => {
            const method = record(methodValue);
            return (
              <article key={methodName}>
                <h3>{label(methodName)}</h3>
                {(["in_sample_2012", "held_out_2022"] as const).map((split) => {
                  const splitData = record(method[split]);
                  return Object.entries(splitData).map(([subset, subsetValue]) => (
                    <MetricsTable key={`${split}-${subset}`} title={`${label(split)} - ${label(subset)}`} data={record(subsetValue)} />
                  ));
                })}
              </article>
            );
          })}
        </div>
      </section>

      <section className="evidence-card">
        <h2>Sensitivity plot</h2>
        {figures.filter((figure) => figure.toLowerCase().includes("sensitivity")).map((figure) => (
          <img key={figure} className="evidence-figure" src={`${API_BASE_URL}/evidence-figure/${encodeURIComponent(figure)}`} alt={figure} />
        ))}
        <p>{String(results.knee_selection_note || "")}</p>
      </section>

      <section className="evidence-card">
        <h2>Held-out 2012 to 2022 result</h2>
        <p>{String(results.temporal_validation_note || "")}</p>
      </section>

      <section className="evidence-card">
        <h2>Unknown-road flag settings</h2>
        {Object.entries(connector).map(([setting, settingValue]) => (
          <MetricsTable key={setting} title={label(setting)} data={record(settingValue)} />
        ))}
      </section>

      <section className="evidence-card">
        <h2>Limitations</h2>
        <p>{String(results.knee_selection_note || "")}</p>
        <p>{String(results.temporal_validation_note || "")}</p>
      </section>
    </main>
  );
}
