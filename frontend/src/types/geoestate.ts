export type CityKey = "saint_petersburg" | "moscow";

export interface CityStatsInfo {
  total: number;
  unique_locations: number;
  by_source: Record<string, number>;
}

export interface CityInfo {
  key: CityKey;
  name: string;
  center: [number, number];
  default_radius_m: number;
  municipal_source: string;
  db_exists: boolean;
  stats: CityStatsInfo;
}

export interface SecretsStatus {
  nvidia_configured: boolean;
  nvidia_masked: string;
  dadata_configured: boolean;
  dadata_masked: string;
  yandex_maps_configured?: boolean;
  yandex_maps_masked?: string;
  dgis_maps_configured?: boolean;
  dgis_maps_masked?: string;
}

export interface MapKeysConfig {
  yandex_maps_key: string;
  dgis_maps_key: string;
}

export interface HealthResponse {
  status: string;
  runtime_root: string;
  cities: CityInfo[];
  secrets: SecretsStatus;
}

export interface ListingPreview {
  id: number | string;
  source: string;
  external_id: string | null;
  title: string | null;
  address: string | null;
  lat: number | null;
  lng: number | null;
  area_sqm: number | null;
  price_month: number | null;
  price_sqm_year?: number | null;
  price_sqm_month?: number | null;
  last_seen_at?: string | null;
  floor: number | string | null;
  url: string | null;
  description?: string | null;
  has_separate_entrance?: boolean | null;
  power_kw?: number | null;
  photos?: string[];
}

export interface ListingsResponse {
  city: CityKey;
  total_matched?: number;
  items: ListingPreview[];
}

export interface SearchRequest {
  city: CityKey;
  target_business: string;
  location_query: string | null;
  min_area_sqm: number | null;
  max_area_sqm: number | null;
  target_area_sqm: number | null;
  max_price_month: number | null;
  max_price_sqm: number | null;
  price_sqm_period: "month" | "year";
  full_cost_limit: number | null;
  allowed_floors: number[] | null;
  business_concept: string;
  max_candidates: number;
  analysis_limit: number | null;
  include_unverified: boolean;
  skip_collect: boolean;
  exclude_property_keys: string[];
  center_lat?: number | null;
  center_lng?: number | null;
  search_radius_m?: number | null;
}

export interface RankingCriterion {
  metric: string;
  label: string;
  weight: number;
  preference: string;
  low: number | null;
  high: number | null;
  concept_quote: string;
  reason: string;
  assumption: string | null;
  scale_source: string;
  usable: boolean;
}

export interface RankingProfile {
  status: string;
  label: string;
  criteria: RankingCriterion[];
  assumptions: string[];
  questions: string[];
  unmodeled_requirements: Array<{ quote: string; status: string }>;
}

export interface RequirementCoverageItem {
  concept_quote: string;
  status: string;
  metric?: string;
  reason?: string;
}

export interface FloorProfile {
  allowed_floors?: number[] | null;
  rule?: string;
  summary?: string;
  [key: string]: unknown;
}

export interface ConfirmationReason {
  code?: string;
  message?: string;
  missing_facts?: string[];
  admission_details?: {
    reason?: string;
    missing_facts?: string[];
  };
  violations?: Array<Record<string, unknown>>;
}

export interface ConfirmationCheckItem {
  id?: string;
  client_quote: string;
  result: "pass" | "fail" | "unknown" | string;
  reason?: string;
  importance?: string;
  label?: string;
  evidence_ids?: string[];
}

export interface ConfirmationCheck {
  result: "pass" | "fail" | "unknown" | "not_applicable" | string;
  reason?: string;
  evidence_ids?: string[];
  items?: ConfirmationCheckItem[];
  primary?: {
    result: string;
    reason?: string;
  };
  independent?: {
    verdict?: string;
    reason?: string;
    error_type?: string;
  };
}

export interface ConfirmationSource {
  source?: string;
  url?: string;
  identity_checked?: boolean;
  publication_active?: boolean;
  fetched_at?: number;
  floor?: number | string | null;
  area_sqm?: number | null;
  price_month?: number | null;
  auction_start_only?: boolean;
}

export interface ConfirmationData {
  status?: "source_conditions_checked" | "requires_confirmation" | "not_suitable" | string;
  label?: string;
  processing_status?: string;
  completed_conditions?: number;
  reasons?: ConfirmationReason[];
  checks?: Record<string, ConfirmationCheck>;
  sources?: ConfirmationSource[];
  evidence?: Record<string, unknown>;
  models?: string[];
  photo_analysis?: {
    status?: string;
    reason?: string;
    model?: string;
    error_type?: string;
  };
  changed_fields?: string[];
  counterparty_confirmation?: boolean;
  on_site_inspection?: boolean;
}

