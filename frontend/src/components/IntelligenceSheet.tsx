import { useEffect, useMemo, useState } from "react";
import { ArrowRight, RefreshCw } from "lucide-react";
import type {
  JobState,
  ListingPreview,
  RankedCandidate,
  SearchReport,
  SelectedLeadTarget
} from "../types/geoestate";
import {
  computePriceSqmMonth,
  formatFloorLabel,
  formatListingFreshness,
  formatNumber,
  formatRub,
  listingIdentityKey,
  pluralizeRu,
  propertyExcludeKey,
  sourceDisplayName
} from "../utils/formatters";
import { ListingDetailPanel } from "./ListingDetailPanel";
import { PropertyDossier } from "./PropertyDossier";
import { ScoreCaliper } from "./ScoreCaliper";

type SortKey = "rank" | "price_asc" | "sqm_asc";
type StatusFilter = "all" | "verified" | "verify";

export interface IntelligenceSheetProps {
  readonly cityName: string;
  readonly cityTotalCount?: number;
  readonly previewListings: readonly ListingPreview[];
  readonly previewTotal: number;
  readonly previewLoading: boolean;
  readonly report: SearchReport | null;
  readonly searchJob: JobState<SearchReport> | null;
  readonly elapsedSec: number;
  readonly selectedRank: number | null;
  readonly dossierOpen: boolean;
  readonly selectedLead: SelectedLeadTarget | null;
  readonly onSelectRank: (rank: number) => void;
  readonly onOpenDossier: (rank: number) => void;
  readonly onCloseDossier: () => void;
  readonly onSelectLead: (lead: SelectedLeadTarget) => void;
  readonly onCloseSelectedLead: () => void;
  readonly onExcludeCandidate: (excludeKey: string) => void;
  readonly onContinueJob: (
    jobId: string,
    payload: {
      action: "continue_unverified" | "find_more" | "supply_references";
      extra_excludes?: string[];
      full_cost_limit?: number | null;
    }
  ) => void;
  readonly onCancelSearch?: () => void;
  readonly onFocusCoord: (lat: number, lng: number, label: string) => void;
  readonly onSearchAroundPoint: (lat: number, lng: number, label?: string) => void;
  readonly onOpenLogsTab: () => void;
  readonly onOpenSources: () => void;
  readonly onRequestSupplyReferences: (jobId: string) => void;
}

