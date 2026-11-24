import { ExternalLink, MapPin, Navigation, X } from "lucide-react";
import type { SelectedLeadTarget, SelectedMapPoint } from "../types/geoestate";
import {
  computePriceSqmMonth,
  formatFloorLabel,
  formatListingFreshness,
  formatNumber,
  formatRub,
  formatScoreInterval,
  sourceDisplayName
} from "../utils/formatters";

export interface MapPointCardProps {
  readonly point: SelectedMapPoint | null;
  readonly radiusMeters: number;
  readonly onClose: () => void;
  readonly onOpenPassport: (rank: number) => void;
  readonly onOpenLeadCard?: (lead: SelectedLeadTarget) => void;
  readonly onSearchAroundPoint: (lat: number, lng: number, label: string) => void;
}

export function MapPointCard({
  point,
  radiusMeters,
  onClose,
  onOpenPassport,
  onOpenLeadCard,
  onSearchAroundPoint
}: MapPointCardProps) {
  if (!point) return null;

  const radiusKm = (radiusMeters / 1000).toFixed(radiusMeters % 1000 === 0 ? 0 : 1);

  if (point.kind === "preview") {
    const item = point.listing;
    const sqmMonth = computePriceSqmMonth(item);
    const fresh = formatListingFreshness(item.last_seen_at);
    return (
      <div className="bg-paper border border-ink-primary rounded-[2px] w-[335px] p-3">
        <div className="flex items-start justify-between gap-2 pb-2 border-b border-hairline">
          <div className="min-w-0">
            <span className="font-mono text-[11px] uppercase tracking-wider text-ink-muted block">
              {sourceDisplayName(item.source)} · База
              {fresh && (
                <span
                  title={fresh.fullDate ? `Снимок в БД: ${fresh.fullDate}` : undefined}
                  className={fresh.isStale ? " text-warn font-semibold" : ""}
                >
                  {" "}
                  · {fresh.label}
                </span>
              )}
            </span>
            <p className="text-[13px] font-bold text-ink-primary leading-snug line-clamp-2 mt-0.5">
              {item.address || item.title || "Помещение"}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Закрыть карточку точки"
            className="p-0.5 text-ink-muted hover:text-ink-primary shrink-0"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>

        <div className="grid grid-cols-3 gap-2 py-2 text-[12px] font-mono tabular-nums">
          <div>
            <span className="text-[11px] text-ink-muted block">ПЛОЩАДЬ</span>
            <strong>{item.area_sqm != null ? `${item.area_sqm} м²` : "—"}</strong>
          </div>
          <div>
            <span className="text-[11px] text-ink-muted block">АРЕНДА</span>
            <strong className="block">{formatRub(item.price_month)}</strong>
            {sqmMonth != null && (
              <span className="text-[11px] text-ink-muted block">
                {formatNumber(sqmMonth)} ₽/м²
              </span>
            )}
          </div>
          <div>
            <span className="text-[11px] text-ink-muted block">ЭТАЖ</span>
            <strong>{formatFloorLabel(item.floor)}</strong>
          </div>
        </div>

        {item.description?.trim() && (
          <p className="text-[11px] text-ink-secondary leading-snug line-clamp-2 pb-2">
            {item.description}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-1.5 pt-2 border-t border-hairline">
          {onOpenLeadCard && (
            <button
              type="button"
              onClick={() => onOpenLeadCard({ kind: "preview", listing: item })}
              className="h-8 px-2.5 bg-ink-primary hover:bg-[#2a2f36] text-[#f5f3ee] text-[12px] font-semibold rounded-[2px]"
            >
              Подробнее →
            </button>
          )}
          {item.lat != null && item.lng != null && (
            <button
              type="button"
              onClick={() =>
                onSearchAroundPoint(
                  item.lat!,
                  item.lng!,
                  item.address || item.title || "Точка"
                )
              }
              className="flex-1 h-8 px-2.5 bg-signal hover:bg-signal-hover text-[#fffdf9] text-[12px] font-semibold rounded-[2px] inline-flex items-center justify-center gap-1.5 whitespace-nowrap"
            >
              <Navigation className="w-3.5 h-3.5" />
              В R {radiusKm} км
            </button>
          )}
          {item.url && (
            <a
              href={item.url}
              target="_blank"
              rel="noreferrer"
              className="h-8 px-2.5 border border-ink-primary bg-canvas hover:bg-subtle text-[12px] font-medium text-ink-primary rounded-[2px] inline-flex items-center gap-1"
            >
              <ExternalLink className="w-3 h-3" />
              Лот
            </a>
          )}
        </div>
      </div>
    );
  }

  if (point.kind === "ranked") {
    const c = point.candidate;
    const p = c.property_data;
    return (
      <div className="bg-paper border border-ink-primary rounded-[2px] w-[335px] p-3">
        <div className="flex items-start justify-between gap-2 pb-2 border-b border-hairline">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="font-mono text-[11px] font-bold px-1.5 py-0.5 bg-signal text-[#fffdf9] rounded-[2px]">
                #{c.rank}
              </span>
              <span className="font-mono text-[12px] font-bold text-ink-primary tabular-nums">
                {formatScoreInterval(c)} / 100
              </span>
            </div>
            <p className="text-[13px] font-bold text-ink-primary truncate mt-1">
              {p.address || p.title}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Закрыть карточку"
            className="p-0.5 text-ink-muted hover:text-ink-primary"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>

        <div className="flex items-center justify-between text-[12px] font-mono tabular-nums py-2">
          <span>{p.area_sqm ?? "?"} м² · {formatFloorLabel(p.floor)}</span>
          <strong>{formatRub(p.price_month)}</strong>
        </div>

        <div className="flex items-center gap-2 pt-1.5 border-t border-hairline">
          <button
            type="button"
            onClick={() => onOpenPassport(c.rank)}
            className="flex-1 h-8 px-2.5 bg-ink-primary hover:bg-[#2a2f36] text-[#f5f3ee] text-[12px] font-semibold rounded-[2px]"
          >
            Открыть досье #{c.rank} →
          </button>
          {p.lat != null && p.lng != null && (
            <button
              type="button"
              onClick={() =>
                onSearchAroundPoint(p.lat!, p.lng!, p.address || p.title || "Лот")
              }
              title="Подобрать вокруг этого адреса"
              className="h-8 px-2.5 border border-ink-primary bg-canvas hover:bg-subtle text-[12px] font-medium rounded-[2px] inline-flex items-center gap-1"
            >
              <MapPin className="w-3.5 h-3.5 text-signal" />
              Вокруг
            </button>
          )}
        </div>
      </div>
    );
  }

  if (point.kind === "deferred") {
    const d = point.candidate;
    return (
      <div className="bg-paper border border-warn rounded-[2px] w-[335px] p-3">
        <div className="flex items-start justify-between gap-2 pb-2 border-b border-hairline">
          <div className="min-w-0">
            <span className="font-mono text-[11px] font-semibold text-warn block">
              ТРЕБУЕТ УТОЧНЕНИЯ
            </span>
            <p className="text-[13px] font-bold text-ink-primary leading-snug line-clamp-2 mt-0.5">
              {d.address || d.title}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Закрыть карточку"
            className="p-0.5 text-ink-muted hover:text-ink-primary"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
        <p className="text-[12px] text-ink-secondary my-2">{d.reason}</p>
        <div className="flex flex-wrap items-center gap-2 pt-1.5 border-t border-hairline">
          {onOpenLeadCard && (
            <button
              type="button"
              onClick={() => onOpenLeadCard({ kind: "deferred", candidate: d })}
              className="h-8 px-2.5 bg-ink-primary hover:bg-[#2a2f36] text-[#f5f3ee] text-[12px] font-semibold rounded-[2px]"
            >
              Подробнее →
            </button>
          )}
          {d.url && (
            <a
              href={d.url}
              target="_blank"
              rel="noreferrer"
              className="h-8 px-2.5 border border-ink-primary bg-canvas hover:bg-subtle inline-flex items-center gap-1 text-[12px] font-medium text-ink-primary rounded-[2px]"
            >
              <ExternalLink className="w-3 h-3" />
              Лот
            </a>
          )}
        </div>
      </div>
    );
  }

  const comp = point.competitor;
  return (
    <div className="bg-paper border border-ink-primary rounded-[2px] w-[310px] p-3">
      <div className="flex items-start justify-between gap-2 pb-1.5 border-b border-hairline">
        <div className="min-w-0">
          <span
            className="inline-flex items-center gap-1.5 text-[11px] font-mono font-semibold"
            style={{ color: point.groupColor }}
          >
            <span
              className="w-2 h-2 rounded-[1px]"
              style={{ backgroundColor: point.groupColor }}
            />
            {point.groupLabel}
          </span>
          <p className="text-[13px] font-bold text-ink-primary truncate mt-0.5">
            {comp.title}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Закрыть карточку конкурента"
          className="p-0.5 text-ink-muted hover:text-ink-primary"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>
      <div className="text-[12px] font-mono tabular-nums text-ink-secondary pt-2 space-y-1">
        <div>Дистанция: {formatNumber(comp.distance_m)} м{comp.rating ? ` · ★ ${comp.rating}` : ""}</div>
        {comp.fns_revenue_rub != null && (
          <div>Выручка ФНС: {(comp.fns_revenue_rub / 1e6).toFixed(1)} млн ₽</div>
        )}
        {comp.address && (
          <div className="font-sans text-ink-muted truncate">{comp.address}</div>
        )}
      </div>
    </div>
  );
}
