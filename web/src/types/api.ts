export interface LatLon {
  lat: number;
  lon: number;
}

export interface DemoScenario {
  id: string;
  rank: number;
  pair_index: number;
  label: string;
  origin: LatLon;
  destination: LatLon;
  as_of: string;
  baseline_length_km: number;
  baseline_flood_km_2012: number;
  resilient_length_km: number;
  resilient_flood_km_2012: number;
  flood_km_reduction_pct_2012: number | null;
  extra_distance_pct: number;
}

export interface ManifestRecord {
  filename: string;
  row_count: number;
  sha256: string;
  source_url: string;
  access_date: string;
  license: string;
}

export interface MetaPayload {
  bbox: [number, number, number, number];
  alpha: number;
  beta: number;
  as_of_default: string;
  data_manifest: ManifestRecord[];
  region?: string;
  demo_scenarios?: DemoScenario[];
}

export interface GeoJSONLineString {
  type: "LineString";
  coordinates: [number, number][];
}

export interface GeoJSONGeometry {
  type: "Polygon" | "MultiPolygon";
  coordinates: number[][][] | number[][][][];
}

export interface FloodFeature {
  type: "Feature";
  id?: number | string;
  properties: Record<string, unknown>;
  geometry: GeoJSONGeometry;
}

export interface FloodFeatureCollection {
  type: "FeatureCollection";
  features: FloodFeature[];
}

export interface RouteSummary {
  geometry: GeoJSONLineString;
  length_km: number;
  flood_km: number;
  weighted_cost?: number;
  n_edges?: number;
}

export interface RouteDifference {
  flood_km_reduction_pct: number | null;
  extra_distance_pct: number | null;
}

export interface RouteParams {
  alpha: number;
  beta: number;
  as_of: string;
}

export interface RouteSnap {
  origin_m: number;
  destination_m: number;
}

export interface RouteComparePayload {
  baseline: RouteSummary;
  resilient: RouteSummary;
  delta: RouteDifference;
  params: RouteParams;
  snap: RouteSnap;
}

export interface ApiErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
  };
}
