import { useEffect, useState } from "react";
import {
  ArrowLeft,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  MapPin,
  Trash2,
  X
} from "lucide-react";
import type { CompetitorRecord, RankedCandidate } from "../types/geoestate";
import {
  checkResultDisplay,
  fnsMatchStatusLabel,
  formatCompetitionSummary,
  formatFloorLabel,
  formatHumanBullet,
  formatNumber,
  formatRange,
  formatRub,
  propertyExcludeKey,
  sanitizeAiSummary,
  sourceDisplayName,
  trafficLevelLabel
} from "../utils/formatters";
import { ScoreCaliper } from "./ScoreCaliper";

const CHECK_GROUP_LABELS: Record<string, string> = {
  availability: "Доступность лота для аренды",
  concept_use: "Соответствие формату и концепции",
  access: "Доступ сотрудников и покупателей",
  client_requirements: "Дополнительные требования клиента",
  access_and_floor: "Этаж и условия доступа",
  floor: "Допустимость этажа",
  additional_concept_requirements: "Дополнительные требования концепции",
  publication_status: "Активность публикации",
  identity: "Верификация объекта"
};

export interface PropertyDossierProps {
  readonly candidate: RankedCandidate;
  readonly totalRanked: number;
  readonly onBackToList: () => void;
  readonly onPrev: (() => void) | null;
  readonly onNext: (() => void) | null;
  readonly onExclude: (excludeKey: string) => void;
  readonly onFocusCoord: (lat: number, lng: number, label: string) => void;
  readonly onSearchAroundPoint: (lat: number, lng: number, label: string) => void;
}

