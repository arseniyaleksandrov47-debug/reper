import type { CityKey, SearchRequest } from "../types/geoestate";

export type AccessPreference = "street" | "any" | "mall_bc";

export interface OnboardingDraft {
  readonly city: CityKey;
  readonly businessText: string;
  readonly targetBusiness: string;
  readonly locationQuery: string;
  readonly floorInput: string;
  readonly accessPreference: AccessPreference;
  readonly minAreaSqm: number | null;
  readonly maxAreaSqm: number | null;
  readonly maxPriceMonth: number | null;
  readonly fullCostLimit: number | null;
  readonly priorities: string[];
  readonly emphasisText: string;
}

export const FLOOR_PRESET_CHIPS: ReadonlyArray<{
  readonly label: string;
  readonly input: string;
  readonly floors: number[] | null;
}> = [
  { label: "Только 1-й", input: "1", floors: [1] },
  { label: "1–2 этаж", input: "1, 2", floors: [1, 2] },
  { label: "Цоколь / 1-й", input: "-1, 1", floors: [-1, 1] },
  { label: "Любой", input: "", floors: null }
];

export const ACCESS_OPTIONS: ReadonlyArray<{
  readonly key: AccessPreference;
  readonly label: string;
}> = [
  { key: "any", label: "Любой / не важно" },
  { key: "street", label: "С улицы (без пропуска)" },
  { key: "mall_bc", label: "Внутри ТЦ / БЦ" }
];

const BUSINESS_TITLE_PATTERNS: ReadonlyArray<readonly [RegExp, string]> = [
  [/пекарн|выпечк|хлеб|булоч/i, "Пекарня и кондитерская"],
  [/кофейн|кофе/i, "Кофейня"],
  [/пвз|пункт\s+выдачи|ozon|wildberries|wb|яндекс\s*маркет/i, "Пункт выдачи заказов (ПВЗ)"],
  [/салон\s+красоты|парикмахер|барбер|ногт|бьюти/i, "Салон красоты"],
  [/аптек/i, "Аптека"],
  [/грум/i, "Зоосалон / груминг"],
  [/цвет/i, "Цветочный магазин"],
  [/бар|ресторан|кафе|бистро|общепит/i, "Кафе / общепит"],
  [/магазин|ритейл|продукт|маркет/i, "Розничный магазин"],
  [/офис|студи|шоурум|агентств|клуб/i, "Офис / студия"]
];

const RETAIL_STREET_PATTERN =
  /пекарн|кофейн|пвз|пункт\s+выдачи|магазин|продукт|аптек|салон\s+красоты|барбер|цвет|бистро|кафе/i;

/** Parse free-form Russian floor expressions ("9", "9 этаж", "на 9-м этаже", "5-9", "-1, 1", "цоколь"). */
export function parseFloorsString(raw: string): number[] | null {
  const cleaned = raw.trim().toLowerCase();
  if (!cleaned || /^(?:любой(?:\s+этаж)?|не\s*важн\w*|все(?:\s+этажи)?)$/i.test(cleaned)) {
    return null;
  }

  const values = new Set<number>();

  if (/цокол\w*|подвал\w*/i.test(cleaned)) {
    values.add(-1);
  }

  // Normalize ordinal suffixes ("9-й", "9-м", "9-го", "9м") before Russian range phrases ("с 5 по 9")
  let normalized = cleaned
    .replace(/[–—]/g, "-")
    .replace(/(-?\d{1,3})\s*-\s*(?:й|м|го|е|ом|ем|ую|ая)(?=$|[\s,.;:!?)])/gi, "$1")
    .replace(/(-?\d{1,3})(?:й|м)(?=$|[\s,.;:!?)])/gi, "$1")
    .replace(/(^|[\s,;])с\s*(-?\d{1,3})\s+по\s+(\d{1,3})(?=$|[\s,.;:!?)])/gi, "$1$2-$3");

  // Extract explicit integer ranges first ("5-9")
  normalized = normalized.replace(/(^|[^\d])(-?\d{1,3})\s*-\s*(\d{1,3})(?=$|[^\d])/g, (m, prefix, aStr, bStr) => {
    const start = Number(aStr);
    const end = Number(bStr);
    if (
      Number.isInteger(start) &&
      Number.isInteger(end) &&
      start >= -3 &&
      end <= 100 &&
      end >= start &&
      end - start <= 25
    ) {
      for (let f = start; f <= end; f += 1) values.add(f);
      return `${prefix} `;
    }
    return m;
  });

  // Extract remaining standalone integers
  const intRegex = /(?:^|[^\d-])(-?\d{1,3})(?=$|[^\d])/g;
  let match: RegExpExecArray | null;
  while ((match = intRegex.exec(normalized)) !== null) {
    const num = Number(match[1]);
    if (Number.isInteger(num) && num >= -3 && num <= 100) {
      values.add(num);
    }
  }

  if (values.size === 0) return null;
  return Array.from(values).sort((a, b) => a - b);
}

