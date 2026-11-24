import type { ConfirmationData, PropertyData, RankedCandidate } from "../types/geoestate";

/** Format RUB monthly / general integer values cleanly with ru-RU thin spaces. */
export function formatRub(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "н/д";
  return `${Math.round(value).toLocaleString("ru-RU")} ₽`;
}

export function formatNumber(value: number | null | undefined, digits = 0): string {
  if (value == null || !Number.isFinite(value)) return "н/д";
  return value.toLocaleString("ru-RU", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });
}

export function formatRange(
  low: number | null | undefined,
  high: number | null | undefined,
  digits = 1,
  unit = ""
): string {
  if (low == null || !Number.isFinite(low)) return "н/д";
  const suffix = unit ? ` ${unit}` : "";
  if (high == null || !Number.isFinite(high) || Math.abs(high - low) < 1e-6) {
    return `${formatNumber(low, digits)}${suffix}`;
  }
  return `${formatNumber(low, digits)}–${formatNumber(high, digits)}${suffix}`;
}

export function formatScoreInterval(cand: Pick<RankedCandidate, "total_score" | "score_upper_bound">): string {
  return formatRange(cand.total_score, cand.score_upper_bound, 1);
}

/** Canonical exclusion key compatible with real_estate_engine.property_sources. */
export function propertyExcludeKey(prop: Pick<PropertyData, "source" | "external_id" | "id">): string {
  if (prop.source && prop.external_id) {
    return `${prop.source}:${prop.external_id}`;
  }
  return String(prop.id);
}

export function listingIdentityKey(
  prop: Pick<PropertyData, "source" | "id" | "address" | "title" | "lat" | "lng"> & {
    external_id?: string | null;
  }
): string {
  if (prop.source && prop.external_id) {
    return `${prop.source}:${prop.external_id}`;
  }
  if (prop.id != null && prop.id !== "") {
    return `${prop.source || "db"}:${prop.id}`;
  }
  return `${prop.lat ?? ""}:${prop.lng ?? ""}:${prop.address || prop.title || ""}`;
}

export function sourceDisplayName(source: string | null | undefined): string {
  const s = (source || "").toLowerCase();
  const map: Record<string, string> = {
    cian: "ЦИАН",
    avito: "Авито",
    yandex: "Яндекс Недвижимость",
    sber: "Sber Aренда",
    domclick: "Домклик",
    torgi: "ГИС Торги",
    gis_torgi: "ГИС Торги"
  };
  return map[s] || (source ? source.toUpperCase() : "БД");
}

export function confirmationBadgeStyle(conf: ConfirmationData | null | undefined): {
  tone: "emerald" | "amber" | "crimson";
  label: string;
} {
  const st = conf?.status;
  if (st === "source_conditions_checked") {
    return {
      tone: "emerald",
      label: conf?.label || "Проверено по источникам"
    };
  }
  if (st === "not_suitable") {
    return {
      tone: "crimson",
      label: conf?.label || "Не соответствует"
    };
  }
  return {
    tone: "amber",
    label: conf?.label || "Требует подтверждения"
  };
}

export function formatFloorLabel(floor: number | string | null | undefined): string {
  if (floor == null || floor === "") return "Этаж н/д";
  if (typeof floor === "number") {
    if (floor < 0) return `Цоколь (${floor})`;
    return `${floor} этаж`;
  }
  return String(floor);
}


/**
 * Normalizes potentially structured backend values (e.g. recommendation objects)
 * into a safe primitive string so React JSX never throws error #31.
 */
export function safeDisplayText(value: unknown, fallback = ""): string {
  if (value == null) return fallback;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    for (const key of ["message", "label", "reason", "title", "status"]) {
      const candidate = record[key];
      if (typeof candidate === "string" && candidate.trim()) {
        return candidate;
      }
    }
  }
  return fallback;
}

