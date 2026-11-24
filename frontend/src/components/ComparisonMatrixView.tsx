import { useEffect, useMemo, useState } from "react";
import { ArrowDownUp, ArrowUpRight, ExternalLink, Trash2 } from "lucide-react";
import type {
  ListingPreview,
  SearchReport,
  SelectedLeadTarget
} from "../types/geoestate";
import {
  computePriceSqmMonth,
  formatFloorLabel,
  formatListingFreshness,
  formatNumber,
  formatRub,
  propertyExcludeKey,
  sourceDisplayName,
  trafficLevelLabel
} from "../utils/formatters";
import { ListingDetailPanel } from "./ListingDetailPanel";
import { PropertyDossier } from "./PropertyDossier";
import { ScoreCaliper } from "./ScoreCaliper";

type MatrixTab = "ranked" | "deferred" | "rejected";
type SortColumn = "rank" | "area" | "price" | "sqm" | "score";

export interface ComparisonMatrixViewProps {
  readonly cityName: string;
  readonly previewListings: readonly ListingPreview[];
  readonly report: SearchReport | null;
  readonly selectedRank: number | null;
  readonly onOpenInWorkbench: (rank: number) => void;
  readonly onFocusLeadOnMap: (lead: SelectedLeadTarget) => void;
  readonly onSearchAroundPoint: (lat: number, lng: number, label: string) => void;
  readonly onExcludeCandidate: (excludeKey: string) => void;
}

