import type {
  CompetitionReport,
  CompetitorRecord,
  ListingPreview,
  RankedCandidate,
  SearchReport
} from "../types/geoestate";
import { formatRub, formatScoreInterval } from "./formatters";

export function escapeHtml(raw: string | null | undefined): string {
  if (!raw) return "";
  return raw
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function pinStyleByTone(tone: "emerald" | "amber" | "crimson", isSelected: boolean): string {
  if (isSelected) {
    return "background:#d13b27;color:#fffdf9;border:2px solid #181b1f;box-shadow:0 0 0 3px rgba(209,59,39,0.28);";
  }
  const bg = tone === "emerald" ? "#166534" : tone === "crimson" ? "#991b1b" : "#9a3412";
  return `background:#181b1f;color:#f5f3ee;border:2px solid ${bg};`;
}

export function buildRankPinHtml(
  rank: number,
  tone: "emerald" | "amber" | "crimson",
  isSelected: boolean
): string {
  const w = isSelected ? 34 : 28;
  const h = isSelected ? 26 : 22;
  return `<div style="min-width:${w}px;height:${h}px;padding:0 5px;border-radius:2px;display:flex;align-items:center;justify-content:center;font-family:'JetBrains Mono',monospace;font-weight:700;font-size:${isSelected ? 12 : 11}px;cursor:pointer;${pinStyleByTone(tone, isSelected)}">#${rank < 10 ? `0${rank}` : rank}</div>`;
}

export function buildDeferredPinHtml(isSelected = false): string {
  if (isSelected) {
    return `<div style="width:24px;height:24px;border-radius:2px;border:2px solid #181b1f;background:#fcf4ed;color:#9a3412;display:flex;align-items:center;justify-content:center;font-family:'JetBrains Mono',monospace;font-weight:700;font-size:11px;cursor:pointer;box-shadow:0 0 0 3px rgba(154,52,18,0.25)">?</div>`;
  }
  return `<div style="width:20px;height:20px;border-radius:2px;border:1.5px dashed #9a3412;background:#fffdf9;color:#9a3412;display:flex;align-items:center;justify-content:center;font-family:'JetBrains Mono',monospace;font-weight:700;font-size:10px;cursor:pointer">?</div>`;
}

export function buildSearchAnchorPinHtml(): string {
  return `<div style="width:22px;height:22px;border:2px solid #d13b27;background:rgba(255,253,249,0.92);border-radius:2px;display:flex;align-items:center;justify-content:center;box-shadow:0 0 0 3px rgba(209,59,39,0.22)"><span style="width:6px;height:6px;background:#d13b27;display:block"></span></div>`;
}

export function previewTooltipHtml(item: ListingPreview): string {
  const title = escapeHtml(item.address || item.title || "Помещение");
  return `<div style="line-height:1.35"><strong>${title}</strong><br/><span style="font-family:'JetBrains Mono',monospace">${item.area_sqm ?? "?"} м² · ${escapeHtml(formatRub(item.price_month))}</span></div>`;
}

export function deferredTooltipHtml(def: {
  address?: string | null;
  title?: string | null;
  reason?: string | null;
}): string {
  const title = escapeHtml(def.address || def.title || "");
  const reason = escapeHtml(def.reason || "");
  return `<div style="line-height:1.35"><strong>Требует уточнения</strong><br/>${title}<br/><span style="color:#9a3412">${reason}</span></div>`;
}

export function competitorTooltipHtml(comp: CompetitorRecord, groupLabel: string): string {
  const title = escapeHtml(comp.title);
  const label = escapeHtml(groupLabel);
  const rating = comp.rating ? ` · ★ ${comp.rating}` : "";
  const revenue = comp.fns_revenue_rub
    ? ` · ФНС ${(comp.fns_revenue_rub / 1e6).toFixed(1)} млн ₽`
    : "";
  return `<div style="line-height:1.35"><strong>${title}</strong> (${label})<br/><span style="font-family:'JetBrains Mono',monospace">${comp.distance_m} м${rating}${revenue}</span></div>`;
}

export function candidateTooltipHtml(cand: RankedCandidate): string {
  const p = cand.property_data;
  const addr = escapeHtml(p.address || p.title);
  return `<div style="line-height:1.35"><strong>#${cand.rank}. ${addr}</strong><br/><span style="font-family:'JetBrains Mono',monospace">${escapeHtml(formatScoreInterval(cand))} / 100 · ${p.area_sqm ?? "?"} м² · ${escapeHtml(formatRub(p.price_month))}</span></div>`;
}

export function extractCompetitorGroups(
  compReport: CompetitionReport
): Array<{ items: CompetitorRecord[]; color: string; label: string }> {
  return [
    {
      items: compReport.synergy_businesses,
      color: "#166534",
      label: "Синергия"
    },
    {
      items: compReport.indirect_competitors,
      color: "#b45309",
      label: "Косвенный конкурент"
    },
    {
      items: compReport.direct_competitors,
      color: "#d13b27",
      label: "Прямой конкурент"
    }
  ];
}

export function collectReportLatLngs(report: SearchReport): Array<[number, number]> {
  const pts: Array<[number, number]> = [[report.center_lat, report.center_lng]];
  for (const c of report.ranked_candidates) {
    if (c.property_data.lat != null && c.property_data.lng != null) {
      pts.push([c.property_data.lat, c.property_data.lng]);
    }
  }
  return pts;
}