export interface ScoringComponent {
  metric: string;
  label: string;
  unit: string;
  weight: number | null;
  value_lower: number | null;
  value_upper: number | null;
  contribution_lower: number | null;
  contribution_upper: number | null;
}

export interface CompetitorRecord {
  level: "DIRECT" | "INDIRECT" | "SYNERGY" | string;
  reason: string;
  org_id: string | null;
  title: string;
  categories: string[];
  address: string | null;
  lat: number | null;
  lng: number | null;
  distance_m: number;
  rating: number | null;
  ratings_count: number | null;
  reviews_count: number | null;
  working_hours: string | null;
  is_chain: boolean | null;
  url: string | null;
  websites: string[];
  traffic_profile_str: string | null;
  fns_match_status: string | null;
  fns_inn: string | null;
  fns_legal_name: string | null;
  fns_okved: string | null;
  fns_revenue_rub: number | null;
  fns_net_profit_rub: number | null;
  fns_reporting_year: number | null;
}

export interface HourlyProfile {
  weekday_hourly_share: number[];
  weekend_hourly_share: number[];
  morning_share_pct: number;
  lunch_share_pct: number;
  evening_share_pct: number;
  night_share_pct: number;
  peak_weekday_hour: number;
  peak_weekend_hour: number;
  weekend_to_weekday_ratio: number;
  location_rhythm_type: string;
  cards_with_histograms: number;
}

export interface NearestMetroInfo {
  name?: string;
  station_name?: string;
  line?: string;
  distance_m?: number;
  daily_passengers?: number;
  [key: string]: unknown;
}

export interface FootTrafficReport {
  estimated_daily_pedestrians: number | null;
  estimated_weekday_daily: number | null;
  estimated_weekend_daily: number | null;
  estimated_avg_hourly_daytime: number | null;
  estimated_peak_hourly: number | null;
  traffic_index: number | null;
  traffic_level: string | null;
  metro_contribution_daily: number | null;
  stops_contribution_daily: number | null;
  local_commercial_daily: number | null;
  unique_stops_count: number | null;
  closest_stop_distance_m: number | null;
  transport_data_status: string | null;
  transport_lower_daily: number | null;
  transport_upper_daily: number | null;
  data_status: string | null;
  data_warnings: string[];
  nearest_metros: NearestMetroInfo[];
  hourly_profile: HourlyProfile | null;
}

export interface ResidentialBuildingItem {
  address?: string;
  name?: string;
  apartments?: number | null;
  floors?: number | null;
  year?: number | null;
  wall_material?: string | null;
  distance_m?: number | null;
  estimated_residents?: number | null;
  passport_exact?: boolean;
  [key: string]: unknown;
}

export interface PopulationReport {
  available: boolean;
  district_name: string | null;
  radius_m: number | null;
  residential_buildings_count: number | null;
  exact_passport_buildings_count: number | null;
  total_apartments_count: number | null;
  total_entrances_count: number | null;
  estimated_residents_count: number | null;
  estimated_residents_range: [number, number] | number[] | null;
  estimated_office_workers_count: number | null;
  office_buildings_count: number | null;
  avg_building_floors: number | null;
  avg_construction_year: number | null;
  predominant_wall_material: string | null;
  median_housing_price_sqm_rub: number | null;
  median_flat_price_mln_rub: number | null;
  data_status: string | null;
  data_warnings: string[];
  population_model: {
    usable: boolean;
    territories: Array<Record<string, unknown>>;
  };
  top_residential_buildings: ResidentialBuildingItem[];
}

export interface CompetitionReport {
  total_cards_found: number;
  fns_enriched_count: number;
  model_used: string | null;
  ai_summary: string | null;
  map_fetch_time_ms: number;
  ai_inference_time_ms: number;
  fns_fetch_time_ms: number;
  data_status: string;
  collection_status: string;
  data_warnings: string[];
  foot_traffic: FootTrafficReport | null;
  population: PopulationReport | null;
  direct_competitors: CompetitorRecord[];
  indirect_competitors: CompetitorRecord[];
  synergy_businesses: CompetitorRecord[];
}

export interface PropertyData {
  id: number | string;
  source: string;
  external_id: string | null;
  title: string | null;
  address: string | null;
  lat: number | null;
  lng: number | null;
  area_sqm: number | null;
  price_month: number | null;
  price_sqm_year: number | null;
  floor: number | string | null;
  url: string | null;
  description: string | null;
  has_separate_entrance: boolean | null;
  power_kw: number | null;
  photos?: string[];
  listing_variants?: Array<{
    source?: string;
    external_id?: string | null;
    id?: number | string;
    url?: string | null;
    address?: string | null;
    area_sqm?: number | null;
    price_month?: number | null;
  }>;
}