export function IntelligenceSheet({
  cityName,
  cityTotalCount,
  previewListings,
  previewTotal,
  previewLoading,
  report,
  searchJob,
  elapsedSec,
  selectedRank,
  dossierOpen,
  selectedLead,
  onSelectRank,
  onOpenDossier,
  onCloseDossier,
  onSelectLead,
  onCloseSelectedLead,
  onExcludeCandidate,
  onContinueJob,
  onCancelSearch,
  onFocusCoord,
  onSearchAroundPoint,
  onOpenLogsTab,
  onOpenSources,
  onRequestSupplyReferences
}: IntelligenceSheetProps) {
  const [sortBy, setSortBy] = useState<SortKey>("rank");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [previewFilter, setPreviewFilter] = useState("");
  const [previewCap, setPreviewCap] = useState(80);
  const [showDeferredSection, setShowDeferredSection] = useState(false);

  useEffect(() => {
    setPreviewCap(80);
  }, [previewListings, previewFilter]);

  const isRunning = searchJob?.status === "running";

  const activeDossierCandidate: RankedCandidate | null = useMemo(() => {
    if (!dossierOpen || !report) return null;
    return (
      report.ranked_candidates.find((c) => c.rank === selectedRank) ??
      report.ranked_candidates[0] ??
      null
    );
  }, [dossierOpen, report, selectedRank]);

  const sortedCandidates = useMemo(() => {
    if (!report) return [];
    const base = report.ranked_candidates.filter((c) => {
      const verified = c.confirmation?.status === "source_conditions_checked";
      if (statusFilter === "verified") return verified;
      if (statusFilter === "verify") return !verified;
      return true;
    });
    if (sortBy === "price_asc") {
      return [...base].sort(
        (a, b) =>
          (a.property_data.price_month ?? 9e9) - (b.property_data.price_month ?? 9e9)
      );
    }
    if (sortBy === "sqm_asc") {
      return [...base].sort(
        (a, b) => (a.price_sqm_month ?? 9e9) - (b.price_sqm_month ?? 9e9)
      );
    }
    return base;
  }, [report, sortBy, statusFilter]);

  const filteredPreview = useMemo(() => {
    const q = previewFilter.trim().toLowerCase();
    if (!q) return previewListings;
    return previewListings.filter((item) =>
      `${item.address ?? ""} ${item.title ?? ""}`.toLowerCase().includes(q)
    );
  }, [previewListings, previewFilter]);

  if (activeDossierCandidate && report) {
    const idx = report.ranked_candidates.findIndex(
      (c) => c.rank === activeDossierCandidate.rank
    );
    const prevCand = idx > 0 ? report.ranked_candidates[idx - 1] : null;
    const nextCand =
      idx >= 0 && idx < report.ranked_candidates.length - 1
        ? report.ranked_candidates[idx + 1]
        : null;

    return (
      <section
        aria-label="Досье выбранного помещения"
        className="w-[430px] min-[1360px]:w-[485px] 2xl:w-[530px] shrink-0 h-full border-l border-ink-primary bg-canvas overflow-hidden"
      >
        <PropertyDossier
          candidate={activeDossierCandidate}
          totalRanked={report.ranked_candidates.length}
          onBackToList={onCloseDossier}
          onPrev={prevCand ? () => onOpenDossier(prevCand.rank) : null}
          onNext={nextCand ? () => onOpenDossier(nextCand.rank) : null}
          onExclude={onExcludeCandidate}
          onFocusCoord={onFocusCoord}
          onSearchAroundPoint={(lat, lng, label) => onSearchAroundPoint(lat, lng, label)}
        />
      </section>
    );
  }

  if (selectedLead) {
    let prevLead: SelectedLeadTarget | null = null;
    let nextLead: SelectedLeadTarget | null = null;
    let posLabel: string | null = null;

    if (selectedLead.kind === "preview") {
      const targetKey = listingIdentityKey(selectedLead.listing);
      const list = filteredPreview.some((it) => listingIdentityKey(it) === targetKey)
        ? filteredPreview
        : previewListings;
      const idx = list.findIndex((it) => listingIdentityKey(it) === targetKey);
      if (idx >= 0) {
        posLabel = `${idx + 1} / ${list.length}`;
        if (idx > 0) prevLead = { kind: "preview", listing: list[idx - 1] };
        if (idx < list.length - 1) nextLead = { kind: "preview", listing: list[idx + 1] };
      }
    } else if (selectedLead.kind === "deferred" && report) {
      const targetKey = listingIdentityKey(selectedLead.candidate);
      const list = report.floor_verification_candidates;
      const idx = list.findIndex((it) => listingIdentityKey(it) === targetKey);
      if (idx >= 0) {
        posLabel = `${idx + 1} / ${list.length}`;
        if (idx > 0) prevLead = { kind: "deferred", candidate: list[idx - 1] };
        if (idx < list.length - 1) nextLead = { kind: "deferred", candidate: list[idx + 1] };
      }
    }

    return (
      <section
        aria-label="Карточка выбранного помещения"
        className="w-[430px] min-[1360px]:w-[485px] 2xl:w-[530px] shrink-0 h-full border-l border-ink-primary bg-canvas overflow-hidden"
      >
        <ListingDetailPanel
          lead={selectedLead}
          backLabel="Список"
          positionLabel={posLabel}
          onClose={onCloseSelectedLead}
          onPrev={prevLead ? () => onSelectLead(prevLead!) : null}
          onNext={nextLead ? () => onSelectLead(nextLead!) : null}
          onFocusOnMap={(lat, lng, label) => onFocusCoord(lat, lng, label)}
          onSearchAroundPoint={(lat, lng, label) => onSearchAroundPoint(lat, lng, label)}
        />
      </section>
    );
  }

  return (
    <section
      aria-label="Список и калибровка помещений"
      className="w-[430px] min-[1360px]:w-[485px] 2xl:w-[530px] shrink-0 h-full border-l border-ink-primary bg-canvas flex flex-col overflow-hidden"
    >
      {isRunning && searchJob && (
        <div className="shrink-0 bg-[#181b1f] text-[#f5f3ee] border-b border-ink-primary px-4 py-3 space-y-1.5">
          <div className="flex items-center justify-between gap-2 text-[12px]">
            <span className="font-mono font-semibold inline-flex items-center gap-2 min-w-0">
              <span className="w-2 h-2 rounded-full bg-signal animate-pulse shrink-0" />
              <span className="truncate">
                {searchJob.stage || "Выполняем геоаналитический расчёт..."}
              </span>
            </span>
            <div className="flex items-center gap-2 shrink-0">
              <span className="font-mono tabular-nums text-[#adb4bf]">
                {elapsedSec} с
              </span>
              {onCancelSearch && (
                <button
                  type="button"
                  onClick={onCancelSearch}
                  className="px-2 py-0.5 rounded-[2px] border border-[#565f6d] text-[11px] font-medium text-[#f5f3ee] hover:border-signal hover:bg-signal transition-colors"
                >
                  Остановить
                </button>
              )}
            </div>
          </div>
          {searchJob.logs.length > 0 && (
            <div className="flex items-center justify-between gap-2 text-[11px] font-mono text-[#adb4bf]">
              <span className="truncate">{searchJob.logs[searchJob.logs.length - 1]}</span>
              <button
                type="button"
                onClick={onOpenLogsTab}
                className="text-[#f5f3ee] underline shrink-0 hover:text-signal"
              >
                Журнал →
              </button>
            </div>
          )}
        </div>
      )}

      {searchJob?.status === "cancelled" && (
        <div className="shrink-0 bg-hatch-warn border-b border-warn/60 px-4 py-2 flex items-center justify-between gap-2 text-[12px]">
          <span className="font-medium text-ink-primary">
            Подбор остановлен пользователем
          </span>
          <button
            type="button"
            onClick={onOpenLogsTab}
            className="font-mono text-[11px] text-ink-primary underline hover:text-signal shrink-0"
          >
            Журнал →
          </button>
        </div>
      )}

      {report ? (
        <>
          <div className="shrink-0 px-4 py-3 bg-paper border-b border-ink-primary space-y-2.5">
            <div className="flex items-baseline justify-between gap-2">
              <h1 className="text-[16px] font-bold text-ink-primary tracking-tight">
                Ранжирование · {report.ranked_candidates.length}{" "}
                {pluralizeRu(report.ranked_candidates.length, ["лот", "лота", "лотов"])}
              </h1>
              <span className="font-mono text-[12px] text-ink-muted tabular-nums">
                из {report.numerical_matching_count} по фильтру
              </span>
            </div>

            <div className="text-[12px] text-ink-secondary leading-snug">
              <strong className="text-ink-primary">{report.target_business}</strong> ·{" "}
              {report.resolved_location_name || report.location_query} ·{" "}
              <span className="font-mono tabular-nums">
                {report.search_radius_m >= 15000
                  ? "по всему городу"
                  : report.search_radius_m >= 1000
                    ? `радиус ${(report.search_radius_m / 1000).toFixed(report.search_radius_m % 1000 === 0 ? 0 : 1)} км`
                    : `радиус ${Math.round(report.search_radius_m)} м`}
              </span>
            </div>

            {report.next_actions.length > 0 && report.job_id && (
              <div className="bg-hatch-warn border border-warn/60 rounded-[2px] p-2.5 space-y-2">
                <p className="text-[12px] font-medium text-ink-primary leading-snug">
                  {report.confirmation_message}
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {report.next_actions.map((act, i) => {
                    const actId = String(act.action ?? "");
                    const label = String(act.label ?? actId);
                    if (
                      actId !== "continue_unverified" &&
                      actId !== "find_more" &&
                      actId !== "supply_references"
                    ) {
                      return null;
                    }
                    return (
                      <button
                        key={i}
                        type="button"
                        onClick={() => {
                          if (actId === "supply_references") {
                            onRequestSupplyReferences(report.job_id!);
                            return;
                          }
                          onContinueJob(report.job_id!, {
                            action: actId,
                            extra_excludes: report.ranked_candidates.map((c) =>
                              propertyExcludeKey(c.property_data)
                            )
                          });
                        }}
                        className="px-2.5 py-1 bg-ink-primary hover:bg-signal text-[#f5f3ee] text-[11px] font-semibold rounded-[2px] transition-colors"
                      >
                        {label}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            <div className="flex items-center justify-between gap-2 pt-1 border-t border-hairline">
              <div className="flex border border-hairline rounded-[2px] overflow-hidden text-[11px]">
                {[
                  { id: "all" as const, label: "Все" },
                  { id: "verified" as const, label: "Допуск ок" },
                  { id: "verify" as const, label: "Уточнить" }
                ].map((f) => (
                  <button
                    key={f.id}
                    type="button"
                    onClick={() => setStatusFilter(f.id)}
                    className={`px-2 py-1 font-medium border-r last:border-r-0 border-hairline ${
                      statusFilter === f.id
                        ? "bg-ink-primary text-[#f5f3ee]"
                        : "bg-canvas text-ink-secondary hover:bg-subtle"
                    }`}
                  >
                    {f.label}
                  </button>
                ))}
              </div>

              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value as SortKey)}
                aria-label="Сортировка кандидатов"
                className="h-7 px-2 py-0 leading-[26px] text-[11px] font-medium bg-canvas border border-hairline rounded-[2px]"
              >
                <option value="rank">По рейтингу</option>
                <option value="price_asc">Дешевле в мес.</option>
                <option value="sqm_asc">Ниже ставка ₽/м²</option>
              </select>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto divide-y divide-hairline">
            {sortedCandidates.length === 0 ? (
              <div className="p-6 text-center text-[13px] text-ink-secondary space-y-2">
                <p>Нет помещений в текущем фильтре статуса.</p>
                {statusFilter !== "all" && (
                  <button
                    type="button"
                    onClick={() => setStatusFilter("all")}
                    className="text-signal font-semibold hover:underline"
                  >
                    Показать все кандидаты ({report.ranked_candidates.length})
                  </button>
                )}
              </div>
            ) : (
              sortedCandidates.map((c) => {
                const p = c.property_data;
                const selected = c.rank === selectedRank;
                const priceSqm =
                  c.price_sqm_month ??
                  (p.price_month && p.area_sqm ? Math.round(p.price_month / p.area_sqm) : null);
                const metro = c.competition_report?.foot_traffic?.nearest_metros?.[0];
                const metroName = metro ? metro.station_name || metro.name : null;

                return (
                  <article
                    key={`${c.rank}-${propertyExcludeKey(p)}`}
                    onClick={() => onSelectRank(c.rank)}
                    className={`px-4 py-3 cursor-pointer transition-colors relative ${
                      selected
                        ? "bg-paper border-l-[4px] border-l-signal pl-3"
                        : "bg-canvas hover:bg-paper"
                    }`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex items-baseline gap-2">
                          <span
                            className={`font-mono text-[12px] font-bold shrink-0 ${
                              selected ? "text-signal" : "text-ink-primary"
                            }`}
                          >
                            #{c.rank < 10 ? `0${c.rank}` : c.rank}
                          </span>
                          <h2 className="text-[14px] font-bold text-ink-primary leading-snug">
                            {p.address || p.title || "Помещение"}
                          </h2>
                        </div>
                        <div className="mt-1 text-[12px] font-mono text-ink-secondary tabular-nums">
                          {p.area_sqm ?? "?"} м² · {formatFloorLabel(p.floor)} ·{" "}
                          {sourceDisplayName(p.source)}
                          {metroName ? ` · М. ${metroName}` : ""}
                        </div>
                      </div>

                      <div className="text-right shrink-0 font-mono tabular-nums">
                        <div className="text-[14px] font-bold text-ink-primary">
                          {formatRub(p.price_month)}
                        </div>
                        {priceSqm && (
                          <div className="text-[11px] text-ink-muted">
                            {formatNumber(priceSqm)} ₽/м²
                          </div>
                        )}
                      </div>
                    </div>

                    <div className="mt-2.5 flex items-end justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <ScoreCaliper
                          minScore={c.total_score}
                          maxScore={c.score_upper_bound}
                          coverage={c.score_coverage}
                          confirmation={c.confirmation}
                          selected={selected}
                          compact
                        />
                      </div>

                      <button
                        type="button"
                        onClick={(ev) => {
                          ev.stopPropagation();
                          onOpenDossier(c.rank);
                        }}
                        className="shrink-0 h-7 px-2.5 border border-ink-primary bg-paper hover:bg-ink-primary hover:text-[#f5f3ee] rounded-[2px] text-[11px] font-semibold inline-flex items-center gap-1 transition-colors"
                      >
                        <span>Досье</span>
                        <ArrowRight className="w-3 h-3" />
                      </button>
                    </div>
                  </article>
                );
              })
            )}

            {report.floor_verification_candidates.length > 0 && (
              <div className="bg-subtle/60 border-t border-hairline">
                <button
                  type="button"
                  onClick={() => setShowDeferredSection((v) => !v)}
                  className="w-full px-4 py-2.5 text-left flex items-center justify-between text-[12px] font-semibold text-warn hover:bg-subtle"
                >
                  <span>
                    Требуют проверки доступа ({report.floor_verification_candidates.length})
                  </span>
                  <span className="font-mono text-[11px]">
                    {showDeferredSection ? "Скрыть" : "Показать"}
                  </span>
                </button>
                {showDeferredSection && (
                  <div className="divide-y divide-hairline bg-canvas">
                    {report.floor_verification_candidates.map((def, idx) => (
                      <div
                        key={idx}
                        onClick={() => onSelectLead({ kind: "deferred", candidate: def })}
                        className="px-4 py-2.5 text-[12px] hover:bg-paper cursor-pointer space-y-1"
                      >
                        <div className="flex justify-between gap-2 font-medium text-ink-primary">
                          <span>{def.address || def.title}</span>
                          <span className="font-mono tabular-nums shrink-0">
                            {formatRub(def.price_month)}
                          </span>
                        </div>
                        <p className="text-[11px] text-warn">{def.reason}</p>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        </>
      ) : (
        <>
          <div className="shrink-0 px-4 py-3.5 bg-paper border-b border-ink-primary space-y-2.5">
            <div className="flex items-baseline justify-between gap-2">
              <h1 className="text-[16px] font-bold text-ink-primary tracking-tight">
                {cityName} · Предпросмотр базы
              </h1>
              <span className="font-mono text-[12px] font-bold text-ink-primary tabular-nums whitespace-nowrap">
                {formatNumber(previewTotal)}{" "}
                {cityTotalCount && previewTotal < cityTotalCount
                  ? `из ${formatNumber(cityTotalCount)}`
                  : "лотов"}
              </span>
            </div>
            <p className="text-[12px] text-ink-secondary leading-snug">
              Уточните нишу, площадь и бюджет в верхней ленте (или выберите точку на карте) и нажмите{" "}
              <strong className="text-signal">«Подобрать»</strong> для ранжирования с оценкой ФНС, конкурентов и трафика.
            </p>
            <div className="flex items-center gap-2 pt-1">
              <input
                type="search"
                value={previewFilter}
                onChange={(e) => setPreviewFilter(e.target.value)}
                placeholder="Быстрый фильтр по адресу в базе..."
                className="flex-1 h-8 px-2.5 text-[12px] bg-canvas border border-hairline focus:border-ink-primary rounded-[2px]"
              />
              <button
                type="button"
                onClick={onOpenSources}
                title="Обновить или догрузить локальную базу объявлений"
                className="h-8 px-2.5 border border-ink-primary bg-canvas hover:bg-subtle text-[11px] font-semibold rounded-[2px] inline-flex items-center gap-1"
              >
                <RefreshCw className="w-3 h-3" />
                <span>База</span>
              </button>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto divide-y divide-hairline">
            {previewLoading ? (
              <div className="p-6 text-center text-[13px] text-ink-secondary">
                Загружаем объекты локальной базы {cityName}…
              </div>
            ) : filteredPreview.length === 0 ? (
              <div className="p-6 text-center space-y-2 text-[13px] text-ink-secondary">
                <p>
                  {previewTotal === 0
                    ? cityTotalCount === 0
                      ? "Локальная база города пока пуста."
                      : "По текущим фильтрам подходящих помещений не найдено. Попробуйте расширить площадь, этажи, бюджет или зону."
                    : "По запросу ничего не найдено среди отфильтрованных помещений."}
                </p>
                {previewTotal === 0 && cityTotalCount === 0 && (
                  <button
                    type="button"
                    onClick={onOpenSources}
                    className="h-8 px-3 bg-signal text-[#fffdf9] text-[12px] font-semibold rounded-[2px]"
                  >
                    Собрать объявления в Базе
                  </button>
                )}
              </div>
            ) : (
              <>
                {filteredPreview.slice(0, previewCap).map((item, idx) => {
                  const sqmMonth = computePriceSqmMonth(item);
                  const fresh = formatListingFreshness(item.last_seen_at);
                  return (
                    <div
                      key={`${item.id}-${idx}`}
                      onClick={() => onSelectLead({ kind: "preview", listing: item })}
                      className="px-4 py-2.5 bg-canvas hover:bg-paper cursor-pointer flex items-center justify-between gap-3 transition-colors"
                    >
                      <div className="min-w-0">
                        <div className="text-[13px] font-semibold text-ink-primary truncate">
                          {item.address || item.title || "Коммерческое помещение"}
                        </div>
                        <div className="flex items-center gap-1 text-[12px] font-mono text-ink-muted tabular-nums mt-0.5 min-w-0">
                          <span className="truncate">
                            {item.area_sqm ?? "?"} м² · {formatFloorLabel(item.floor)} ·{" "}
                            {sourceDisplayName(item.source)}
                          </span>
                          {fresh && (
                            <span
                              title={
                                fresh.fullDate
                                  ? `Последний снимок в БД: ${fresh.fullDate}`
                                  : undefined
                              }
                              className={`shrink-0 whitespace-nowrap ${
                                fresh.isStale ? "text-warn font-medium" : ""
                              }`}
                            >
                              · {fresh.label}
                            </span>
                          )}
                        </div>
                      </div>

                      <div className="flex items-center gap-2 shrink-0">
                        <div className="text-right tabular-nums leading-tight">
                          <span className="font-mono text-[13px] font-bold text-ink-primary block">
                            {formatRub(item.price_month)}
                          </span>
                          {sqmMonth != null && (
                            <span className="font-mono text-[11px] text-ink-muted block">
                              {formatNumber(sqmMonth)} ₽/м²
                            </span>
                          )}
                        </div>
                        {item.lat != null && item.lng != null && (
                          <button
                            type="button"
                            onClick={(ev) => {
                              ev.stopPropagation();
                              onSearchAroundPoint(
                                item.lat!,
                                item.lng!,
                                item.address || item.title || "Точка"
                              );
                            }}
                            title="Подобрать вокруг этого объекта"
                            className="px-2 py-1 border border-hairline hover:border-signal text-[11px] font-medium text-ink-secondary hover:text-signal rounded-[2px]"
                          >
                            Вокруг
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
                {filteredPreview.length > previewCap && (
                  <div className="p-3 text-center bg-canvas">
                    <button
                      type="button"
                      onClick={() => setPreviewCap((c) => c + 80)}
                      className="h-8 px-4 border border-ink-primary bg-paper hover:bg-subtle text-[12px] font-semibold text-ink-primary rounded-[2px] transition-colors"
                    >
                      Показать ещё {Math.min(80, filteredPreview.length - previewCap)} из{" "}
                      {formatNumber(filteredPreview.length)}
                    </button>
                  </div>
                )}
              </>
            )}
          </div>
        </>
      )}
    </section>
  );
}