export function ComparisonMatrixView({
  cityName,
  previewListings,
  report,
  selectedRank,
  onOpenInWorkbench,
  onFocusLeadOnMap,
  onSearchAroundPoint,
  onExcludeCandidate
}: ComparisonMatrixViewProps) {
  const [subTab, setSubTab] = useState<MatrixTab>("ranked");
  const [sortCol, setSortCol] = useState<SortColumn>("rank");
  const [sortAsc, setSortAsc] = useState(true);

  const [inspectedPreviewIdx, setInspectedPreviewIdx] = useState<number | null>(null);
  const [previewCap, setPreviewCap] = useState(80);
  const [inspectedRankedRank, setInspectedRankedRank] = useState<number | null>(null);
  const [inspectedDeferredIdx, setInspectedDeferredIdx] = useState<number | null>(null);
  const [inspectedRejectedIdx, setInspectedRejectedIdx] = useState<number | null>(null);

  useEffect(() => {
    setInspectedPreviewIdx(null);
    setPreviewCap(80);
  }, [previewListings]);

  useEffect(() => {
    setInspectedRankedRank(null);
    setInspectedDeferredIdx(null);
    setInspectedRejectedIdx(null);
  }, [report?.job_id]);

  const handleSubTabChange = (next: MatrixTab) => {
    setSubTab(next);
    setInspectedRankedRank(null);
    setInspectedDeferredIdx(null);
    setInspectedRejectedIdx(null);
  };

  const toggleSort = (col: SortColumn) => {
    if (sortCol === col) {
      setSortAsc((v) => !v);
    } else {
      setSortCol(col);
      setSortAsc(col === "rank" || col === "price" || col === "sqm");
    }
  };

  const sortedRanked = useMemo(() => {
    if (!report) return [];
    const list = [...report.ranked_candidates];
    list.sort((a, b) => {
      let diff = 0;
      if (sortCol === "rank") diff = a.rank - b.rank;
      else if (sortCol === "area")
        diff = (a.property_data.area_sqm ?? 0) - (b.property_data.area_sqm ?? 0);
      else if (sortCol === "price")
        diff = (a.property_data.price_month ?? 0) - (b.property_data.price_month ?? 0);
      else if (sortCol === "sqm")
        diff = (a.price_sqm_month ?? 0) - (b.price_sqm_month ?? 0);
      else if (sortCol === "score") diff = a.total_score - b.total_score;
      return sortAsc ? diff : -diff;
    });
    return list;
  }, [report, sortCol, sortAsc]);

  if (!report) {
    const activePreview =
      inspectedPreviewIdx != null && previewListings[inspectedPreviewIdx]
        ? previewListings[inspectedPreviewIdx]
        : null;

    return (
      <div className="h-full flex bg-canvas overflow-hidden">
        <div className="flex-1 min-w-0 h-full flex flex-col overflow-hidden">
          <div className="shrink-0 px-5 py-3.5 bg-paper border-b border-ink-primary flex items-center justify-between gap-3">
            <div>
              <h1 className="text-[16px] font-bold text-ink-primary">
                Сравнительная матрица · Предпросмотр БД ({cityName})
              </h1>
              <p className="text-[12px] text-ink-secondary mt-0.5">
                Нажмите на любую строку для просмотра полного описания, фото и характеристик объекта.
              </p>
            </div>
            <span className="font-mono text-[12px] text-ink-muted tabular-nums shrink-0">
              Выборка: {previewListings.length} шт.
            </span>
          </div>

          <div className="flex-1 overflow-auto">
            <table className="w-full text-left border-collapse text-[12px]">
              <thead className="sticky top-0 bg-subtle border-b border-ink-primary font-mono text-[11px] uppercase tracking-wider text-ink-secondary z-10">
                <tr>
                  <th className={`py-2.5 ${activePreview ? "px-2.5" : "px-3"} border-r border-hairline`}>
                    Адрес / Название
                  </th>
                  <th className="py-2.5 px-2.5 border-r border-hairline">Источник</th>
                  <th className="py-2.5 px-2.5 border-r border-hairline text-right">
                    {activePreview ? "М² / Этаж" : "Площадь"}
                  </th>
                  {!activePreview && (
                    <th className="py-2.5 px-2.5 border-r border-hairline text-right">Этаж</th>
                  )}
                  <th className="py-2.5 px-2.5 border-r border-hairline text-right">Аренда ₽/мес</th>
                  <th className={`py-2.5 ${activePreview ? "px-2.5" : "px-3"}`}>Действия</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline bg-paper">
                {previewListings.slice(0, previewCap).map((item, idx) => {
                  const active = idx === inspectedPreviewIdx;
                  const sqmMonth = computePriceSqmMonth(item);
                  const fresh = formatListingFreshness(item.last_seen_at);
                  return (
                    <tr
                      key={`${item.id}-${idx}`}
                      onClick={() => setInspectedPreviewIdx(idx)}
                      className={`cursor-pointer transition-colors ${
                        active ? "bg-signal-tint/60" : "hover:bg-canvas"
                      }`}
                    >
                      <td className={`py-2.5 ${activePreview ? "px-2" : "px-3"} border-r border-hairline`}>
                        <div className="font-medium text-ink-primary leading-snug">
                          {item.address || item.title || "Помещение"}
                        </div>
                        {item.title && item.address && item.title !== item.address && (
                          <div
                            className={`text-[11px] text-ink-muted truncate ${
                              activePreview ? "max-w-[175px] min-[1360px]:max-w-[260px]" : "max-w-[360px]"
                            }`}
                          >
                            {item.title}
                          </div>
                        )}
                      </td>
                      <td
                        className={`py-2.5 ${
                          activePreview ? "px-2 whitespace-normal max-w-[100px]" : "px-2.5 whitespace-nowrap"
                        } border-r border-hairline font-mono text-ink-secondary`}
                      >
                        <div>{sourceDisplayName(item.source)}</div>
                        {fresh && (
                          <div
                            title={
                              fresh.fullDate
                                ? `Последний снимок в БД: ${fresh.fullDate}`
                                : undefined
                            }
                            className={`text-[11px] ${
                              fresh.isStale ? "text-warn font-medium" : "text-ink-muted"
                            }`}
                          >
                            сб. {fresh.label}
                          </div>
                        )}
                      </td>
                      <td className={`py-2.5 ${activePreview ? "px-2" : "px-2.5"} border-r border-hairline font-mono text-right tabular-nums whitespace-nowrap`}>
                        <div>{item.area_sqm != null ? `${item.area_sqm} м²` : "—"}</div>
                        {activePreview && (
                          <div className="text-[11px] text-ink-muted font-normal">
                            {formatFloorLabel(item.floor)}
                          </div>
                        )}
                      </td>
                      {!activePreview && (
                        <td className="py-2.5 px-2.5 border-r border-hairline font-mono text-right tabular-nums whitespace-nowrap">
                          {formatFloorLabel(item.floor)}
                        </td>
                      )}
                      <td className={`py-2.5 ${activePreview ? "px-2" : "px-2.5"} border-r border-hairline font-mono font-bold text-right tabular-nums whitespace-nowrap`}>
                        <div>{formatRub(item.price_month)}</div>
                        {sqmMonth != null && (
                          <div className="text-[11px] font-normal text-ink-muted">
                            {formatNumber(sqmMonth)} ₽/м²
                          </div>
                        )}
                      </td>
                      <td className={`py-2.5 ${activePreview ? "px-2" : "px-3"} whitespace-nowrap`}>
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={(ev) => {
                              ev.stopPropagation();
                              setInspectedPreviewIdx(idx);
                            }}
                            className={`h-7 px-2 border rounded-[2px] text-[11px] font-semibold inline-flex items-center gap-1 transition-colors ${
                              active
                                ? "bg-ink-primary text-[#f5f3ee] border-ink-primary"
                                : "border-hairline bg-canvas hover:border-ink-primary text-ink-primary"
                            }`}
                          >
                            <span>Карточка</span>
                            <ArrowUpRight className="w-3 h-3" />
                          </button>
                          {item.url && (
                            <a
                              href={item.url}
                              target="_blank"
                              rel="noreferrer"
                              onClick={(ev) => ev.stopPropagation()}
                              title="Открыть оригинальное объявление"
                              className="inline-flex items-center gap-1 text-signal hover:underline font-medium text-[11px]"
                            >
                              <ExternalLink className="w-3 h-3" />
                            </a>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {previewListings.length > previewCap && (
              <div className="py-3 text-center bg-canvas border-t border-hairline">
                <button
                  type="button"
                  onClick={() => setPreviewCap((c) => c + 80)}
                  className="h-8 px-4 border border-ink-primary bg-paper hover:bg-subtle text-[12px] font-semibold text-ink-primary rounded-[2px] transition-colors"
                >
                  Показать ещё {Math.min(80, previewListings.length - previewCap)} из{" "}
                  {formatNumber(previewListings.length)}
                </button>
              </div>
            )}
          </div>
        </div>

        {activePreview && inspectedPreviewIdx != null && (
          <aside
            aria-label="Карточка выбранного лида"
            className="w-[430px] min-[1360px]:w-[485px] 2xl:w-[530px] shrink-0 h-full border-l border-ink-primary bg-canvas overflow-hidden"
          >
            <ListingDetailPanel
              lead={{ kind: "preview", listing: activePreview }}
              backLabel="К таблице"
              positionLabel={`${inspectedPreviewIdx + 1} / ${previewListings.length}`}
              onClose={() => setInspectedPreviewIdx(null)}
              onPrev={
                inspectedPreviewIdx > 0
                  ? () => setInspectedPreviewIdx((i) => (i != null && i > 0 ? i - 1 : i))
                  : null
              }
              onNext={
                inspectedPreviewIdx < previewListings.length - 1
                  ? () =>
                      setInspectedPreviewIdx((i) =>
                        i != null && i < previewListings.length - 1 ? i + 1 : i
                      )
                  : null
              }
              onFocusOnMap={() =>
                onFocusLeadOnMap({ kind: "preview", listing: activePreview })
              }
              onSearchAroundPoint={onSearchAroundPoint}
            />
          </aside>
        )}
      </div>
    );
  }

  const activeRankedIdx =
    subTab === "ranked" && inspectedRankedRank != null
      ? sortedRanked.findIndex((c) => c.rank === inspectedRankedRank)
      : -1;
  const activeRanked = activeRankedIdx >= 0 ? sortedRanked[activeRankedIdx] : null;

  const activeDeferred =
    subTab === "deferred" && inspectedDeferredIdx != null
      ? report.floor_verification_candidates[inspectedDeferredIdx] ?? null
      : null;

  const activeRejected =
    subTab === "rejected" && inspectedRejectedIdx != null
      ? report.floor_rejected_candidates[inspectedRejectedIdx] ?? null
      : null;

  const hasDrawer = Boolean(activeRanked || activeDeferred || activeRejected);

  return (
    <div className="h-full flex bg-canvas overflow-hidden">
      <div className="flex-1 min-w-0 h-full flex flex-col overflow-hidden">
        {/* Header & Matrix Subtabs */}
        <div className="shrink-0 px-5 py-3 bg-paper border-b border-ink-primary flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-4">
            <h1 className="text-[16px] font-bold text-ink-primary tracking-tight">
              Таблица сравнения · {report.target_business}
            </h1>
            <div className="flex border border-ink-primary rounded-[2px] overflow-hidden text-[12px] font-semibold">
              <button
                type="button"
                onClick={() => handleSubTabChange("ranked")}
                className={`px-3 py-1 border-r border-ink-primary ${
                  subTab === "ranked"
                    ? "bg-ink-primary text-[#f5f3ee]"
                    : "bg-canvas text-ink-primary hover:bg-subtle"
                }`}
              >
                Рейтинг ({report.ranked_candidates.length})
              </button>
              <button
                type="button"
                onClick={() => handleSubTabChange("deferred")}
                className={`px-3 py-1 border-r border-ink-primary ${
                  subTab === "deferred"
                    ? "bg-ink-primary text-[#f5f3ee]"
                    : "bg-canvas text-ink-primary hover:bg-subtle"
                }`}
              >
                Допроверка ({report.floor_verification_candidates.length})
              </button>
              <button
                type="button"
                onClick={() => handleSubTabChange("rejected")}
                className={`px-3 py-1 ${
                  subTab === "rejected"
                    ? "bg-ink-primary text-[#f5f3ee]"
                    : "bg-canvas text-ink-primary hover:bg-subtle"
                }`}
              >
                Отклонены ({report.floor_rejected_candidates.length})
              </button>
            </div>
          </div>

          <span className="font-mono text-[12px] text-ink-muted tabular-nums">
            {hasDrawer
              ? "Клик по строке обновляет карточку справа"
              : `Проверено ${report.evaluated_count} из ${report.numerical_matching_count} · клик по строке открывает досье`}
          </span>
        </div>

        {/* Main Table Surface */}
        <div className="flex-1 overflow-auto">
          {subTab === "ranked" && (
            <table className="w-full text-left border-collapse text-[12px]">
              <thead className="sticky top-0 bg-subtle border-b border-ink-primary font-mono text-[11px] uppercase tracking-wider text-ink-secondary z-10">
                <tr>
                  <th className={`py-2.5 ${hasDrawer ? "px-1.5" : "px-2"} border-r border-hairline`}>
                    <button
                      type="button"
                      onClick={() => toggleSort("rank")}
                      className="inline-flex items-center gap-1 hover:text-ink-primary"
                    >
                      <span>#</span>
                      <ArrowDownUp className="w-3 h-3" />
                    </button>
                  </th>
                  <th
                    className={`py-2.5 ${
                      hasDrawer ? "px-1.5 min-w-[125px] max-w-[175px]" : "px-2.5 min-w-[170px]"
                    } border-r border-hairline`}
                  >
                    Адрес и источник
                  </th>
                  <th className={`py-2.5 ${hasDrawer ? "px-1.5" : "px-2"} border-r border-hairline text-right whitespace-nowrap`}>
                    <button
                      type="button"
                      onClick={() => toggleSort("area")}
                      className="inline-flex items-center gap-1 hover:text-ink-primary"
                    >
                      <span>{hasDrawer ? "М² / эт." : "Площадь"}</span>
                      <ArrowDownUp className="w-3 h-3" />
                    </button>
                  </th>
                  {!hasDrawer && (
                    <th className="py-2.5 px-2 border-r border-hairline text-right">Этаж</th>
                  )}
                  <th className={`py-2.5 ${hasDrawer ? "px-1.5" : "px-2"} border-r border-hairline text-right whitespace-nowrap`}>
                    <button
                      type="button"
                      onClick={() => toggleSort("price")}
                      className="inline-flex items-center gap-1 hover:text-ink-primary"
                    >
                      <span>{hasDrawer ? "Аренда" : "Аренда / мес"}</span>
                      <ArrowDownUp className="w-3 h-3" />
                    </button>
                  </th>
                  {!hasDrawer && (
                    <th className="py-2.5 px-2 border-r border-hairline text-right whitespace-nowrap">
                      <button
                        type="button"
                        onClick={() => toggleSort("sqm")}
                        className="inline-flex items-center gap-1 hover:text-ink-primary"
                      >
                        <span>₽ / м²</span>
                        <ArrowDownUp className="w-3 h-3" />
                      </button>
                    </th>
                  )}
                  <th
                    className={`py-2.5 ${
                      hasDrawer ? "px-1.5 min-w-[120px]" : "px-2.5 min-w-[165px]"
                    } border-r border-hairline`}
                  >
                    <button
                      type="button"
                      onClick={() => toggleSort("score")}
                      className="inline-flex items-center gap-1 hover:text-ink-primary"
                    >
                      <span>{hasDrawer ? "Оценка" : "Калибр оценки"}</span>
                      <ArrowDownUp className="w-3 h-3" />
                    </button>
                  </th>
                  {!hasDrawer && (
                    <th className="py-2.5 px-2 border-r border-hairline whitespace-nowrap">
                      Трафик / ФНС
                    </th>
                  )}
                  <th className={`py-2.5 ${hasDrawer ? "px-1.5" : "px-2"}`}>
                    {hasDrawer ? "Карта" : "Действия"}
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline bg-paper">
                {sortedRanked.map((c) => {
                  const p = c.property_data;
                  const inspecting = inspectedRankedRank === c.rank;
                  const highlighted = inspecting || (inspectedRankedRank == null && c.rank === selectedRank);
                  const sqm =
                    c.price_sqm_month ??
                    (p.price_month && p.area_sqm ? Math.round(p.price_month / p.area_sqm) : null);
                  const hasFnsMatch =
                    (c.competition_report?.fns_enriched_count ?? 0) > 0 &&
                    c.verified_fns_revenue_mln_sum != null;
                  const trafficTxt = trafficLevelLabel(
                    c.competition_report?.foot_traffic?.traffic_level
                  );
                  const fnsTxt = hasFnsMatch
                    ? `ФНС ${c.verified_fns_revenue_mln_sum!.toFixed(1)} млн ₽`
                    : "ФНС —";

                  return (
                    <tr
                      key={`${c.rank}-${propertyExcludeKey(p)}`}
                      onClick={() => setInspectedRankedRank(c.rank)}
                      className={`cursor-pointer transition-colors ${
                        highlighted ? "bg-signal-tint/55" : "hover:bg-canvas"
                      }`}
                    >
                      <td className={`py-2.5 ${hasDrawer ? "px-1.5" : "px-2"} border-r border-hairline font-mono font-bold`}>
                        <span className={highlighted ? "text-signal" : "text-ink-primary"}>
                          #{c.rank < 10 ? `0${c.rank}` : c.rank}
                        </span>
                      </td>
                      <td className={`py-2.5 ${hasDrawer ? "px-1.5 max-w-[175px]" : "px-2.5"} border-r border-hairline`}>
                        <div className="font-bold text-ink-primary leading-snug">
                          {p.address || p.title || "Помещение"}
                        </div>
                        <div className="font-mono text-[11px] text-ink-muted mt-0.5 leading-snug">
                          {sourceDisplayName(p.source)}
                          {c.competition_report?.foot_traffic?.nearest_metros?.[0]
                            ? ` · м. ${
                                c.competition_report.foot_traffic.nearest_metros[0].station_name ||
                                c.competition_report.foot_traffic.nearest_metros[0].name ||
                                "Метро"
                              } (${Math.round(
                                c.competition_report.foot_traffic.nearest_metros[0].distance_m ?? 0
                              )} м)`
                            : ""}
                          {hasDrawer ? ` · ${trafficTxt} · ${fnsTxt}` : ""}
                        </div>
                      </td>
                      <td className={`py-2.5 ${hasDrawer ? "px-1.5" : "px-2"} border-r border-hairline font-mono text-right tabular-nums whitespace-nowrap`}>
                        <div>{p.area_sqm != null ? `${p.area_sqm} м²` : "—"}</div>
                        {hasDrawer && (
                          <div className="text-[11px] text-ink-muted font-normal">
                            {formatFloorLabel(p.floor)}
                          </div>
                        )}
                      </td>
                      {!hasDrawer && (
                        <td className="py-2.5 px-2 border-r border-hairline font-mono text-right tabular-nums whitespace-nowrap">
                          {formatFloorLabel(p.floor)}
                        </td>
                      )}
                      <td className={`py-2.5 ${hasDrawer ? "px-1.5" : "px-2"} border-r border-hairline font-mono text-right tabular-nums whitespace-nowrap`}>
                        <div className="font-bold">{formatRub(p.price_month)}</div>
                        {hasDrawer && (
                          <div className="text-[11px] text-ink-muted font-normal">
                            {sqm ? `${formatNumber(sqm)} ₽/м²` : "—"}
                          </div>
                        )}
                      </td>
                      {!hasDrawer && (
                        <td className="py-2.5 px-2 border-r border-hairline font-mono text-right tabular-nums text-ink-secondary whitespace-nowrap">
                          {sqm ? `${formatNumber(sqm)} ₽` : "—"}
                        </td>
                      )}
                      <td className={`py-2.5 ${hasDrawer ? "px-1.5" : "px-2.5"} border-r border-hairline`}>
                        <ScoreCaliper
                          minScore={c.total_score}
                          maxScore={c.score_upper_bound}
                          coverage={c.score_coverage}
                          confirmation={c.confirmation}
                          selected={highlighted}
                          compact
                        />
                      </td>
                      {!hasDrawer && (
                        <td className="py-2.5 px-2 border-r border-hairline text-[11px] font-mono text-ink-secondary max-w-[115px]">
                          <div className="truncate" title={c.competition_report?.foot_traffic?.traffic_level || ""}>
                            {trafficTxt}
                          </div>
                          <div className={hasFnsMatch ? "text-ink-primary font-semibold" : "text-ink-muted"}>
                            {fnsTxt}
                          </div>
                        </td>
                      )}
                      <td className={`py-2.5 ${hasDrawer ? "px-1.5" : "px-2"} whitespace-nowrap`}>
                        <div className="flex items-center gap-1">
                          <button
                            type="button"
                            onClick={(ev) => {
                              ev.stopPropagation();
                              onOpenInWorkbench(c.rank);
                            }}
                            title="Открыть на карте"
                            className={`h-7 ${hasDrawer ? "px-1.5" : "px-2"} border border-ink-primary bg-paper hover:bg-ink-primary hover:text-[#f5f3ee] rounded-[2px] text-[11px] font-semibold inline-flex items-center gap-1 transition-colors`}
                          >
                            <span>{hasDrawer ? "Карта" : "На карте"}</span>
                            {!hasDrawer && <ArrowUpRight className="w-3 h-3" />}
                          </button>
                          <button
                            type="button"
                            onClick={(ev) => {
                              ev.stopPropagation();
                              onExcludeCandidate(propertyExcludeKey(p));
                            }}
                            title="Исключить лот"
                            aria-label={`Исключить лот #${c.rank}`}
                            className={`${hasDrawer ? "h-7 w-6" : "h-7 w-7"} border border-hairline hover:border-danger hover:text-danger rounded-[2px] flex items-center justify-center text-ink-muted`}
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}

          {subTab === "deferred" && (
            <table className="w-full text-left border-collapse text-[12px]">
              <thead className="sticky top-0 bg-subtle border-b border-ink-primary font-mono text-[11px] uppercase tracking-wider text-ink-secondary z-10">
                <tr>
                  <th className="py-2.5 px-4 border-r border-hairline">Объект</th>
                  <th className="py-2.5 px-3 border-r border-hairline text-right">Площадь / Цена</th>
                  <th className="py-2.5 px-3 border-r border-hairline">Потенциальный балл</th>
                  <th className="py-2.5 px-4">Причина переноса на уточнение</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline bg-paper">
                {report.floor_verification_candidates.map((def, i) => {
                  const active = i === inspectedDeferredIdx;
                  return (
                    <tr
                      key={i}
                      onClick={() => setInspectedDeferredIdx(i)}
                      className={`cursor-pointer transition-colors ${
                        active ? "bg-signal-tint/55" : "hover:bg-canvas"
                      }`}
                    >
                      <td className="py-2.5 px-4 border-r border-hairline">
                        <div className="font-semibold text-ink-primary">
                          {def.address || def.title}
                        </div>
                        <div className="flex items-center gap-2 mt-0.5 font-mono text-[11px] text-ink-muted">
                          <span>{sourceDisplayName(def.source)}</span>
                          {def.url && (
                            <a
                              href={def.url}
                              target="_blank"
                              rel="noreferrer"
                              onClick={(ev) => ev.stopPropagation()}
                              className="text-signal hover:underline"
                            >
                              Объявление ↗
                            </a>
                          )}
                        </div>
                      </td>
                      <td className="py-2.5 px-3 border-r border-hairline font-mono text-right tabular-nums whitespace-nowrap">
                        {def.area_sqm ?? "?"} м² · {formatRub(def.price_month)}
                      </td>
                      <td className="py-2.5 px-3 border-r border-hairline font-mono tabular-nums whitespace-nowrap">
                        {def.bound?.score_lower_bound != null && def.bound?.score_upper_bound != null
                          ? `${def.bound.score_lower_bound.toFixed(1)}–${def.bound.score_upper_bound.toFixed(
                              1
                            )}`
                          : "—"}
                      </td>
                      <td className="py-2.5 px-4 text-warn">{def.reason}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}

          {subTab === "rejected" && (
            <table className="w-full text-left border-collapse text-[12px]">
              <thead className="sticky top-0 bg-subtle border-b border-ink-primary font-mono text-[11px] uppercase tracking-wider text-ink-secondary z-10">
                <tr>
                  <th className="py-2.5 px-4 border-r border-hairline">Объект</th>
                  <th className="py-2.5 px-3 border-r border-hairline text-right">Площадь / Цена</th>
                  <th className="py-2.5 px-4">Причина отклонения</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline bg-paper">
                {report.floor_rejected_candidates.map((rej, i) => {
                  const active = i === inspectedRejectedIdx;
                  return (
                    <tr
                      key={i}
                      onClick={() => setInspectedRejectedIdx(i)}
                      className={`cursor-pointer transition-colors ${
                        active ? "bg-signal-tint/55" : "hover:bg-canvas"
                      }`}
                    >
                      <td className="py-2.5 px-4 border-r border-hairline">
                        <div className="font-medium text-ink-primary">
                          {rej.address || rej.title || "Помещение"}
                        </div>
                        <div className="flex items-center gap-2 mt-0.5 font-mono text-[11px] text-ink-muted">
                          <span>{sourceDisplayName(rej.source)}</span>
                          {rej.url && (
                            <a
                              href={rej.url}
                              target="_blank"
                              rel="noreferrer"
                              onClick={(ev) => ev.stopPropagation()}
                              className="text-signal hover:underline"
                            >
                              Объявление ↗
                            </a>
                          )}
                        </div>
                      </td>
                      <td className="py-2.5 px-3 border-r border-hairline font-mono text-right tabular-nums whitespace-nowrap">
                        {rej.area_sqm ?? "?"} м² · {formatRub(rej.price_month)}
                      </td>
                      <td className="py-2.5 px-4 text-danger">{rej.reason}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {activeRanked && (
        <aside
          aria-label="Досье выбранного кандидата"
          className="w-[430px] min-[1360px]:w-[485px] 2xl:w-[530px] shrink-0 h-full border-l border-ink-primary bg-canvas overflow-hidden"
        >
          <PropertyDossier
            candidate={activeRanked}
            totalRanked={sortedRanked.length}
            onBackToList={() => setInspectedRankedRank(null)}
            onPrev={
              activeRankedIdx > 0
                ? () => setInspectedRankedRank(sortedRanked[activeRankedIdx - 1].rank)
                : null
            }
            onNext={
              activeRankedIdx >= 0 && activeRankedIdx < sortedRanked.length - 1
                ? () => setInspectedRankedRank(sortedRanked[activeRankedIdx + 1].rank)
                : null
            }
            onExclude={onExcludeCandidate}
            onFocusCoord={() => onOpenInWorkbench(activeRanked.rank)}
            onSearchAroundPoint={onSearchAroundPoint}
          />
        </aside>
      )}

      {activeDeferred && inspectedDeferredIdx != null && (
        <aside
          aria-label="Карточка лота на допроверке"
          className="w-[430px] min-[1360px]:w-[485px] 2xl:w-[530px] shrink-0 h-full border-l border-ink-primary bg-canvas overflow-hidden"
        >
          <ListingDetailPanel
            lead={{ kind: "deferred", candidate: activeDeferred }}
            backLabel="К таблице"
            positionLabel={`${inspectedDeferredIdx + 1} / ${report.floor_verification_candidates.length}`}
            onClose={() => setInspectedDeferredIdx(null)}
            onPrev={
              inspectedDeferredIdx > 0
                ? () => setInspectedDeferredIdx((i) => (i != null && i > 0 ? i - 1 : i))
                : null
            }
            onNext={
              inspectedDeferredIdx < report.floor_verification_candidates.length - 1
                ? () =>
                    setInspectedDeferredIdx((i) =>
                      i != null && i < report.floor_verification_candidates.length - 1 ? i + 1 : i
                    )
                : null
            }
            onFocusOnMap={() =>
              onFocusLeadOnMap({ kind: "deferred", candidate: activeDeferred })
            }
            onSearchAroundPoint={onSearchAroundPoint}
          />
        </aside>
      )}

      {activeRejected && inspectedRejectedIdx != null && (
        <aside
          aria-label="Карточка отклонённого лота"
          className="w-[430px] min-[1360px]:w-[485px] 2xl:w-[530px] shrink-0 h-full border-l border-ink-primary bg-canvas overflow-hidden"
        >
          <ListingDetailPanel
            lead={{ kind: "rejected", candidate: activeRejected }}
            backLabel="К таблице"
            positionLabel={`${inspectedRejectedIdx + 1} / ${report.floor_rejected_candidates.length}`}
            onClose={() => setInspectedRejectedIdx(null)}
            onPrev={
              inspectedRejectedIdx > 0
                ? () => setInspectedRejectedIdx((i) => (i != null && i > 0 ? i - 1 : i))
                : null
            }
            onNext={
              inspectedRejectedIdx < report.floor_rejected_candidates.length - 1
                ? () =>
                    setInspectedRejectedIdx((i) =>
                      i != null && i < report.floor_rejected_candidates.length - 1 ? i + 1 : i
                    )
                : null
            }
            onFocusOnMap={() =>
              onFocusLeadOnMap({ kind: "rejected", candidate: activeRejected })
            }
            onSearchAroundPoint={onSearchAroundPoint}
          />
        </aside>
      )}
    </div>
  );
}
