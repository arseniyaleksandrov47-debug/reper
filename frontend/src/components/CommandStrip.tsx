import { useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  Copy,
  Crosshair,
  Download,
  MapPin,
  Minus,
  Play,
  Ruler,
  SlidersHorizontal,
  Square,
  Wallet,
  X
} from "lucide-react";
import type { CityInfo, CityKey, SearchRequest } from "../types/geoestate";
import { FLOOR_PRESET_CHIPS, formatFloorsSummary, parseFloorsString } from "../utils/conceptParser";
import { formatNumber } from "../utils/formatters";

type PopoverId = "zone" | "space" | "budget" | "export" | null;

export interface CommandStripProps {
  readonly request: SearchRequest;
  readonly cities: readonly CityInfo[];
  readonly floorInput: string;
  readonly onChangeFloorInput: (val: string) => void;
  readonly onChangeRequest: (updater: (prev: SearchRequest) => SearchRequest) => void;
  readonly onCitySwitch: (city: CityKey) => void;
  readonly onRunSearch: () => void;
  readonly onCancelSearch?: () => void;
  readonly onOpenConceptHelper: () => void;
  readonly onExport: (format: "markdown" | "json") => void;
  readonly hasReport: boolean;
  readonly isRunning: boolean;
  readonly elapsedSec: number;
  readonly pinPickMode: boolean;
  readonly onTogglePinPickMode: () => void;
  readonly onClearMapCenter: () => void;
  readonly dbPreviewCount: number;
  readonly isDesktopShell?: boolean;
  readonly isMaximized?: boolean;
  readonly onMinimizeWindow?: () => void;
  readonly onToggleMaximizeWindow?: () => void;
  readonly onCloseWindow?: () => void;
}

const POOL_OPTIONS: ReadonlyArray<{ label: string; value: number | null }> = [
  { label: "6", value: 6 },
  { label: "12", value: 12 },
  { label: "24", value: 24 },
  { label: "Все", value: null }
];

const RADIUS_OPTIONS = [500, 800, 1200, 2000, 3000];