/** Human-readable badge text for allowed_floors. */
export function formatFloorsSummary(floors: number[] | null | undefined): string {
  if (!floors || floors.length === 0) return "Любой этаж";
  if (floors.length === 1) {
    return floors[0] === -1 ? "Цоколь (-1)" : `${floors[0]}-й этаж`;
  }
  return (
    floors
      .map((f) => (f === -1 ? "цоколь (-1)" : `${f}-й`))
      .join(", ") + " эт."
  );
}

/** Check whether two floor arrays contain the same sorted floor set (or both null). */
export function sameFloorSet(a: number[] | null | undefined, b: number[] | null | undefined): boolean {
  const normA = a && a.length > 0 ? Array.from(new Set(a)).sort((x, y) => x - y) : null;
  const normB = b && b.length > 0 ? Array.from(new Set(b)).sort((x, y) => x - y) : null;
  if (normA === null || normB === null) return normA === normB;
  return normA.length === normB.length && normA.every((val, i) => val === normB[i]);
}

function extractCity(text: string, fallbackCity: CityKey): CityKey {
  if (/москв|мск|цао|басман|арбат|тверск|хамовник/i.test(text)) {
    return "moscow";
  }
  if (/петербург|спб|питер|василеостров|петроград|невск/i.test(text)) {
    return "saint_petersburg";
  }
  return fallbackCity;
}

function extractTargetBusiness(text: string): string {
  for (const [pattern, label] of BUSINESS_TITLE_PATTERNS) {
    if (pattern.test(text)) return label;
  }
  const firstClause = text.split(/[.,;!\n]/)[0].trim();
  if (!firstClause) return "Коммерческое помещение";
  return firstClause.length > 48 ? `${firstClause.slice(0, 45).trim()}…` : firstClause;
}

function extractLocation(text: string): string | null {
  const metroMatch = text.match(
    /(?:у\s+|возле\s+|рядом\s+с\s+)?(?:метро|м\.)\s*([А-ЯЁA-Za-z-]{3,}(?:\s+[А-ЯЁA-Za-z-]{3,})?)/i
  );
  if (metroMatch?.[1]) {
    return `метро ${metroMatch[1].trim()}`;
  }
  const districtMatch = text.match(/([А-ЯЁ][а-яё-]{3,}\s+район)/i);
  if (districtMatch?.[1]) {
    return districtMatch[1].trim();
  }
  return null;
}

