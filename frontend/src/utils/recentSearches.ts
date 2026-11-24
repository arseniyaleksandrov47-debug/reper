import type { CityKey, SearchRequest } from "../types/geoestate";
import {
  extractAccessPreferenceFromConcept,
  extractEmphasisFromConcept,
  extractPrioritiesFromConcept,
  stripGeneratedConceptClauses,
  type AccessPreference,
  type OnboardingDraft
} from "./conceptParser";

const STORAGE_KEY = "reper.recent_searches.v1";
const MAX_RECENT_SEARCHES = 3;

export interface RecentSearchEntry {
  readonly id: string;
  readonly label: string;
  readonly freeText: string;
  readonly city: CityKey;
  readonly target_business: string;
  readonly location_query: string;
  readonly min_area_sqm: number | null;
  readonly max_area_sqm: number | null;
  readonly target_area_sqm: number | null;
  readonly max_price_month: number | null;
  readonly full_cost_limit: number | null;
  readonly allowed_floors: number[] | null;
  readonly floorInput: string;
  readonly accessPreference: AccessPreference;
  readonly business_concept: string;
  readonly priorities: string[];
  readonly emphasisText: string;
  readonly center_lat: number | null;
  readonly center_lng: number | null;
  readonly search_radius_m: number | null;
  readonly timestamp: number;
}

export function formatRecentSearchLabel(
  req: SearchRequest,
  floorInput: string
): string {
  const parts: string[] = [];
  parts.push(req.target_business.trim() || "Помещение");
  parts.push(req.city === "moscow" ? "МСК" : "СПб");

  if (req.center_lat != null && req.center_lng != null) {
    const r = req.search_radius_m ?? 1200;
    const rTxt = r >= 1000 ? `${(r / 1000).toFixed(r % 1000 === 0 ? 0 : 1)} км` : `${Math.round(r)} м`;
    parts.push(`${req.location_query?.trim() || "Точка"} (R ${rTxt})`);
  } else {
    parts.push(req.location_query?.trim() || "весь город");
  }

  if (req.min_area_sqm != null && req.max_area_sqm != null) {
    parts.push(`${req.min_area_sqm}–${req.max_area_sqm} м²`);
  } else if (req.max_area_sqm != null) {
    parts.push(`до ${req.max_area_sqm} м²`);
  } else if (req.min_area_sqm != null) {
    parts.push(`от ${req.min_area_sqm} м²`);
  }

  if (floorInput.trim()) {
    parts.push(`${floorInput.trim()} эт.`);
  }

  if (req.max_price_month != null) {
    const budget =
      req.max_price_month >= 10000
        ? `до ${Math.round(req.max_price_month / 1000)} тыс ₽`
        : `до ${Math.round(req.max_price_month).toLocaleString("ru-RU")} ₽`;
    parts.push(budget);
  }

  return parts.join(" · ");
}

function signatureForEntry(
  city: CityKey,
  targetBusiness: string,
  locationQuery: string,
  minArea: number | null,
  maxArea: number | null,
  maxPrice: number | null,
  floorInput: string,
  centerLat: number | null,
  centerLng: number | null,
  radiusM: number | null,
  concept: string
): string {
  return JSON.stringify([
    city,
    targetBusiness.trim().toLowerCase(),
    locationQuery.trim().toLowerCase(),
    minArea ?? null,
    maxArea ?? null,
    maxPrice ?? null,
    floorInput.trim().toLowerCase(),
    centerLat != null ? Number(centerLat.toFixed(4)) : null,
    centerLng != null ? Number(centerLng.toFixed(4)) : null,
    radiusM ?? null,
    concept.trim().toLowerCase()
  ]);
}

function isValidEntry(raw: unknown): raw is RecentSearchEntry {
  if (!raw || typeof raw !== "object") return false;
  const obj = raw as Record<string, unknown>;
  return (
    typeof obj.id === "string" &&
    typeof obj.label === "string" &&
    (obj.city === "saint_petersburg" || obj.city === "moscow") &&
    typeof obj.target_business === "string"
  );
}

export function loadRecentSearches(): RecentSearchEntry[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isValidEntry).slice(0, MAX_RECENT_SEARCHES);
  } catch {
    return [];
  }
}

function persistRecentSearches(entries: readonly RecentSearchEntry[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify(entries.slice(0, MAX_RECENT_SEARCHES))
    );
  } catch {
    // Ignore storage quota errors in sandboxed/private modes
  }
}

export function recordRecentSearch(
  req: SearchRequest,
  floorInput: string,
  freeTextOverride?: string
): RecentSearchEntry[] {
  const cleanConcept = stripGeneratedConceptClauses(req.business_concept || "");
  const freeText = (freeTextOverride ?? "").trim() || cleanConcept || formatRecentSearchLabel(req, floorInput);
  const label = formatRecentSearchLabel(req, floorInput);
  const priorities = extractPrioritiesFromConcept(req.business_concept || "");
  const emphasisText = extractEmphasisFromConcept(req.business_concept || "");
  const accessPreference = extractAccessPreferenceFromConcept(req.business_concept || "");

  const entry: RecentSearchEntry = {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    label,
    freeText,
    city: req.city,
    target_business: req.target_business,
    location_query: req.location_query || "весь город",
    min_area_sqm: req.min_area_sqm,
    max_area_sqm: req.max_area_sqm,
    target_area_sqm: req.target_area_sqm,
    max_price_month: req.max_price_month,
    full_cost_limit: req.full_cost_limit,
    allowed_floors: req.allowed_floors ? [...req.allowed_floors] : null,
    floorInput,
    accessPreference,
    business_concept: req.business_concept,
    priorities,
    emphasisText,
    center_lat: req.center_lat ?? null,
    center_lng: req.center_lng ?? null,
    search_radius_m: req.search_radius_m ?? null,
    timestamp: Date.now()
  };

  const nextSig = signatureForEntry(
    entry.city,
    entry.target_business,
    entry.location_query,
    entry.min_area_sqm,
    entry.max_area_sqm,
    entry.max_price_month,
    entry.floorInput,
    entry.center_lat,
    entry.center_lng,
    entry.search_radius_m,
    entry.business_concept
  );

  const existing = loadRecentSearches().filter(
    (item) =>
      signatureForEntry(
        item.city,
        item.target_business,
        item.location_query,
        item.min_area_sqm,
        item.max_area_sqm,
        item.max_price_month,
        item.floorInput,
        item.center_lat,
        item.center_lng,
        item.search_radius_m,
        item.business_concept
      ) !== nextSig
  );

  const nextList = [entry, ...existing].slice(0, MAX_RECENT_SEARCHES);
  persistRecentSearches(nextList);
  return nextList;
}

export function clearRecentSearches(): RecentSearchEntry[] {
  if (typeof window !== "undefined") {
    try {
      window.localStorage.removeItem(STORAGE_KEY);
    } catch {
      // Ignore storage errors
    }
  }
  return [];
}

export function draftFromRecentSearch(entry: RecentSearchEntry): OnboardingDraft {
  return {
    city: entry.city,
    businessText: stripGeneratedConceptClauses(entry.freeText || entry.business_concept),
    targetBusiness: entry.target_business,
    locationQuery: entry.location_query || "весь город",
    floorInput: entry.floorInput,
    accessPreference: entry.accessPreference,
    minAreaSqm: entry.min_area_sqm,
    maxAreaSqm: entry.max_area_sqm,
    maxPriceMonth: entry.max_price_month,
    fullCostLimit: entry.full_cost_limit,
    priorities: [...entry.priorities],
    emphasisText: entry.emphasisText
  };
}