export function CommandStrip({
  request,
  cities,
  floorInput,
  onChangeFloorInput,
  onChangeRequest,
  onCitySwitch,
  onRunSearch,
  onCancelSearch,
  onOpenConceptHelper,
  onExport,
  hasReport,
  isRunning,
  elapsedSec,
  pinPickMode,
  onTogglePinPickMode,
  onClearMapCenter,
  dbPreviewCount,
  isDesktopShell = false,
  isMaximized = false,
  onMinimizeWindow,
  onToggleMaximizeWindow,
  onCloseWindow
}: CommandStripProps) {
  const [openPopover, setOpenPopover] = useState<PopoverId>(null);
  const stripRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const handleOutside = (ev: MouseEvent) => {
      if (stripRef.current && !stripRef.current.contains(ev.target as Node)) {
        setOpenPopover(null);
      }
    };
    const handleKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") setOpenPopover(null);
    };
    document.addEventListener("mousedown", handleOutside);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleOutside);
      document.removeEventListener("keydown", handleKey);
    };
  }, []);

  const hasPinnedPoint = request.center_lat != null && request.center_lng != null;
  const radiusLabel = `${request.search_radius_m ?? 1200} м`;
  const isWholeCity =
    !hasPinnedPoint &&
    /^(?:|весь город|любой|все|спб|мск|москва|санкт-петербург)$/i.test(
      (request.location_query ?? "").trim()
    );

  const zoneSummary = hasPinnedPoint
    ? `Точка · ${radiusLabel}`
    : isWholeCity
      ? "Весь город"
      : request.location_query?.trim() || "Весь город";

  const areaSummary = (() => {
    const a = request.min_area_sqm;
    const b = request.max_area_sqm;
    if (a && b) return `${a}–${b} м²`;
    if (a) return `от ${a} м²`;
    if (b) return `до ${b} м²`;
    return "Площадь любая";
  })();

  const floorSummary = formatFloorsSummary(request.allowed_floors);

  const budgetSummary = (() => {
    if (request.max_price_month) {
      return `≤ ${formatNumber(Math.round(request.max_price_month / 1000))} тыс. ₽`;
    }
    if (request.max_price_sqm) {
      return `≤ ${formatNumber(request.max_price_sqm)} ₽/м²`;
    }
    return "Бюджет любой";
  })();

  const activeCityList: ReadonlyArray<{ key: CityKey; short: string; count?: number }> =
    cities.length > 0
      ? cities.map((c) => ({
          key: c.key,
          short: c.key === "moscow" ? "МСК" : "СПб",
          count: c.stats?.total
        }))
      : [
          { key: "saint_petersburg", short: "СПб" },
          { key: "moscow", short: "МСК" }
        ];

  const handleFloorChange = (val: string) => {
    onChangeFloorInput(val);
    const parsed = parseFloorsString(val);
    onChangeRequest((prev) => ({ ...prev, allowed_floors: parsed }));
  };

  return (
    <header
      ref={stripRef}
      className="relative z-[1100] h-[54px] shrink-0 bg-canvas border-b border-ink-primary px-2.5 xl:px-3 flex items-center gap-1.5 xl:gap-2"
    >
      {/* City Segmented Switch */}
      <div
        role="group"
        aria-label="Выбор города"
        className="flex border border-ink-primary rounded-[2px] overflow-hidden shrink-0"
      >
        {activeCityList.map((c) => {
          const active = request.city === c.key;
          return (
            <button
              key={c.key}
              type="button"
              onClick={() => onCitySwitch(c.key)}
              aria-pressed={active}
              title={c.count != null ? `${c.short}: ${c.count} лотов в локальной базе` : c.short}
              className={`h-[34px] px-2.5 xl:px-3 text-[12px] font-semibold tracking-wide transition-colors ${
                active
                  ? "bg-ink-primary text-[#f5f3ee]"
                  : "bg-paper text-ink-primary hover:bg-subtle"
              }`}
            >
              {c.short}
            </button>
          );
        })}
      </div>

      {/* Inline Niche Field */}
      <div className="flex items-center h-[34px] border border-hairline focus-within:border-ink-primary bg-paper rounded-[2px] px-2 xl:px-2.5 min-w-[115px] flex-1 max-w-[180px] xl:max-w-[215px]">
        <span className="font-mono text-[11px] uppercase tracking-wider text-ink-muted mr-1.5 xl:mr-2 select-none shrink-0">
          Ниша
        </span>
        <input
          type="text"
          value={request.target_business}
          onChange={(e) =>
            onChangeRequest((prev) => ({ ...prev, target_business: e.target.value }))
          }
          onKeyDown={(e) => {
            if (e.key === "Enter" && !isRunning) onRunSearch();
          }}
          placeholder="Кофейня, ПВЗ, студия..."
          aria-label="Целевой бизнес"
          className="w-full min-w-0 bg-transparent border-0 p-0 text-[13px] font-medium text-ink-primary focus:ring-0 focus:outline-none"
        />
      </div>

      {/* Popover Trigger 1: Zone / Coordinates */}
      <div className="relative">
        <button
          type="button"
          onClick={() => setOpenPopover((p) => (p === "zone" ? null : "zone"))}
          aria-expanded={openPopover === "zone"}
          title={zoneSummary}
          className={`h-[34px] px-2 xl:px-2.5 border rounded-[2px] text-[12px] flex items-center gap-1.5 transition-colors whitespace-nowrap ${
            hasPinnedPoint
              ? "border-signal bg-signal-tint text-signal font-semibold"
              : openPopover === "zone"
                ? "border-ink-primary bg-paper text-ink-primary font-semibold"
                : "border-hairline bg-paper text-ink-primary hover:border-ink-primary"
          }`}
        >
          <MapPin className="w-3.5 h-3.5 shrink-0" />
          <span className="max-w-[100px] xl:max-w-[150px] truncate">{zoneSummary}</span>
          <ChevronDown className="w-3 h-3 text-ink-muted shrink-0" />
        </button>

        {openPopover === "zone" && (
          <div className="absolute left-0 top-[40px] w-[340px] bg-paper border border-ink-primary rounded-[2px] p-3.5 z-50">
            <div className="flex items-center justify-between mb-2">
              <span className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary">
                Зона и точка отсчёта
              </span>
              <button
                type="button"
                onClick={() => setOpenPopover(null)}
                aria-label="Закрыть панель зоны"
                className="p-0.5 text-ink-muted hover:text-ink-primary"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>

            <label className="block text-[12px] font-medium text-ink-secondary mb-1">
              Район, улица или метро
            </label>
            <input
              type="text"
              value={request.location_query ?? ""}
              onChange={(e) =>
                onChangeRequest((prev) => ({
                  ...prev,
                  location_query: e.target.value
                }))
              }
              placeholder="Напр.: Петроградская или Невский"
              className="w-full h-8 px-2.5 text-[13px] bg-canvas border border-hairline focus:border-ink-primary rounded-[2px] mb-3"
            />

            <div className="border-t border-hairline pt-2.5 mb-2.5">
              <div className="flex items-center justify-between gap-2 mb-2">
                <span className="text-[12px] font-medium text-ink-secondary">
                  Привязка на карте
                </span>
                <button
                  type="button"
                  onClick={() => {
                    onTogglePinPickMode();
                    setOpenPopover(null);
                  }}
                  className={`px-2 py-1 text-[11px] font-medium border rounded-[2px] inline-flex items-center gap-1 ${
                    pinPickMode
                      ? "bg-signal text-white border-signal"
                      : "bg-canvas border-ink-primary text-ink-primary hover:bg-subtle"
                  }`}
                >
                  <Crosshair className="w-3 h-3" />
                  {pinPickMode ? "Кликните карту" : "Указать точку"}
                </button>
              </div>

              {hasPinnedPoint ? (
                <div className="bg-signal-tint border border-signal/40 px-2.5 py-2 rounded-[2px] flex items-center justify-between gap-2 text-[12px]">
                  <span className="font-mono text-ink-primary tabular-nums">
                    {request.center_lat?.toFixed(4)}, {request.center_lng?.toFixed(4)}
                  </span>
                  <button
                    type="button"
                    onClick={onClearMapCenter}
                    className="text-signal font-semibold hover:underline"
                  >
                    Сбросить
                  </button>
                </div>
              ) : (
                <p className="text-[12px] text-ink-muted">
                  Нажмите «Указать точку» или кликните любой лот на карте.
                </p>
              )}
            </div>

            <div>
              <span className="block text-[12px] font-medium text-ink-secondary mb-1.5">
                {isWholeCity
                  ? "Режим «Весь город» (охват ≈25 км; задайте радиус ниже для точки/района)"
                  : "Радиус поиска вокруг точки / района"}
              </span>
              <div className="grid grid-cols-5 gap-1">
                {RADIUS_OPTIONS.map((r) => {
                  const active = (request.search_radius_m ?? (isWholeCity ? null : 1200)) === r;
                  return (
                    <button
                      key={r}
                      type="button"
                      onClick={() =>
                        onChangeRequest((prev) => ({ ...prev, search_radius_m: r }))
                      }
                      className={`py-1 font-mono text-[11px] border rounded-[2px] tabular-nums ${
                        active
                          ? "bg-ink-primary text-[#f5f3ee] border-ink-primary font-semibold"
                          : "bg-canvas text-ink-primary border-hairline hover:border-ink-primary"
                      }`}
                    >
                      {r >= 1000 ? `${(r / 1000).toFixed(r % 1000 === 0 ? 0 : 1)}к` : `${r}м`}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Popover Trigger 2: Area + Floors + Concept */}
      <div className="relative">
        <button
          type="button"
          onClick={() => setOpenPopover((p) => (p === "space" ? null : "space"))}
          aria-expanded={openPopover === "space"}
          className={`h-[34px] px-2 xl:px-2.5 border rounded-[2px] text-[12px] flex items-center gap-1.5 transition-colors whitespace-nowrap ${
            openPopover === "space"
              ? "border-ink-primary bg-paper text-ink-primary font-semibold"
              : "border-hairline bg-paper text-ink-primary hover:border-ink-primary"
          }`}
        >
          <Ruler className="w-3.5 h-3.5 shrink-0 text-ink-secondary" />
          <span className="tabular-nums max-w-[132px] xl:max-w-none truncate">
            {areaSummary} · {floorSummary}
          </span>
          <ChevronDown className="w-3 h-3 text-ink-muted shrink-0" />
        </button>

        {openPopover === "space" && (
          <div className="absolute left-0 top-[40px] w-[390px] bg-paper border border-ink-primary rounded-[2px] p-3.5 z-50 space-y-3">
            <div className="flex items-center justify-between">
              <span className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary">
                Помещение, этаж и концепция
              </span>
              <button
                type="button"
                onClick={() => setOpenPopover(null)}
                aria-label="Закрыть параметры помещения"
                className="p-0.5 text-ink-muted hover:text-ink-primary"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>

            <div className="grid grid-cols-3 gap-2">
              <label className="block">
                <span className="text-[11px] font-medium text-ink-muted">Мин. м²</span>
                <input
                  type="number"
                  min={5}
                  value={request.min_area_sqm ?? ""}
                  onChange={(e) =>
                    onChangeRequest((p) => ({
                      ...p,
                      min_area_sqm: e.target.value ? Number(e.target.value) : null
                    }))
                  }
                  placeholder="40"
                  className="mt-1 w-full h-8 px-2 font-mono text-[12px] bg-canvas border border-hairline rounded-[2px] tabular-nums"
                />
              </label>
              <label className="block">
                <span className="text-[11px] font-medium text-ink-muted">Макс. м²</span>
                <input
                  type="number"
                  min={5}
                  value={request.max_area_sqm ?? ""}
                  onChange={(e) =>
                    onChangeRequest((p) => ({
                      ...p,
                      max_area_sqm: e.target.value ? Number(e.target.value) : null
                    }))
                  }
                  placeholder="120"
                  className="mt-1 w-full h-8 px-2 font-mono text-[12px] bg-canvas border border-hairline rounded-[2px] tabular-nums"
                />
              </label>
              <label className="block">
                <span className="text-[11px] font-medium text-ink-muted">Цель м²</span>
                <input
                  type="number"
                  min={5}
                  value={request.target_area_sqm ?? ""}
                  onChange={(e) =>
                    onChangeRequest((p) => ({
                      ...p,
                      target_area_sqm: e.target.value ? Number(e.target.value) : null
                    }))
                  }
                  placeholder="70"
                  className="mt-1 w-full h-8 px-2 font-mono text-[12px] bg-canvas border border-hairline rounded-[2px] tabular-nums"
                />
              </label>
            </div>

            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-[12px] font-medium text-ink-secondary">
                  Допустимые этажи
                </span>
                <span className="font-mono text-[11px] text-ink-muted">{floorSummary}</span>
              </div>
              <div className="flex flex-wrap gap-1 mb-2">
                {FLOOR_PRESET_CHIPS.map((preset) => {
                  const active =
                    (request.allowed_floors ?? null)?.join(",") ===
                    (preset.floors ?? null)?.join(",");
                  return (
                    <button
                      key={preset.label}
                      type="button"
                      onClick={() => handleFloorChange(preset.input)}
                      className={`px-2 py-1 text-[11px] border rounded-[2px] ${
                        active
                          ? "bg-ink-primary text-[#f5f3ee] border-ink-primary font-semibold"
                          : "bg-canvas text-ink-primary border-hairline hover:border-ink-primary"
                      }`}
                    >
                      {preset.label}
                    </button>
                  );
                })}
              </div>
              <input
                type="text"
                value={floorInput}
                onChange={(e) => handleFloorChange(e.target.value)}
                placeholder="1, 2 или цоколь (-1)"
                className="w-full h-8 px-2.5 text-[12px] font-mono bg-canvas border border-hairline rounded-[2px]"
              />
            </div>

            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-[12px] font-medium text-ink-secondary">
                  Концепция и требования
                </span>
                <button
                  type="button"
                  onClick={() => {
                    setOpenPopover(null);
                    onOpenConceptHelper();
                  }}
                  className="text-[11px] font-semibold text-signal hover:underline"
                >
                  Разобрать из текста →
                </button>
              </div>
              <textarea
                rows={3}
                value={request.business_concept}
                onChange={(e) =>
                  onChangeRequest((p) => ({ ...p, business_concept: e.target.value }))
                }
                placeholder="Опишите формат, требования ко входу с улицы, мощности или приоритеты..."
                className="w-full p-2 text-[12px] bg-canvas border border-hairline rounded-[2px] resize-y"
              />
            </div>

            <div className="space-y-1.5 pt-2 border-t border-hairline text-[12px]">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={request.include_unverified}
                  onChange={(e) =>
                    onChangeRequest((p) => ({ ...p, include_unverified: e.target.checked }))
                  }
                  className="rounded-[2px] border-ink-primary text-signal focus:ring-signal"
                />
                <span>Включать лоты без точного этажа (с пометкой)</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={request.skip_collect}
                  onChange={(e) =>
                    onChangeRequest((p) => ({ ...p, skip_collect: e.target.checked }))
                  }
                  className="rounded-[2px] border-ink-primary text-signal focus:ring-signal"
                />
                <span>Только локальная база ({dbPreviewCount} лотов, без парсинга)</span>
              </label>
            </div>
          </div>
        )}
      </div>

      {/* Popover Trigger 3: Budget */}
      <div className="relative">
        <button
          type="button"
          onClick={() => setOpenPopover((p) => (p === "budget" ? null : "budget"))}
          aria-expanded={openPopover === "budget"}
          className={`h-[34px] px-2 xl:px-2.5 border rounded-[2px] text-[12px] flex items-center gap-1.5 transition-colors whitespace-nowrap ${
            openPopover === "budget"
              ? "border-ink-primary bg-paper text-ink-primary font-semibold"
              : "border-hairline bg-paper text-ink-primary hover:border-ink-primary"
          }`}
        >
          <Wallet className="w-3.5 h-3.5 shrink-0 text-ink-secondary" />
          <span className="tabular-nums">{budgetSummary}</span>
          <ChevronDown className="w-3 h-3 text-ink-muted shrink-0" />
        </button>

        {openPopover === "budget" && (
          <div className="absolute left-0 top-[40px] w-[320px] bg-paper border border-ink-primary rounded-[2px] p-3.5 z-50 space-y-3">
            <div className="flex items-center justify-between">
              <span className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary">
                Финансовые ограничения
              </span>
              <button
                type="button"
                onClick={() => setOpenPopover(null)}
                aria-label="Закрыть настройки бюджета"
                className="p-0.5 text-ink-muted hover:text-ink-primary"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>

            <label className="block">
              <span className="text-[12px] font-medium text-ink-secondary">
                Макс. аренда, ₽/мес
              </span>
              <input
                type="number"
                step={5000}
                value={request.max_price_month ?? ""}
                onChange={(e) =>
                  onChangeRequest((p) => ({
                    ...p,
                    max_price_month: e.target.value ? Number(e.target.value) : null
                  }))
                }
                placeholder="Напр.: 150000"
                className="mt-1 w-full h-8 px-2.5 font-mono text-[12px] bg-canvas border border-hairline rounded-[2px] tabular-nums"
              />
            </label>

            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-[12px] font-medium text-ink-secondary">
                  Ставка за м²
                </span>
                <div className="flex border border-hairline rounded-[2px] text-[11px] overflow-hidden">
                  <button
                    type="button"
                    onClick={() =>
                      onChangeRequest((p) => ({ ...p, price_sqm_period: "month" }))
                    }
                    className={`px-2 py-0.5 ${
                      request.price_sqm_period === "month"
                        ? "bg-ink-primary text-[#f5f3ee] font-semibold"
                        : "bg-canvas text-ink-secondary"
                    }`}
                  >
                    ₽/мес
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      onChangeRequest((p) => ({ ...p, price_sqm_period: "year" }))
                    }
                    className={`px-2 py-0.5 ${
                      request.price_sqm_period === "year"
                        ? "bg-ink-primary text-[#f5f3ee] font-semibold"
                        : "bg-canvas text-ink-secondary"
                    }`}
                  >
                    ₽/год
                  </button>
                </div>
              </div>
              <input
                type="number"
                step={100}
                value={request.max_price_sqm ?? ""}
                onChange={(e) =>
                  onChangeRequest((p) => ({
                    ...p,
                    max_price_sqm: e.target.value ? Number(e.target.value) : null
                  }))
                }
                placeholder="Не ограничено"
                className="w-full h-8 px-2.5 font-mono text-[12px] bg-canvas border border-hairline rounded-[2px] tabular-nums"
              />
            </div>

            <label className="block">
              <span className="text-[12px] font-medium text-ink-secondary">
                Потолок полных расходов, ₽/мес
              </span>
              <input
                type="number"
                step={10000}
                value={request.full_cost_limit ?? ""}
                onChange={(e) =>
                  onChangeRequest((p) => ({
                    ...p,
                    full_cost_limit: e.target.value ? Number(e.target.value) : null
                  }))
                }
                placeholder="Опционально (аренда + ремонт/ФОТ)"
                className="mt-1 w-full h-8 px-2.5 font-mono text-[12px] bg-canvas border border-hairline rounded-[2px] tabular-nums"
              />
            </label>
          </div>
        )}
      </div>

      {/* Inline Pool Depth Chips */}
      <div
        role="group"
        aria-label="Сколько лучших лотов оценить детально"
        title="Сколько лучших подходящих объектов проанализировать по окружению, трафику и ФНС"
        className="hidden min-[1060px]:flex items-center border border-hairline bg-paper rounded-[2px] px-1.5 h-[34px] gap-0.5 shrink-0"
      >
        <SlidersHorizontal className="w-3 h-3 text-ink-muted mr-0.5 hidden xl:inline" />
        <span className="text-[11px] font-mono text-ink-muted mr-0.5 hidden 2xl:inline">Оценка:</span>
        {POOL_OPTIONS.map((opt) => {
          const active = request.analysis_limit === opt.value;
          return (
            <button
              key={opt.label}
              type="button"
              onClick={() =>
                onChangeRequest((p) => ({
                  ...p,
                  analysis_limit: opt.value,
                  max_candidates: opt.value ? Math.max(opt.value, p.max_candidates) : 50
                }))
              }
              aria-pressed={active}
              className={`px-1.5 py-0.5 font-mono text-[11px] rounded-[2px] tabular-nums transition-colors ${
                active
                  ? "bg-ink-primary text-[#f5f3ee] font-semibold"
                  : "text-ink-secondary hover:bg-subtle"
              }`}
            >
              {opt.label}
            </button>
          );
        })}
      </div>

      {/* Frameless Window Drag Strip (between filters and CTA; interactive controls sit safely outside) */}
      <div
        onDoubleClick={() => {
          if (isDesktopShell && onToggleMaximizeWindow) {
            onToggleMaximizeWindow();
          }
        }}
        className={`flex-1 min-w-[8px] h-full ${
          isDesktopShell ? "pywebview-drag-region cursor-grab active:cursor-grabbing" : ""
        }`}
        aria-hidden="true"
      />

      {/* Right Action Cluster */}
      <div className="flex items-center gap-1.5 xl:gap-2 shrink-0">
        {hasReport && (
          <div className="relative">
            <button
              type="button"
              title="Скачать отчёт (Markdown / JSON)"
              aria-label="Экспорт отчёта"
              onClick={() => setOpenPopover((p) => (p === "export" ? null : "export"))}
              className="h-[34px] px-2 xl:px-2.5 border border-ink-primary bg-paper hover:bg-subtle rounded-[2px] text-[12px] font-semibold text-ink-primary inline-flex items-center gap-1.5"
            >
              <Download className="w-3.5 h-3.5" />
              <span className="hidden xl:inline">Экспорт</span>
            </button>
            {openPopover === "export" && (
              <div className="absolute right-0 top-[40px] w-44 bg-paper border border-ink-primary rounded-[2px] p-1 z-50">
                <button
                  type="button"
                  onClick={() => {
                    onExport("markdown");
                    setOpenPopover(null);
                  }}
                  className="w-full text-left px-2.5 py-1.5 text-[12px] hover:bg-subtle rounded-[2px]"
                >
                  Отчёт Markdown (.md)
                </button>
                <button
                  type="button"
                  onClick={() => {
                    onExport("json");
                    setOpenPopover(null);
                  }}
                  className="w-full text-left px-2.5 py-1.5 text-[12px] font-mono hover:bg-subtle rounded-[2px]"
                >
                  Данные JSON (.json)
                </button>
              </div>
            )}
          </div>
        )}

        <div className="flex items-center shrink-0">
          <button
            type="button"
            onClick={onRunSearch}
            disabled={isRunning || !request.target_business.trim()}
            className={`h-[34px] px-3 xl:px-4 text-[13px] font-semibold inline-flex items-center gap-1.5 xl:gap-2 transition-colors whitespace-nowrap ${
              isRunning && onCancelSearch
                ? "bg-ink-primary text-[#f5f3ee] cursor-wait rounded-l-[2px]"
                : isRunning
                  ? "bg-ink-primary text-[#f5f3ee] cursor-wait rounded-[2px]"
                  : "bg-signal hover:bg-signal-hover text-[#fffdf9] disabled:opacity-50 rounded-[2px]"
            }`}
          >
            <Play className="w-3.5 h-3.5 fill-current" />
            <span>
              {isRunning ? (
                `Расчёт · ${elapsedSec}с`
              ) : hasPinnedPoint ? (
                <>
                  <span>Подобрать</span>
                  <span className="hidden xl:inline"> в точке</span>
                </>
              ) : (
                "Подобрать"
              )}
            </span>
          </button>

          {isRunning && onCancelSearch && (
            <button
              type="button"
              onClick={onCancelSearch}
              title="Остановить текущий расчёт"
              aria-label="Остановить расчёт"
              className="h-[34px] px-2 bg-[#2c313a] hover:bg-signal text-[#f5f3ee] border-l border-[#404652] rounded-r-[2px] inline-flex items-center justify-center transition-colors"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>

        {isDesktopShell && (
          <div
            role="group"
            aria-label="Управление окном"
            className="flex items-center gap-0.5 pl-2 ml-0.5 border-l border-hairline shrink-0"
          >
            <button
              type="button"
              onClick={onMinimizeWindow}
              title="Свернуть"
              aria-label="Свернуть окно"
              className="w-7 h-7 flex items-center justify-center rounded-[2px] text-ink-secondary hover:bg-subtle hover:text-ink-primary transition-colors"
            >
              <Minus className="w-3.5 h-3.5" />
            </button>
            <button
              type="button"
              onClick={onToggleMaximizeWindow}
              title={isMaximized ? "Восстановить размер" : "Развернуть"}
              aria-label={isMaximized ? "Восстановить размер окна" : "Развернуть окно"}
              className="w-7 h-7 flex items-center justify-center rounded-[2px] text-ink-secondary hover:bg-subtle hover:text-ink-primary transition-colors"
            >
              {isMaximized ? (
                <Copy className="w-3 h-3" />
              ) : (
                <Square className="w-3 h-3" />
              )}
            </button>
            <button
              type="button"
              onClick={onCloseWindow}
              title="Закрыть Reper"
              aria-label="Закрыть окно"
              className="w-7 h-7 flex items-center justify-center rounded-[2px] text-ink-secondary hover:bg-signal hover:text-[#fffdf9] transition-colors"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        )}
      </div>
    </header>
  );
}