function extractFloorInput(text: string): string {
  if (!text) return "";
  if (/любой\s+этаж|этаж\s+не\s+важен/i.test(text)) return "";

  const floorClauses: string[] = [];
  const clauseRegex =
    /(?:цокол\w*|подвал\w*|(?:с\s*-?\d{1,3}\s+по\s+\d{1,3}|-?\d{1,3}(?:\s*[-–—]\s*\d{1,3})?(?:\s*-\s*(?:й|м|го|е))?(?:\s*(?:,|и)\s*-?\d{1,3}(?:\s*-\s*(?:й|м|го|е))?)*)\s*этаж\w*|этаж\s*[:#№]?\s*-?\d{1,3}|на\s+-?\d{1,3}(?:\s*-\s*(?:м|ом))?\s+этаж\w*)/gi;

  let m: RegExpExecArray | null;
  while ((m = clauseRegex.exec(text)) !== null) {
    floorClauses.push(m[0]);
  }

  if (floorClauses.length > 0) {
    const parsed = parseFloorsString(floorClauses.join(", "));
    if (parsed && parsed.length > 0) {
      return parsed.join(", ");
    }
  }

  if (RETAIL_STREET_PATTERN.test(text)) {
    return "1";
  }
  return "";
}

function extractAreaRange(text: string): { min: number | null; max: number | null } {
  const range = text.match(/(\d{2,4})\s*[-–—]\s*(\d{2,4})\s*(?:м2|м²|кв)/i);
  if (range) {
    const a = Number(range[1]);
    const b = Number(range[2]);
    if (a > 0 && b >= a) return { min: a, max: b };
  }
  const fromTo = text.match(/от\s*(\d{2,4})\s*до\s*(\d{2,4})\s*(?:м2|м²|кв)/i);
  if (fromTo) {
    const a = Number(fromTo[1]);
    const b = Number(fromTo[2]);
    if (a > 0 && b >= a) return { min: a, max: b };
  }
  const single = text.match(/(?:около|примерно|площадь\s*)?(\d{2,4})\s*(?:м2|м²|кв\.?\s*м)/i);
  if (single) {
    const val = Number(single[1]);
    if (val >= 10) {
      return { min: Math.max(10, Math.round(val * 0.75)), max: Math.round(val * 1.25) };
    }
  }
  return { min: null, max: null };
}

function extractMaxRent(text: string): number | null {
  const m = text.match(
    /(?:до|бюджет|аренда|не\s+дороже)[^\d]{0,12}(\d+(?:[\s.,]\d+)*)\s*(тыс|т\.?\s*р|к\b|₽|руб)?/i
  );
  if (!m) return null;
  const rawNum = Number(m[1].replace(/\s+/g, "").replace(",", "."));
  if (!Number.isFinite(rawNum) || rawNum <= 0) return null;
  const suffix = (m[2] || "").toLowerCase();
  if (suffix.startsWith("тыс") || suffix.startsWith("т") || suffix.startsWith("к") || rawNum < 1500) {
    return Math.round(rawNum * 1000);
  }
  return Math.round(rawNum);
}

/** Extract initial refined draft fields from free-form business description. */
export function buildDraftFromBusinessText(
  base: SearchRequest,
  businessText: string,
  selectedCity: CityKey
): OnboardingDraft {
  const cleaned = businessText.trim();
  const city = extractCity(cleaned, selectedCity);
  const area = extractAreaRange(cleaned);
  const rent = extractMaxRent(cleaned);
  const loc = extractLocation(cleaned);
  const floorInput = cleaned
    ? extractFloorInput(cleaned)
    : base.allowed_floors?.join(", ") || "1";
  const parsedFloors = parseFloorsString(floorInput);

  const defaultLoc =
    city === base.city
      ? base.location_query || "весь город"
      : "весь город";

  const minArea = area.min ?? base.min_area_sqm;
  const maxArea = area.max ?? base.max_area_sqm;
  const maxRent = rent ?? base.max_price_month;

  let accessPreference: AccessPreference = "any";
  if (/внутри\s*(?:тц|бц|торгов|бизнес)/i.test(cleaned)) {
    accessPreference = "mall_bc";
  } else if (
    /уличн\w*\s+вход|с\s+улицы|отдельн\w*\s+вход|без\s+пропуск/i.test(cleaned) ||
    ((!parsedFloors || parsedFloors.includes(1)) && RETAIL_STREET_PATTERN.test(cleaned))
  ) {
    accessPreference = "street";
  }

  return {
    city,
    businessText: cleaned,
    targetBusiness: cleaned ? extractTargetBusiness(cleaned) : base.target_business,
    locationQuery: loc ?? defaultLoc,
    floorInput,
    accessPreference,
    minAreaSqm: minArea,
    maxAreaSqm: maxArea,
    maxPriceMonth: maxRent,
    fullCostLimit: maxRent ? Math.round(maxRent * 1.25) : base.full_cost_limit,
    priorities: [],
    emphasisText: ""
  };
}

const ACCENT_LINE_REGEX = /(?:^|\n)Главный\s+акцент(?:\s+клиента)?:\s?([^\n]*)/i;
const PRIORITIES_LINE_REGEX = /Приоритетные\s+критерии(?:\s+клиента)?:\s*([^\n.!?]+)(?:[.!?]|$)/i;

/** Extract saved custom priority characteristics from an assembled business_concept string. */
export function extractPrioritiesFromConcept(concept: string): string[] {
  const m = concept.match(PRIORITIES_LINE_REGEX);
  if (!m?.[1]) return [];
  return m[1]
    .split(/[,;]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

/** Extract access preference flag encoded in business_concept. */
export function extractAccessPreferenceFromConcept(concept: string): AccessPreference {
  if (/Рассматривается помещение внутри торгового или бизнес-центра/i.test(concept)) {
    return "mall_bc";
  }
  if (/Обязателен удобный уличный вход для посетителей без пропускной системы/i.test(concept)) {
    return "street";
  }
  return "any";
}

/** Remove previously appended structured footer clauses so editing in modal stays idempotent. */
export function stripGeneratedConceptClauses(concept: string): string {
  return concept
    .replace(/\n?Главный\s+акцент(?:\s+клиента)?:\s?[^\n]*/gi, "")
    .replace(/\s*Приоритетные\s+критерии(?:\s+клиента)?:\s*[^\n.!?]+[.]?/gi, "")
    .replace(/\s*Допустимые\s+этажи\s+клиента:\s*[^\n.!?]+[.]?/gi, "")
    .replace(/\s*Этаж\s+помещения:\s*любой[.]?/gi, "")
    .replace(/\s*Обязателен\s+удобный\s+уличный\s+вход\s+для\s+посетителей\s+без\s+пропускной\s+системы[.]?/gi, "")
    .replace(/\s*Рассматривается\s+помещение\s+внутри\s+торгового\s+или\s+бизнес-центра[.]?/gi, "")
    .trim();
}

/** Read the dedicated emphasis line embedded inside business_concept, if present. */
export function extractEmphasisFromConcept(concept: string): string {
  const m = concept.match(ACCENT_LINE_REGEX);
  return m?.[1] ?? "";
}

/** Update or remove the dedicated emphasis line inside business_concept while keeping user prose intact. */
export function mergeEmphasisIntoConcept(concept: string, emphasis: string): string {
  const singleLine = emphasis.replace(/[\r\n]+/g, " ");
  const baseBody = concept
    .replace(/\n?Главный\s+акцент(?:\s+клиента)?:\s?[^\n]*/gi, "")
    .trimEnd();
  if (!singleLine.trim()) {
    return baseBody;
  }
  const accentLine = `Главный акцент клиента: ${singleLine}`;
  return baseBody ? `${baseBody}\n${accentLine}` : accentLine;
}

/** Build the final SearchRequest from confirmed modal/wizard draft. */
export function applyDraftToRequest(
  base: SearchRequest,
  draft: OnboardingDraft,
  pendingCustomPriority = ""
): SearchRequest {
  const resolvedFloors = parseFloorsString(draft.floorInput);
  const cleanedBody = stripGeneratedConceptClauses(draft.businessText);

  const conceptParts: string[] = [];
  if (cleanedBody) {
    conceptParts.push(cleanedBody);
  } else {
    conceptParts.push(draft.targetBusiness.trim() || base.target_business);
  }

  if (resolvedFloors && resolvedFloors.length > 0) {
    conceptParts.push(`Допустимые этажи клиента: ${resolvedFloors.join(", ")}.`);
  } else if (!/этаж|цокол/i.test(conceptParts[0])) {
    conceptParts.push("Этаж помещения: любой.");
  }

  if (draft.accessPreference === "street" && !/вход|пропуск/i.test(conceptParts[0])) {
    conceptParts.push("Обязателен удобный уличный вход для посетителей без пропускной системы.");
  } else if (draft.accessPreference === "mall_bc" && !/внутри\s*(?:тц|бц)/i.test(conceptParts[0])) {
    conceptParts.push("Рассматривается помещение внутри торгового или бизнес-центра.");
  }

  const extraItems = pendingCustomPriority
    .split(/[,;]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  const allPriorities = Array.from(
    new Set([...draft.priorities.map((s) => s.trim()).filter(Boolean), ...extraItems])
  );
  if (allPriorities.length > 0) {
    conceptParts.push(`Приоритетные критерии клиента: ${allPriorities.join(", ")}.`);
  }

  let concept = conceptParts
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => (/[.!?;]$/.test(p) ? p : `${p}.`))
    .join(" ");
  if (draft.emphasisText.trim()) {
    concept = mergeEmphasisIntoConcept(concept, draft.emphasisText);
  }

  const targetArea =
    draft.minAreaSqm != null && draft.maxAreaSqm != null
      ? Math.round((draft.minAreaSqm + draft.maxAreaSqm) / 2)
      : base.target_area_sqm;

  const nextLoc = draft.locationQuery.trim() || "весь город";
  const locationChanged =
    draft.city !== base.city ||
    nextLoc.toLowerCase() !== (base.location_query ?? "").trim().toLowerCase();

  return {
    ...base,
    city: draft.city,
    target_business: draft.targetBusiness.trim() || base.target_business,
    location_query: nextLoc,
    min_area_sqm: draft.minAreaSqm,
    max_area_sqm: draft.maxAreaSqm,
    target_area_sqm: targetArea,
    max_price_month: draft.maxPriceMonth,
    full_cost_limit: draft.fullCostLimit,
    allowed_floors: resolvedFloors,
    business_concept: concept,
    exclude_property_keys: [],
    center_lat: locationChanged ? null : base.center_lat,
    center_lng: locationChanged ? null : base.center_lng,
    search_radius_m: locationChanged ? null : base.search_radius_m
  };
}
