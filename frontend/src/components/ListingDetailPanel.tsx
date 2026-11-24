import { useEffect, useState } from "react";
import {
  ArrowLeft,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  MapPin,
  Navigation,
  X
} from "lucide-react";
import type { SelectedLeadTarget } from "../types/geoestate";
import {
  formatFloorLabel,
  formatListingFreshness,
  formatNumber,
  formatRub,
  listingIdentityKey,
  sourceDisplayName
} from "../utils/formatters";

export interface ListingDetailPanelProps {
  readonly lead: SelectedLeadTarget;
  readonly backLabel: string;
  readonly positionLabel?: string | null;
  readonly onClose: () => void;
  readonly onPrev: (() => void) | null;
  readonly onNext: (() => void) | null;
  readonly onFocusOnMap?: ((lat: number, lng: number, label: string) => void) | null;
  readonly onSearchAroundPoint: (lat: number, lng: number, label: string) => void;
}

export function ListingDetailPanel({
  lead,
  backLabel,
  positionLabel,
  onClose,
  onPrev,
  onNext,
  onFocusOnMap,
  onSearchAroundPoint
}: ListingDetailPanelProps) {
  const [lightboxIdx, setLightboxIdx] = useState<number | null>(null);
  const [failedPhotos, setFailedPhotos] = useState<Record<number, boolean>>({});

  const item = lead.kind === "preview" ? lead.listing : lead.candidate;
  const itemKey = `${lead.kind}:${listingIdentityKey(item)}`;
  const photos = item.photos ?? [];
  const visiblePhotos = photos
    .map((src, idx) => ({ src, idx }))
    .filter(({ idx }) => !failedPhotos[idx]);

  const pricePerSqmMonth =
    item.price_month && item.area_sqm && item.area_sqm > 0
      ? Math.round(item.price_month / item.area_sqm)
      : item.price_sqm_year && item.price_sqm_year > 0
        ? Math.round(item.price_sqm_year / 12)
        : null;

  const pricePerSqmYear =
    item.price_sqm_year && item.price_sqm_year > 0
      ? Math.round(item.price_sqm_year)
      : pricePerSqmMonth
        ? pricePerSqmMonth * 12
        : null;

  useEffect(() => {
    setLightboxIdx(null);
    setFailedPhotos({});
  }, [itemKey]);

  useEffect(() => {
    if (lightboxIdx === null) return;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") setLightboxIdx(null);
      if (ev.key === "ArrowLeft" && visiblePhotos.length > 0) {
        const curPos = visiblePhotos.findIndex((p) => p.idx === lightboxIdx);
        const prevPos = (curPos - 1 + visiblePhotos.length) % visiblePhotos.length;
        setLightboxIdx(visiblePhotos[prevPos]?.idx ?? null);
      }
      if (ev.key === "ArrowRight" && visiblePhotos.length > 0) {
        const curPos = visiblePhotos.findIndex((p) => p.idx === lightboxIdx);
        const nextPos = (curPos + 1) % visiblePhotos.length;
        setLightboxIdx(visiblePhotos[nextPos]?.idx ?? null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lightboxIdx, visiblePhotos]);

  const badgeMeta =
    lead.kind === "deferred"
      ? {
          label: "Требует уточнения",
          cls: "bg-state-warn-bg text-warn border-warn/60"
        }
      : lead.kind === "rejected"
        ? {
            label: "Отклонён фильтром",
            cls: "bg-[#fef1f1] text-danger border-danger/50"
          }
        : {
            label: "Объект базы БД",
            cls: "bg-subtle text-ink-primary border-ink-primary"
          };

  const titleLine = item.title?.trim() || null;
  const addressLine = item.address?.trim() || titleLine || "Коммерческое помещение";
  const entranceText =
    item.has_separate_entrance === true
      ? "Отдельный вход"
      : item.has_separate_entrance === false
        ? "Не выделен как отдельный"
        : "Не указан";

  return (
    <div className="h-full flex flex-col bg-canvas">
      {/* Top Sticky Action Bar */}
      <div className="shrink-0 h-[44px] px-3 bg-paper border-b border-ink-primary flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <button
            type="button"
            onClick={onClose}
            className="h-7 px-2 border border-hairline bg-canvas hover:border-ink-primary rounded-[2px] text-[12px] font-semibold inline-flex items-center gap-1 text-ink-primary whitespace-nowrap"
          >
            <ArrowLeft className="w-3.5 h-3.5" />
            <span>{backLabel}</span>
          </button>

          {(onPrev || onNext) && (
            <div className="flex border border-hairline rounded-[2px] overflow-hidden shrink-0">
              <button
                type="button"
                disabled={!onPrev}
                onClick={onPrev ?? undefined}
                title="Предыдущий объект"
                aria-label="Предыдущий объект"
                className="h-7 w-7 bg-canvas hover:bg-subtle disabled:opacity-35 flex items-center justify-center border-r border-hairline"
              >
                <ChevronUp className="w-3.5 h-3.5" />
              </button>
              <button
                type="button"
                disabled={!onNext}
                onClick={onNext ?? undefined}
                title="Следующий объект"
                aria-label="Следующий объект"
                className="h-7 w-7 bg-canvas hover:bg-subtle disabled:opacity-35 flex items-center justify-center"
              >
                <ChevronDown className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          {item.lat != null && item.lng != null && (
            <button
              type="button"
              onClick={() => onSearchAroundPoint(item.lat!, item.lng!, addressLine)}
              title="Подобрать подходящие помещения в радиусе от этого адреса"
              className="h-7 px-2.5 bg-signal hover:bg-signal-hover text-[#fffdf9] rounded-[2px] text-[12px] font-semibold inline-flex items-center gap-1 transition-colors whitespace-nowrap"
            >
              <Navigation className="w-3 h-3" />
              <span>Вокруг</span>
            </button>
          )}

          {item.url && (
            <a
              href={item.url}
              target="_blank"
              rel="noreferrer"
              className="h-7 px-2 border border-ink-primary bg-paper hover:bg-ink-primary hover:text-[#f5f3ee] rounded-[2px] text-[12px] font-medium inline-flex items-center gap-1 transition-colors whitespace-nowrap"
            >
              <ExternalLink className="w-3 h-3" />
              <span>{sourceDisplayName(item.source)}</span>
            </a>
          )}
        </div>
      </div>

      {/* Scrollable Body */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        <section className="bg-paper border border-ink-primary rounded-[2px] p-3.5 space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-[11px]">
            <div className="flex flex-wrap items-center gap-2 min-w-0">
              <span className={`px-1.5 py-0.5 font-bold uppercase border rounded-[2px] ${badgeMeta.cls}`}>
                {badgeMeta.label}
              </span>
              <span className="text-ink-secondary font-semibold">
                {sourceDisplayName(item.source)}
              </span>
              {item.external_id && (
                <span className="text-ink-muted tabular-nums">ID {item.external_id}</span>
              )}
              {positionLabel && (
                <span className="text-ink-muted tabular-nums">· {positionLabel}</span>
              )}
              {item.has_separate_entrance === true && (
                <span className="text-verified font-semibold">· Отдельный вход</span>
              )}
              {(() => {
                const fresh = formatListingFreshness(item.last_seen_at);
                if (!fresh) return null;
                return (
                  <span
                    title={
                      fresh.fullDate
                        ? `Последний снимок объявления в БД: ${fresh.fullDate}`
                        : undefined
                    }
                    className={fresh.isStale ? "text-warn font-semibold" : "text-ink-muted"}
                  >
                    · Снимок: {fresh.label}
                  </span>
                );
              })()}
            </div>
          </div>

          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h2 className="text-[16px] font-bold text-ink-primary leading-snug">
                {addressLine}
              </h2>
              {titleLine && titleLine !== addressLine && (
                <p className="text-[12px] text-ink-secondary mt-1">{titleLine}</p>
              )}
            </div>

            {item.lat != null && item.lng != null && onFocusOnMap && (
              <button
                type="button"
                onClick={() => onFocusOnMap(item.lat!, item.lng!, addressLine)}
                title="Показать объект на карте"
                className="shrink-0 h-8 px-2.5 border border-ink-primary bg-canvas hover:bg-subtle rounded-[2px] text-[11px] font-mono font-semibold inline-flex items-center gap-1 text-ink-primary whitespace-nowrap"
              >
                <MapPin className="w-3 h-3 text-signal" />
                <span>На карте</span>
              </button>
            )}
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 border border-hairline divide-x divide-y sm:divide-y-0 divide-hairline bg-canvas text-[12px] font-mono tabular-nums">
            <div className="p-2">
              <span className="text-[11px] text-ink-muted block">ПЛОЩАДЬ</span>
              <strong className="text-[13px] text-ink-primary">
                {item.area_sqm != null ? `${item.area_sqm} м²` : "—"}
              </strong>
            </div>
            <div className="p-2">
              <span className="text-[11px] text-ink-muted block">ЭТАЖ</span>
              <strong className="text-[13px] text-ink-primary">
                {formatFloorLabel(item.floor)}
              </strong>
            </div>
            <div className="p-2">
              <span className="text-[11px] text-ink-muted block">АРЕНДА / МЕС</span>
              <strong className="text-[13px] text-ink-primary">
                {formatRub(item.price_month)}
              </strong>
            </div>
            <div className="p-2">
              <span className="text-[11px] text-ink-muted block">СТАВКА М²</span>
              <strong className="text-[13px] text-ink-primary">
                {pricePerSqmMonth ? `${formatNumber(pricePerSqmMonth)} ₽` : "—"}
              </strong>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 pt-1 text-[12px]">
            <div className="bg-canvas border border-hairline rounded-[2px] px-2.5 py-2">
              <span className="font-mono text-[11px] text-ink-muted block">ВХОД</span>
              <span className="font-medium text-ink-primary">{entranceText}</span>
            </div>
            <div className="bg-canvas border border-hairline rounded-[2px] px-2.5 py-2">
              <span className="font-mono text-[11px] text-ink-muted block">МОЩНОСТЬ</span>
              <span className="font-mono font-medium text-ink-primary tabular-nums">
                {item.power_kw != null ? `${item.power_kw} кВт` : "Не указана"}
              </span>
            </div>
            <div className="bg-canvas border border-hairline rounded-[2px] px-2.5 py-2">
              <span className="font-mono text-[11px] text-ink-muted block">ГОДОВАЯ СТАВКА</span>
              <span className="font-mono font-medium text-ink-primary tabular-nums">
                {pricePerSqmYear ? `${formatNumber(pricePerSqmYear)} ₽/м²·год` : "—"}
              </span>
            </div>
          </div>

          {item.lat != null && item.lng != null && (
            <div className="font-mono text-[11px] text-ink-muted tabular-nums flex items-center justify-between gap-2 pt-1 border-t border-hairline">
              <span>
                Координаты: {item.lat.toFixed(6)}, {item.lng.toFixed(6)}
              </span>
              <span>ID БД: #{item.id}</span>
            </div>
          )}
        </section>

        {lead.kind === "deferred" && (
          <section className="bg-hatch-warn border border-warn/60 rounded-[2px] p-3.5 space-y-2 text-[12px]">
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-[11px] font-bold uppercase tracking-wider text-warn">
                Причина переноса на допроверку
              </span>
              {lead.candidate.bound?.score_lower_bound != null &&
                lead.candidate.bound?.score_upper_bound != null && (
                  <span className="font-mono text-[11px] font-bold text-ink-primary tabular-nums">
                    Потенциал: {lead.candidate.bound.score_lower_bound.toFixed(1)}–
                    {lead.candidate.bound.score_upper_bound.toFixed(1)} / 100
                  </span>
                )}
            </div>
            <p className="text-ink-primary font-medium leading-snug">{lead.candidate.reason}</p>
            {lead.candidate.missing_facts.length > 0 && (
              <div className="flex flex-wrap gap-1.5 pt-1">
                {lead.candidate.missing_facts.map((fact, idx) => (
                  <span
                    key={idx}
                    className="font-mono text-[11px] px-2 py-0.5 bg-paper border border-warn/50 rounded-[2px] text-warn"
                  >
                    {fact}
                  </span>
                ))}
              </div>
            )}
          </section>
        )}

        {lead.kind === "rejected" && (
          <section className="bg-[#fef1f1] border border-danger/60 rounded-[2px] p-3.5 space-y-1.5 text-[12px]">
            <span className="font-mono text-[11px] font-bold uppercase tracking-wider text-danger block">
              Причина отклонения при фильтрации
            </span>
            <p className="text-ink-primary font-medium leading-snug">{lead.candidate.reason}</p>
          </section>
        )}

        {visiblePhotos.length > 0 && (
          <section className="space-y-2">
            <div className="flex items-center justify-between">
              <h3 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary">
                Фотографии помещения ({visiblePhotos.length})
              </h3>
              <span className="text-[11px] text-ink-muted">Клик для увеличения</span>
            </div>
            <div className="grid grid-cols-3 gap-2">
              {visiblePhotos.slice(0, 9).map(({ src, idx }) => (
                <button
                  key={idx}
                  type="button"
                  onClick={() => setLightboxIdx(idx)}
                  className="border border-hairline hover:border-signal rounded-[2px] overflow-hidden aspect-[4/3] bg-subtle"
                >
                  <img
                    src={src}
                    alt={`Фото ${idx + 1} объекта`}
                    loading="lazy"
                    onError={() => setFailedPhotos((prev) => ({ ...prev, [idx]: true }))}
                    className="w-full h-full object-cover"
                  />
                </button>
              ))}
            </div>
          </section>
        )}

        <section className="bg-paper border border-hairline rounded-[2px] p-3.5 space-y-2">
          <h3 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary">
            Полное описание из объявления
          </h3>
          {item.description?.trim() ? (
            <p className="text-[12px] text-ink-primary leading-relaxed whitespace-pre-line">
              {item.description}
            </p>
          ) : (
            <p className="text-[12px] text-ink-muted">
              Текстовое описание не передано источником при сохранении лота.
            </p>
          )}
        </section>
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
                Фото {lightboxIdx + 1} / {photos.length} · {addressLine}
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