export function PropertyDossier({
  candidate,
  totalRanked,
  onBackToList,
  onPrev,
  onNext,
  onExclude,
  onFocusCoord,
  onSearchAroundPoint
}: PropertyDossierProps) {
  const [lightboxIdx, setLightboxIdx] = useState<number | null>(null);
  const [section, setSection] = useState<"overview" | "env" | "fns">("overview");

  const p = candidate.property_data;
  const comp = candidate.competition_report;
  const photos = p.photos ?? [];
  const pricePerSqmMonth =
    candidate.price_sqm_month ??
    (p.price_month && p.area_sqm ? Math.round(p.price_month / p.area_sqm) : null);

  useEffect(() => {
    setLightboxIdx(null);
  }, [candidate.rank]);

  useEffect(() => {
    if (lightboxIdx === null) return;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") setLightboxIdx(null);
      if (ev.key === "ArrowLeft" && photos.length > 0) {
        setLightboxIdx((i) => ((i ?? 0) - 1 + photos.length) % photos.length);
      }
      if (ev.key === "ArrowRight" && photos.length > 0) {
        setLightboxIdx((i) => ((i ?? 0) + 1) % photos.length);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lightboxIdx, photos.length]);

  const hourlyWeekday = comp?.foot_traffic?.hourly_profile?.weekday_hourly_share ?? [];
  const peakHourlyVal = hourlyWeekday.reduce((acc, v) => Math.max(acc, Number(v || 0)), 0.01);

  const fnsRecords: Array<{
    comp: CompetitorRecord;
    kind: string;
  }> = comp
    ? [
        ...comp.direct_competitors.map((c) => ({ comp: c, kind: "Прямой" })),
        ...comp.indirect_competitors.map((c) => ({ comp: c, kind: "Косвенный" })),
        ...comp.synergy_businesses.map((c) => ({ comp: c, kind: "Синергия" }))
      ].filter((r) => Boolean(r.comp.fns_match_status || r.comp.fns_revenue_rub))
    : [];

  const nearestMetro = comp?.foot_traffic?.nearest_metros?.[0];
  const metroLabel = nearestMetro
    ? nearestMetro.station_name || nearestMetro.name || "Метро"
    : null;

  const checkEntries = Object.entries(candidate.confirmation?.checks ?? {});
  const reasonMessages = (candidate.confirmation?.reasons ?? [])
    .map((r) => r.message || r.admission_details?.reason)
    .filter((m): m is string => Boolean(m));

  const compSummaryText = comp
    ? formatCompetitionSummary(candidate.competitor_comparison_summary, {
        direct: comp.direct_competitors.length,
        indirect: comp.indirect_competitors.length,
        synergy: comp.synergy_businesses.length
      })
    : null;

  const cleanAiSummary = comp
    ? sanitizeAiSummary(comp.ai_summary, {
        direct: comp.direct_competitors.length,
        indirect: comp.indirect_competitors.length,
        synergy: comp.synergy_businesses.length
      })
    : null;

  return (
    <div className="h-full flex flex-col bg-canvas">
      {/* Top Sticky Dossier Bar */}
      <div className="shrink-0 h-[44px] px-3 bg-paper border-b border-ink-primary flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <button
            type="button"
            onClick={onBackToList}
            className="h-7 px-2 border border-hairline bg-canvas hover:border-ink-primary rounded-[2px] text-[12px] font-semibold inline-flex items-center gap-1 text-ink-primary"
          >
            <ArrowLeft className="w-3.5 h-3.5" />
            <span>Список ({totalRanked})</span>
          </button>

          <div className="flex border border-hairline rounded-[2px] overflow-hidden">
            <button
              type="button"
              disabled={!onPrev}
              onClick={onPrev ?? undefined}
              title="Предыдущий лот"
              aria-label="Предыдущий лот"
              className="h-7 w-7 bg-canvas hover:bg-subtle disabled:opacity-35 flex items-center justify-center border-r border-hairline"
            >
              <ChevronUp className="w-3.5 h-3.5" />
            </button>
            <button
              type="button"
              disabled={!onNext}
              onClick={onNext ?? undefined}
              title="Следующий лот"
              aria-label="Следующий лот"
              className="h-7 w-7 bg-canvas hover:bg-subtle disabled:opacity-35 flex items-center justify-center"
            >
              <ChevronDown className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          {p.lat != null && p.lng != null && (
            <button
              type="button"
              onClick={() =>
                onSearchAroundPoint(p.lat!, p.lng!, p.address || p.title || "Адрес")
              }
              title="Перезапустить подбор вокруг этого помещения"
              className="h-7 px-2 border border-hairline bg-canvas hover:border-ink-primary rounded-[2px] text-[12px] font-medium inline-flex items-center gap-1"
            >
              <MapPin className="w-3 h-3 text-signal" />
              <span>Вокруг</span>
            </button>
          )}

          {p.url && (
            <a
              href={p.url}
              target="_blank"
              rel="noreferrer"
              className="h-7 px-2 border border-hairline bg-canvas hover:border-ink-primary rounded-[2px] text-[12px] font-medium inline-flex items-center gap-1"
            >
              <ExternalLink className="w-3 h-3" />
              <span>{sourceDisplayName(p.source)}</span>
            </a>
          )}

          <button
            type="button"
            onClick={() => onExclude(propertyExcludeKey(p))}
            title="Исключить помещение из выборки и пересчитать рейтинг"
            className="h-7 px-2 border border-danger/40 bg-[#fef1f1] hover:bg-danger hover:text-white text-danger rounded-[2px] text-[12px] font-semibold inline-flex items-center gap-1 transition-colors"
          >
            <Trash2 className="w-3 h-3" />
            <span>Исключить</span>
          </button>
        </div>
      </div>

      {/* Sub-Section Tabs */}
      <div className="shrink-0 grid grid-cols-3 border-b border-hairline bg-subtle text-[12px] font-semibold">
        {[
          { id: "overview" as const, label: "1. Оценка и фото" },
          { id: "env" as const, label: "2. Среда и трафик" },
          { id: "fns" as const, label: "3. ФНС и допуск" }
        ].map((tab) => {
          const active = section === tab.id;
          return (
            <button
              key={tab.id}
              type="button"
              onClick={() => setSection(tab.id)}
              className={`py-2 px-2 text-center border-r last:border-r-0 border-hairline transition-colors ${
                active
                  ? "bg-paper text-ink-primary border-b-2 border-b-signal"
                  : "text-ink-secondary hover:bg-canvas"
              }`}
            >
              {tab.label}
            </button>
          );
        })}
      </div>

      {/* Scrollable Body */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        <section className="bg-paper border border-ink-primary rounded-[2px] p-3.5 space-y-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2 font-mono text-[12px]">
                <span className="px-1.5 py-0.5 bg-signal text-[#fffdf9] font-bold rounded-[2px]">
                  #{candidate.rank < 10 ? `0${candidate.rank}` : candidate.rank}
                </span>
                <span className="text-ink-muted">{sourceDisplayName(p.source)}</span>
                {p.has_separate_entrance === true && (
                  <span className="text-verified font-semibold">· Отдельный вход</span>
                )}
              </div>
              <h2 className="text-[16px] font-bold text-ink-primary leading-snug mt-1.5">
                {p.address || p.title || "Коммерческое помещение"}
              </h2>
              {p.title && p.address && p.title !== p.address && (
                <p className="text-[12px] text-ink-secondary mt-0.5">{p.title}</p>
              )}
            </div>

            {p.lat != null && p.lng != null && (
              <button
                type="button"
                onClick={() =>
                  onFocusCoord(p.lat!, p.lng!, p.address || p.title || `Лот #${candidate.rank}`)
                }
                title="Центрировать карту на этом объекте"
                className="shrink-0 h-8 px-2 border border-hairline bg-canvas hover:border-ink-primary rounded-[2px] text-[11px] font-mono"
              >
                На карте
              </button>
            )}
          </div>

          <div className="grid grid-cols-4 border border-hairline divide-x divide-hairline bg-canvas text-[12px] font-mono tabular-nums">
            <div className="p-2">
              <span className="text-[11px] text-ink-muted block">ПЛОЩАДЬ</span>
              <strong className="text-[13px] text-ink-primary">
                {p.area_sqm != null ? `${p.area_sqm} м²` : "—"}
              </strong>
            </div>
            <div className="p-2">
              <span className="text-[11px] text-ink-muted block">ЭТАЖ</span>
              <strong className="text-[13px] text-ink-primary">
                {formatFloorLabel(p.floor)}
              </strong>
            </div>
            <div className="p-2">
              <span className="text-[11px] text-ink-muted block">АРЕНДА</span>
              <strong className="text-[13px] text-ink-primary">
                {formatRub(p.price_month)}
              </strong>
            </div>
            <div className="p-2">
              <span className="text-[11px] text-ink-muted block">СТАВКА М²</span>
              <strong className="text-[13px] text-ink-primary">
                {pricePerSqmMonth ? `${formatNumber(pricePerSqmMonth)} ₽` : "—"}
              </strong>
            </div>
          </div>

          <ScoreCaliper
            minScore={candidate.total_score}
            maxScore={candidate.score_upper_bound}
            coverage={candidate.score_coverage}
            confirmation={candidate.confirmation}
            selected
          />
        </section>

        {section === "overview" && (
          <>
            {(reasonMessages.length > 0 || candidate.data_warnings.length > 0) && (
              <section className="bg-hatch-warn border border-warn/60 rounded-[2px] p-3 text-[12px] space-y-1.5">
                <div className="font-mono text-[11px] font-bold uppercase tracking-wider text-warn">
                  Границы достоверности
                </div>
                {reasonMessages.slice(0, 2).map((m, idx) => (
                  <p key={`r-${idx}`} className="text-ink-primary font-medium leading-snug">
                    {formatHumanBullet(m)}
                  </p>
                ))}
                {candidate.data_warnings.slice(0, 4).map((w, idx) => (
                  <p key={`w-${idx}`} className="text-ink-secondary leading-snug">
                    · {formatHumanBullet(w)}
                  </p>
                ))}
              </section>
            )}

            {photos.length > 0 && (
              <section className="space-y-2">
                <div className="flex items-center justify-between">
                  <h3 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary">
                    Фотографии помещения ({photos.length})
                  </h3>
                  <span className="text-[11px] text-ink-muted">Клик для увеличения</span>
                </div>
                <div className="grid grid-cols-3 gap-2">
                  {photos.slice(0, 6).map((src, i) => (
                    <button
                      key={i}
                      type="button"
                      onClick={() => setLightboxIdx(i)}
                      className="border border-hairline hover:border-signal rounded-[2px] overflow-hidden aspect-[4/3] bg-subtle"
                    >
                      <img
                        src={src}
                        alt={`Фото ${i + 1} объекта`}
                        loading="lazy"
                        className="w-full h-full object-cover"
                      />
                    </button>
                  ))}
                </div>
              </section>
            )}

            <section className="bg-paper border border-hairline rounded-[2px] p-3.5 space-y-3">
              <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-hairline pb-2">
                <h3 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary">
                  Вклад критериев в скоринговый балл
                </h3>
                <span className="font-mono text-[11px] text-ink-muted">
                  Шкала 0–100
                </span>
              </div>

              <div className="space-y-3">
                {candidate.scoring_components.map((sc, idx) => {
                  const maxPts = Math.max(1, (sc.weight ?? 0.25) * 100);
                  const lo = sc.contribution_lower ?? 0;
                  const hi = sc.contribution_upper ?? lo;
                  const pct = Math.min(100, Math.max(0, (lo / maxPts) * 100));
                  const upperPct = Math.min(100, Math.max(pct, (hi / maxPts) * 100));
                  const isInterval = hi - lo > 0.25;
                  const valText = formatRange(
                    sc.value_lower,
                    sc.value_upper,
                    1,
                    sc.unit ? ` ${sc.unit}` : ""
                  );

                  return (
                    <div key={idx} className="space-y-1">
                      <div className="flex items-baseline justify-between gap-2 text-[12px]">
                        <span className="font-medium text-ink-primary leading-snug">
                          {sc.label}
                        </span>
                        <span className="font-mono font-bold text-ink-primary tabular-nums shrink-0">
                          +{isInterval ? `${lo.toFixed(1)}–${hi.toFixed(1)}` : lo.toFixed(1)}
                        </span>
                      </div>
                      <div className="flex items-center gap-2">
                        <div className="relative flex-1 h-2 bg-subtle border border-hairline rounded-[1px]">
                          <div
                            className="absolute left-0 top-0 bottom-0 bg-ink-primary"
                            style={{ width: `${pct}%` }}
                          />
                          {isInterval && (
                            <div
                              className="absolute top-0 bottom-0 bg-signal/35"
                              style={{ left: `${pct}%`, width: `${upperPct - pct}%` }}
                            />
                          )}
                        </div>
                        <span className="font-mono text-[11px] text-ink-muted tabular-nums min-w-[90px] text-right">
                          {valText}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </section>

            <section className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div className="bg-paper border border-hairline rounded-[2px] p-3 space-y-1.5">
                <h4 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-verified">
                  Плюсы локации
                </h4>
                {candidate.pros.length === 0 ? (
                  <p className="text-[12px] text-ink-muted">Нет выраженных преимуществ.</p>
                ) : (
                  <ul className="space-y-1 text-[12px] text-ink-primary">
                    {candidate.pros.map((item, i) => (
                      <li key={i} className="leading-snug">
                        + {formatHumanBullet(item)}
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <div className="bg-paper border border-hairline rounded-[2px] p-3 space-y-1.5">
                <h4 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-warn">
                  Риски и ограничения
                </h4>
                {candidate.cons.length === 0 ? (
                  <p className="text-[12px] text-ink-muted">Явных минусов не найдено.</p>
                ) : (
                  <ul className="space-y-1 text-[12px] text-ink-primary">
                    {candidate.cons.map((item, i) => (
                      <li key={i} className="leading-snug">
                        − {formatHumanBullet(item)}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </section>

            {p.description && (
              <section className="bg-paper border border-hairline rounded-[2px] p-3 space-y-1.5">
                <h4 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary">
                  Описание из объявления
                </h4>
                <p className="text-[12px] text-ink-secondary leading-relaxed whitespace-pre-line">
                  {p.description}
                </p>
              </section>
            )}
          </>
        )}

        {section === "env" && comp && (
          <>
            <section className="bg-paper border border-hairline rounded-[2px] p-3.5 space-y-2.5">
              <div className="flex items-center justify-between border-b border-hairline pb-2">
                <h3 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary">
                  Конкуренты, трафик и жилой фонд
                </h3>
                <span className="font-mono text-[12px] font-semibold text-ink-primary tabular-nums">
                  Организаций в радиусе: {comp.total_cards_found}
                </span>
              </div>

              {compSummaryText && (
                <p className="text-[12px] font-medium text-ink-primary">{compSummaryText}</p>
              )}

              {cleanAiSummary && cleanAiSummary !== compSummaryText && (
                <p className="text-[12px] text-ink-secondary leading-relaxed bg-canvas p-2.5 border border-hairline rounded-[2px]">
                  {cleanAiSummary}
                </p>
              )}

              <div className="grid grid-cols-2 gap-2.5 pt-1 text-[12px]">
                <div className="border border-hairline bg-canvas p-2.5 rounded-[2px]">
                  <span className="font-mono text-[11px] uppercase text-ink-muted block">
                    ПРОКСИ ПОТОКА
                  </span>
                  <strong className="font-mono text-[14px] text-ink-primary tabular-nums">
                    {comp.foot_traffic?.estimated_daily_pedestrians != null
                      ? `${formatNumber(comp.foot_traffic.estimated_daily_pedestrians)} / день`
                      : trafficLevelLabel(comp.foot_traffic?.traffic_level)}
                  </strong>
                  {nearestMetro && metroLabel && (
                    <div className="text-[11px] text-ink-secondary mt-1">
                      М. {metroLabel} ({Math.round(nearestMetro.distance_m ?? 0)} м)
                    </div>
                  )}
                </div>

                <div className="border border-hairline bg-canvas p-2.5 rounded-[2px]">
                  <span className="font-mono text-[11px] uppercase text-ink-muted block">
                    ЖИТЕЛИ В {comp.population?.radius_m ? Math.round(comp.population.radius_m) : 200} М (ЕИСЖС)
                  </span>
                  <strong className="font-mono text-[14px] text-ink-primary tabular-nums">
                    {comp.population?.estimated_residents_count != null
                      ? `${formatNumber(comp.population.estimated_residents_count)} чел.`
                      : "Нет точных данных"}
                  </strong>
                  {comp.population?.residential_buildings_count != null && (
                    <div className="text-[11px] text-ink-secondary mt-1">
                      МКД в зоне: {comp.population.residential_buildings_count}
                    </div>
                  )}
                </div>
              </div>
            </section>

            {hourlyWeekday.length > 0 && (
              <section className="bg-paper border border-hairline rounded-[2px] p-3.5 space-y-2">
                <div className="flex items-center justify-between">
                  <h4 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary">
                    Суточный ритм активности (будни)
                  </h4>
                  {comp.foot_traffic?.hourly_profile?.peak_weekday_hour != null && (
                    <span className="font-mono text-[11px] text-signal font-semibold">
                      Пик: {comp.foot_traffic.hourly_profile.peak_weekday_hour}:00
                    </span>
                  )}
                </div>
                <div className="flex items-end gap-1 h-[76px] pt-2 border-b border-ink-primary">
                  {hourlyWeekday.map((share, hourIdx) => {
                    const pct = Math.max(8, Math.round((Number(share || 0) / peakHourlyVal) * 100));
                    const isPeak = Number(share || 0) >= peakHourlyVal * 0.85;
                    return (
                      <div
                        key={hourIdx}
                        title={`${hourIdx}:00 — доля ${(Number(share || 0) * 100).toFixed(1)}%`}
                        className="flex-1 flex flex-col items-center justify-end h-full"
                      >
                        <div
                          className={`w-full rounded-t-[1px] ${
                            isPeak ? "bg-signal" : "bg-ink-primary/80"
                          }`}
                          style={{ height: `${pct}%` }}
                        />
                      </div>
                    );
                  })}
                </div>
                <div className="flex justify-between font-mono text-[11px] text-ink-muted">
                  <span>00:00</span>
                  <span>08:00</span>
                  <span>12:00</span>
                  <span>18:00</span>
                  <span>23:00</span>
                </div>
              </section>
            )}

            <section className="bg-paper border border-hairline rounded-[2px] divide-y divide-hairline">
              <div className="px-3.5 py-2 bg-canvas font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary flex justify-between gap-2">
                <span>Организации рядом</span>
                <span>
                  {comp.direct_competitors.length} прямых · {comp.indirect_competitors.length} косвенных ·{" "}
                  {comp.synergy_businesses.length} синергия
                </span>
              </div>

              {[
                ...comp.direct_competitors.map((c) => ({ c, tag: "Прямой", cls: "text-signal" })),
                ...comp.indirect_competitors.map((c) => ({ c, tag: "Косвенный", cls: "text-warn" })),
                ...comp.synergy_businesses.map((c) => ({ c, tag: "Синергия", cls: "text-verified" }))
              ]
                .slice(0, 20)
                .map(({ c, tag, cls }, idx) => (
                  <button
                    key={idx}
                    type="button"
                    onClick={() => {
                      if (c.lat != null && c.lng != null) {
                        onFocusCoord(c.lat, c.lng, c.title);
                      }
                    }}
                    className="w-full text-left px-3.5 py-2 hover:bg-subtle transition-colors flex items-center justify-between gap-2 text-[12px]"
                  >
                    <div className="min-w-0">
                      <span className={`font-mono text-[11px] font-semibold mr-2 ${cls}`}>
                        [{tag}]
                      </span>
                      <span className="font-medium text-ink-primary">{c.title}</span>
                    </div>
                    <span className="font-mono text-[11px] text-ink-muted tabular-nums shrink-0">
                      {Math.round(c.distance_m)} м{c.rating ? ` · ★ ${c.rating}` : ""}
                    </span>
                  </button>
                ))}
            </section>
          </>
        )}

        {section === "fns" && (
          <>
            <section className="bg-paper border border-hairline rounded-[2px] overflow-hidden">
              <div className="px-3.5 py-2.5 border-b border-hairline bg-canvas flex items-center justify-between gap-2">
                <h3 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary">
                  Проверка по данным ФНС ({comp?.fns_enriched_count ?? 0})
                </h3>
                {(comp?.fns_enriched_count ?? 0) > 0 && candidate.verified_fns_revenue_mln_sum != null ? (
                  <span className="font-mono text-[12px] font-bold text-ink-primary tabular-nums">
                    Выручка рядом: {candidate.verified_fns_revenue_mln_sum.toFixed(1)} млн ₽
                  </span>
                ) : (
                  <span className="font-mono text-[11px] text-ink-muted">
                    Нет совпадений в ФНС
                  </span>
                )}
              </div>

              {fnsRecords.length === 0 ? (
                <div className="p-4 text-[12px] text-ink-muted">
                  Для окружения этого помещения нет подтверждённых карточек ФНС (или не настроен ключ DaData).
                </div>
              ) : (
                <div className="divide-y divide-hairline">
                  {fnsRecords.map(({ comp: rec, kind }, i) => (
                    <div key={i} className="px-3.5 py-2.5 text-[12px] space-y-1">
                      <div className="flex items-center justify-between gap-2">
                        <strong className="text-ink-primary">{rec.fns_legal_name || rec.title}</strong>
                        <span className="font-mono text-[11px] text-ink-secondary">
                          {kind} · {fnsMatchStatusLabel(rec.fns_match_status)}
                        </span>
                      </div>
                      <div className="font-mono text-[11px] text-ink-muted tabular-nums flex flex-wrap gap-3">
                        {rec.fns_inn && <span>ИНН {rec.fns_inn}</span>}
                        {rec.fns_revenue_rub != null && (
                          <span>
                            Выручка: {(rec.fns_revenue_rub / 1e6).toFixed(2)} млн ₽
                            {rec.fns_reporting_year ? ` (${rec.fns_reporting_year})` : ""}
                          </span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>

            <section className="bg-paper border border-hairline rounded-[2px] overflow-hidden">
              <div className="px-3.5 py-2.5 border-b border-hairline bg-canvas">
                <h3 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary">
                  Проверка условий допуска и публикации
                </h3>
              </div>

              {checkEntries.length === 0 ? (
                <div className="p-4 text-[12px] text-ink-muted">
                  Дополнительных ограничений допуска не зафиксировано.
                </div>
              ) : (
                <div className="divide-y divide-hairline">
                  {checkEntries.map(([key, chk]) => (
                    <div key={key} className="px-3.5 py-2.5 text-[12px] space-y-1.5">
                      <div className="flex items-start justify-between gap-2">
                        <strong className="text-ink-primary">
                          {CHECK_GROUP_LABELS[key] || key}
                        </strong>
                        <span className="font-mono text-[11px] font-semibold text-ink-secondary shrink-0">
                          {checkResultDisplay(chk.result, key)}
                        </span>
                      </div>
                      {chk.reason && (
                        <p className="text-ink-secondary leading-snug">{chk.reason}</p>
                      )}
                      {chk.items && chk.items.length > 0 && (
                        <div className="space-y-1 pl-2 border-l-2 border-hairline">
                          {chk.items.map((sub, j) => (
                            <div key={j} className="text-[11px] text-ink-secondary">
                              <span className="font-medium text-ink-primary">
                                {sub.label || sub.client_quote}:
                              </span>{" "}
                              {checkResultDisplay(sub.result)}
                              {sub.reason ? ` — ${sub.reason}` : ""}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </section>
          </>
        )}
      </div>

      {lightboxIdx !== null && photos[lightboxIdx] && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 bg-[#181b1f]/90 flex flex-col items-center justify-center p-6"
          onClick={() => setLightboxIdx(null)}
        >
          <div
            className="relative max-w-4xl w-full flex flex-col items-center"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="w-full flex items-center justify-between text-[#f5f3ee] mb-3 font-mono text-[12px]">
              <span>
                Фото {lightboxIdx + 1} / {photos.length} · {p.address || p.title}
              </span>
              <button
                type="button"
                onClick={() => setLightboxIdx(null)}
                className="px-2.5 py-1 border border-[#f5f3ee]/40 rounded-[2px] hover:bg-white/10 inline-flex items-center gap-1"
              >
                <X className="w-3.5 h-3.5" /> Закрыть (Esc)
              </button>
            </div>
            <img
              src={photos[lightboxIdx]}
              alt={`Фото ${lightboxIdx + 1}`}
              className="max-h-[75vh] object-contain border border-[#f5f3ee]/30"
            />
          </div>
        </div>
      )}
    </div>
  );
}