export function checkResultDisplay(result: unknown, groupKey?: string): string {
  const raw = safeDisplayText(result, "unknown").trim();
  const norm = raw.toLowerCase();
  if (norm === "pass") return "Пройдено";
  if (norm === "not_applicable") {
    return groupKey === "client_requirements"
      ? "Особых ограничений не задано"
      : "Не требуется";
  }
  if (norm === "fail") return "Не пройдено";
  if (norm === "unknown" || !raw) return "Требует проверки";
  return raw;
}

export function fnsMatchStatusLabel(status: string | null | undefined): string {
  const raw = (status || "").trim();
  const norm = raw.toLowerCase();
  if (!raw) return "Не сопоставлено";
  if (norm === "verified" || norm === "matched" || norm === "exact") return "Совпадение ФНС";
  if (norm === "unverified" || norm === "pending") return "Не подтверждено";
  if (norm === "not_found" || norm === "miss" || norm === "none") return "Не найдено в ФНС";
  if (norm === "unavailable" || norm === "error") return "ФНС недоступна";
  if (/[а-яё]/i.test(raw)) return raw;
  return "Не сопоставлено";
}

export function trafficLevelLabel(level: string | null | undefined): string {
  const raw = (level || "").trim();
  const norm = raw.toLowerCase();
  if (!raw) return "Нет замера";
  if (norm === "high" || /^высок/i.test(raw)) return "Высокий трафик";
  if (norm === "medium" || norm === "moderate" || /^средн/i.test(raw)) return "Средний трафик";
  if (norm === "low" || /^низк/i.test(raw)) return "Низкий трафик";
  if (norm === "unknown" || norm === "unavailable" || /^нет\b|^н\/д/i.test(raw)) return "Нет замера";
  if (/замера\s+у\s+входа\s+нет/i.test(raw)) return "Прокси-оценка";
  if (/[а-яё]/i.test(raw)) return raw;
  return raw;
}

export function formatCompetitionSummary(
  summary: string | null | undefined,
  counts: { readonly direct: number; readonly indirect: number; readonly synergy: number }
): string | null {
  const raw = (summary || "").trim();
  if (!raw) return null;
  const looksLikeInternalDebug =
    /\b(Classified|organizations?|idx\s*\d|same_purchase_offer|relationship_basis|need_relation|unrelated|match\s+')/i.test(
      raw
    ) || !/[а-яё]/i.test(raw);
  const contradictsDirectCount =
    counts.direct > 0 && /отсутствие конкурентов не подтверждает/i.test(raw);
  if (looksLikeInternalDebug || contradictsDirectCount) {
    return `Окружение (D = 400 м): прямых конкурентов — ${counts.direct}, косвенных — ${counts.indirect}, синергия — ${counts.synergy}.`;
  }
  return raw;
}

/** Correct Russian pluralization: pluralizeRu(21, ["лот", "лота", "лотов"]) -> "21 лот". */
export function pluralizeRu(
  count: number,
  forms: readonly [string, string, string]
): string {
  const n = Math.abs(Math.trunc(count)) % 100;
  const n1 = n % 10;
  if (n > 10 && n < 20) return forms[2];
  if (n1 > 1 && n1 < 5) return forms[1];
  if (n1 === 1) return forms[0];
  return forms[2];
}

/** Clean Python raw float noise ("5.5014476097653695–24.433998527331486", "16.5–16.5", "190000") in prose bullets. */
export function formatHumanBullet(raw: string): string {
  let text = raw.replace(/\b(\d+)\.(\d{3,})\b/g, (_m, a, b) => {
    const val = Number(`${a}.${b}`);
    return Number.isFinite(val) ? val.toFixed(1).replace(".", ",") : _m;
  });
  text = text.replace(/\b(\d+)\.00(?=\s*(?:₽|м²|руб|%|\b))/g, "$1");
  text = text.replace(
    /\b(\d+(?:[.,]\d+)?)\s*[–—-]\s*\1\b/g,
    "$1"
  );
  text = text.replace(
    /(Площадь(?:\s+помещения)?:\s*)(\d+(?:[.,]\d+)?)(?!\s*(?:м²|кв\.?\s*м))/gi,
    "$1$2 м²"
  );
  text = text.replace(
    /((?:Аренда|Стоимость|Бюджет|Расходы|Ставка)[^:\n]{0,80}:\s*)(\d[\d\s]*\d)(?!\s*(?:₽|руб|тыс|млн|м²|%))/gi,
    (_m, prefix: string, numStr: string) => {
      const parsed = Number(numStr.replace(/\s+/g, ""));
      if (!Number.isFinite(parsed)) return _m;
      return `${prefix}${Math.round(parsed).toLocaleString("ru-RU")} ₽`;
    }
  );
  text = text
    .replace(/\bкофе\/(?:бе|be)verage\b/gi, "кофе и напитки")
    .replace(/\bberevage\b|\bbeverage\b/gi, "напитки")
    .replace(/\bexplicit offer\b/gi, "явное предложение");
  return text;
}

