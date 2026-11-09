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

export interface Preset {
  id: string;
  label: string;
  alpha: number;
  beta: number;
}

export interface AsOfOption {
  as_of: string;
  mean_recent_mm: number;
  mean_rain_idx: number;
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
  demo_selection_rule?: string;
  presets: Preset[];
  default_preset_id: string;
  max_acceptable_detour_pct: number;
  flood_tolerance_km: number;
  max_batch_pairs: number;
  as_of_options?: {
    rule: string;
    wet: AsOfOption;
    dry: AsOfOption;
  } | null;
  as_of_options_error?: string | null;
}

export interface GeoJSONLineString {
  type: "LineString";
  coordinates: [number, number][];
}

export interface GeoJSONGeometry {
  type: "LineString" | "Polygon" | "MultiPolygon";
  coordinates: [number, number][] | number[][][] | number[][][][];
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

export interface RoadFeatureCollection {
  type: "FeatureCollection";
  features: FloodFeature[];
  truncated: boolean;
  total_matching: number;
  returned_features: number;
  max_features: number;
  road_classes: string[];
  zoom?: number;
  bbox?: [number, number, number, number];
}

export interface RouteSummary {
  geometry: GeoJSONLineString;
  length_km: number;
  flood_km: number;
  weighted_cost?: number;
  n_edges?: number;
  flooded_segments?: FloodFeatureCollection;
  steps?: RouteStep[];
}

export interface RouteStep {
  step: number;
  instruction: string;
  road_label: string;
  start: LatLon;
  end: LatLon;
  length_km: number;
  flood_km: number;
}

export interface RouteDifference {
  flood_km_reduction_pct: number | null;
  extra_distance_pct: number | null;
}

export interface RouteParams {
  alpha: number;
  beta: number;
  as_of: string;
  preset_id?: string | null;
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
  recommendation: {
    status: "clear" | "recommended" | "trade_off" | "no_better_route";
    baseline_flood_km: number;
    resilient_flood_km: number;
    avoided_flood_km: number;
    extra_distance_km: number;
    extra_distance_pct: number;
  };
}

export interface ApiErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
  };
}

export interface BatchRowResult {
  row: number;
  status: "ok" | "error";
  baseline_length_km?: number | null;
  baseline_flood_km?: number | null;
  resilient_length_km?: number | null;
  resilient_flood_km?: number | null;
  avoided_flood_km?: number | null;
  extra_distance_km?: number | null;
  extra_distance_pct?: number | null;
  recommendation?: string | null;
  error_code?: string | null;
  error_message?: string | null;
}

export interface BatchPayload {
  rows: BatchRowResult[];
  totals: {
    total_rows: number;
    succeeded_rows: number;
    failed_rows: number;
    baseline_length_km: number;
    baseline_flood_km: number;
    resilient_length_km: number;
    resilient_flood_km: number;
    avoided_flood_km: number;
    extra_distance_km: number;
    extra_distance_pct: number | null;
    status_counts: Record<string, number>;
  };
}