export interface FloorAssessment {
  status?: "approved" | "needs_verification" | "rejected" | string;
  reason?: string;
  missing_facts?: string[];
  [key: string]: unknown;
}

export interface RankedCandidate {
  rank: number;
  property_data: PropertyData;
  total_score: number;
  score_upper_bound: number;
  score_coverage: number;
  input_score_lower_bound: number | null;
  input_score_upper_bound: number | null;
  price_sqm_month: number | null;
  verified_fns_revenue_mln_sum: number | null;
  direct_competitors_avg_rating: number | null;
  competitor_comparison_summary: string | null;
  pros: string[];
  cons: string[];
  verdict: string | null;
  data_warnings: string[];
  floor_assessment: FloorAssessment | null;
  confirmation: ConfirmationData | null;
  scoring_components: ScoringComponent[];
  business_model_diagnostics: Record<string, unknown> | null;
  competition_report: CompetitionReport | null;
}

export interface DeferredCandidate {
  id: number | string;
  source: string;
  external_id: string | null;
  title: string | null;
  address: string | null;
  lat: number | null;
  lng: number | null;
  area_sqm: number | null;
  price_month: number | null;
  price_sqm_year?: number | null;
  price_sqm_month?: number | null;
  last_seen_at?: string | null;
  floor: number | string | null;
  url: string | null;
  description?: string | null;
  has_separate_entrance?: boolean | null;
  power_kw?: number | null;
  photos?: string[];
  reason: string;
  missing_facts: string[];
  bound: {
    property_id: number | string;
    score_lower_bound?: number;
    score_upper_bound?: number;
    can_beat_selected?: boolean;
    [key: string]: unknown;
  } | null;
}

export type SelectedMapPoint =
  | { kind: "preview"; listing: ListingPreview }
  | { kind: "ranked"; candidate: RankedCandidate }
  | { kind: "deferred"; candidate: DeferredCandidate }
  | {
      kind: "competitor";
      competitor: CompetitorRecord;
      groupLabel: string;
      groupColor: string;
    };

export interface RejectedCandidate {
  id: number | string;
  source: string;
  external_id?: string | null;
  title: string | null;
  address: string | null;
  lat: number | null;
  lng: number | null;
  area_sqm: number | null;
  price_month: number | null;
  price_sqm_year?: number | null;
  price_sqm_month?: number | null;
  last_seen_at?: string | null;
  floor: number | string | null;
  url: string | null;
  description?: string | null;
  has_separate_entrance?: boolean | null;
  power_kw?: number | null;
  photos?: string[];
  reason: string;
}

export type SelectedLeadTarget =
  | { kind: "preview"; listing: ListingPreview }
  | { kind: "deferred"; candidate: DeferredCandidate }
  | { kind: "rejected"; candidate: RejectedCandidate };

export interface SearchReport {
  job_id: string | null;
  city: CityKey;
  target_business: string;
  location_query: string;
  resolved_location_name: string | null;
  center_lat: number;
  center_lng: number;
  search_radius_m: number;
  competition_diameter_m: number;
  total_matching_in_db: number;
  numerical_matching_count: number;
  evaluated_count: number;
  total_search_time_ms: number;
  selection_status: string;
  confirmation_status: string;
  confirmation_message: string;
  comparison_complete: boolean;
  data_complete: boolean;
  include_unverified: boolean;
  unverified_evaluated_count: number;
  top_score_gap: number | null;
  stage_timings_ms: Record<string, number>;
  ranking_warnings: string[];
  missing_inputs: Array<Record<string, unknown>>;
  analysis_errors: Array<Record<string, unknown>>;
  recommendation:
    | string
    | {
        status?: string;
        message?: string;
        confirmation_status?: string;
        primary_candidate_id?: number | string | null;
        [key: string]: unknown;
      }
    | null;
  recommendation_details?: Record<string, unknown> | null;
  next_actions: Array<Record<string, unknown>>;
  ranking_profile: RankingProfile | null;
  requirement_coverage: RequirementCoverageItem[];
  floor_profile: FloorProfile | null;
  ranked_candidates: RankedCandidate[];
  floor_verification_candidates: DeferredCandidate[];
  floor_rejected_candidates: RejectedCandidate[];
}

export interface CollectResult {
  total_saved?: number;
  by_source?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface JobState<T = SearchReport | CollectResult> {
  job_id: string;
  type: "search" | "collect";
  city: CityKey;
  status: "running" | "completed" | "failed" | "cancelled";
  stage: string;
  logs: string[];
  result: T | null;
  error: string | null;
}