/** Strip internal English LLM debug/classifier strings while keeping real Russian analytical notes. */
export function sanitizeAiSummary(
  aiSummary: string | null | undefined,
  counts: { readonly direct: number; readonly indirect: number; readonly synergy: number }
): string | null {
  const raw = (aiSummary || "").trim();
  if (!raw) return null;
  const isInternalDebug =
    /\b(Classified\s+\d+\s+orgs?|same_purchase(?:_offer)?|yes-same_purchase|no\/unrelated|idx\s*\d|relationship_basis)\b/i.test(
      raw
    ) || !/[а-яё]/i.test(raw);
  if (isInternalDebug) {
    return formatCompetitionSummary(null, counts);
  }
  return formatHumanBullet(raw);
}

/** Monthly ruble rate per 1 m² computed from monthly price or annual rate fallback. */
export function computePriceSqmMonth(item: {
  readonly price_sqm_month?: number | null;
  readonly price_month?: number | null;
  readonly area_sqm?: number | null;
  readonly price_sqm_year?: number | null;
}): number | null {
  if (
    item.price_sqm_month != null &&
    Number.isFinite(item.price_sqm_month) &&
    item.price_sqm_month > 0
  ) {
    return Math.round(item.price_sqm_month);
  }
  if (
    item.price_month != null &&
    item.area_sqm != null &&
    Number.isFinite(item.price_month) &&
    Number.isFinite(item.area_sqm) &&
    item.area_sqm > 0 &&
    item.price_month > 0
  ) {
    return Math.round(item.price_month / item.area_sqm);
  }
  if (
    item.price_sqm_year != null &&
    Number.isFinite(item.price_sqm_year) &&
    item.price_sqm_year > 0
  ) {
    return Math.round(item.price_sqm_year / 12);
  }
  return null;
}

export interface ListingFreshnessInfo {
  readonly label: string;
  readonly fullDate: string | null;
  readonly isStale: boolean;
}

/** Compact relative age of the last DB snapshot ("сегодня", "4 дн.", flagging > 14 days). */
export function formatListingFreshness(
  lastSeenAt: string | null | undefined
): ListingFreshnessInfo | null {
  const raw = (lastSeenAt || "").trim();
  if (!raw) return null;
  const normalized = /^\d{4}-\d{2}-\d{2}$/.test(raw)
    ? `${raw}T00:00:00Z`
    : raw.includes(" ") && !raw.includes("T")
      ? `${raw.replace(" ", "T")}${/(?:Z|[+-]\d{2}:?\d{2})$/.test(raw) ? "" : "Z"}`
      : raw;
  const parsed = new Date(normalized);
  if (!Number.isFinite(parsed.getTime())) return null;
  const diffDays = Math.max(0, Math.floor((Date.now() - parsed.getTime()) / 86400000));
  const fullDate = parsed.toLocaleDateString("ru-RU");
  if (diffDays === 0) return { label: "сегодня", fullDate, isStale: false };
  if (diffDays === 1) return { label: "1 дн.", fullDate, isStale: false };
  return {
    label: `${diffDays} дн.`,
    fullDate,
    isStale: diffDays > 14
  };
}
