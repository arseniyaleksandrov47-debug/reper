import { useEffect, useState } from "react";
import { History, Plus, Sparkles, X } from "lucide-react";
import type { CityKey, SearchRequest } from "../types/geoestate";
import {
  ACCESS_OPTIONS,
  applyDraftToRequest,
  buildDraftFromBusinessText,
  extractAccessPreferenceFromConcept,
  extractEmphasisFromConcept,
  extractPrioritiesFromConcept,
  stripGeneratedConceptClauses,
  type OnboardingDraft
} from "../utils/conceptParser";
import {
  draftFromRecentSearch,
  type RecentSearchEntry
} from "../utils/recentSearches";

export interface ConceptHelperModalProps {
  readonly open: boolean;
  readonly currentRequest: SearchRequest;
  readonly currentFloorInput: string;
  readonly recentSearches: readonly RecentSearchEntry[];
  readonly onApply: (
    req: SearchRequest,
    newFloorInput: string,
    sourceFreeText?: string
  ) => void;
  readonly onClearRecentSearches?: () => void;
  readonly onClose: () => void;
}

/** On-demand free-text business parser modal (never blocks first launch). */
export function ConceptHelperModal({
  open,
  currentRequest,
  currentFloorInput,
  recentSearches,
  onApply,
  onClearRecentSearches,
  onClose
}: ConceptHelperModalProps) {
  const [freeText, setFreeText] = useState("");
  const [customPriorityInput, setCustomPriorityInput] = useState("");
  const [restoredAnchor, setRestoredAnchor] = useState<RecentSearchEntry | null>(null);
  const [draft, setDraft] = useState<OnboardingDraft>(() => {
    const rawConcept = currentRequest.business_concept || "";
    return {
      city: currentRequest.city,
      businessText: stripGeneratedConceptClauses(rawConcept),
      targetBusiness: currentRequest.target_business,
      locationQuery: currentRequest.location_query || "весь город",
      floorInput: currentFloorInput,
      accessPreference: extractAccessPreferenceFromConcept(rawConcept),
      minAreaSqm: currentRequest.min_area_sqm,
      maxAreaSqm: currentRequest.max_area_sqm,
      maxPriceMonth: currentRequest.max_price_month,
      fullCostLimit: currentRequest.full_cost_limit,
      priorities: extractPrioritiesFromConcept(rawConcept),
      emphasisText: extractEmphasisFromConcept(rawConcept)
    };
  });

  // Sync draft state from toolbar filters cleanly each time modal opens
  useEffect(() => {
    if (!open) return;
    const rawConcept = currentRequest.business_concept || "";
    const cleanBody = stripGeneratedConceptClauses(rawConcept);
    setFreeText(cleanBody);
    setCustomPriorityInput("");
    setRestoredAnchor(null);
    setDraft({
      city: currentRequest.city,
      businessText: cleanBody,
      targetBusiness: currentRequest.target_business,
      locationQuery: currentRequest.location_query || "весь город",
      floorInput: currentFloorInput,
      accessPreference: extractAccessPreferenceFromConcept(rawConcept),
      minAreaSqm: currentRequest.min_area_sqm,
      maxAreaSqm: currentRequest.max_area_sqm,
      maxPriceMonth: currentRequest.max_price_month,
      fullCostLimit: currentRequest.full_cost_limit,
      priorities: extractPrioritiesFromConcept(rawConcept),
      emphasisText: extractEmphasisFromConcept(rawConcept)
    });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const parseTextNow = (text: string) => {
    setFreeText(text);
    setDraft((prev) => {
      const parsed = buildDraftFromBusinessText(currentRequest, text, prev.city);
      return {
        ...parsed,
        priorities: prev.priorities,
        emphasisText: prev.emphasisText
      };
    });
  };

  const restoreRecentSearch = (entry: RecentSearchEntry) => {
    const restored = draftFromRecentSearch(entry);
    setFreeText(restored.businessText || entry.freeText || entry.label);
    setDraft(restored);
    setRestoredAnchor(entry);
    setCustomPriorityInput("");
  };

  const addCustomPriorities = (rawValue: string) => {
    const parts = rawValue
      .split(/[,;\n]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (parts.length === 0) return;
    setDraft((prev) => ({
      ...prev,
      priorities: Array.from(new Set([...prev.priorities, ...parts]))
    }));
    setCustomPriorityInput("");
  };

  const removePriority = (label: string) => {
    setDraft((prev) => ({
      ...prev,
      priorities: prev.priorities.filter((x) => x !== label)
    }));
  };

  const handleConfirm = () => {
    let nextReq = applyDraftToRequest(currentRequest, draft, customPriorityInput);
    if (
      restoredAnchor &&
      restoredAnchor.center_lat != null &&
      restoredAnchor.center_lng != null &&
      restoredAnchor.city === draft.city &&
      restoredAnchor.location_query.trim().toLowerCase() ===
        (draft.locationQuery.trim() || "весь город").toLowerCase()
    ) {
      nextReq = {
        ...nextReq,
        center_lat: restoredAnchor.center_lat,
        center_lng: restoredAnchor.center_lng,
        search_radius_m: restoredAnchor.search_radius_m
      };
    }
    onApply(nextReq, draft.floorInput, freeText);
    onClose();
  };

  const shownRecent = recentSearches.slice(0, 3);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="concept-helper-title"
      onClick={onClose}
      className="fixed inset-0 z-[1200] bg-[#181b1f]/55 flex items-center justify-center p-4"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-[680px] bg-paper border border-ink-primary rounded-[2px] flex flex-col max-h-[90vh] overflow-hidden"
      >
        <div className="px-4 py-3 border-b border-ink-primary bg-canvas flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Sparkles className="w-4 h-4 text-signal" />
            <h2 id="concept-helper-title" className="text-[15px] font-bold text-ink-primary">
              Разбор параметров из свободного описания бизнеса
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Закрыть"
            className="p-1 text-ink-muted hover:text-ink-primary"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4 overflow-y-auto space-y-4">
          <div>
            <label className="block text-[12px] font-semibold text-ink-secondary mb-1.5">
              Опишите задачу своими словами (город, формат, площадь, бюджет, этаж)
            </label>
            <textarea
              rows={3}
              value={freeText}
              onChange={(e) => parseTextNow(e.target.value)}
              placeholder="Например: Кофейня спешелти 45–80 м² на 1-м этаже у метро Чкаловская, бюджет до 120 000 ₽..."
              className="w-full p-2.5 text-[13px] bg-canvas border border-ink-primary rounded-[2px]"
            />

            <div className="mt-2.5">
              <div className="flex items-center justify-between gap-2 mb-1.5">
                <span className="inline-flex items-center gap-1.5 text-[11px] font-mono uppercase tracking-wider text-ink-muted">
                  <History className="w-3 h-3" />
                  Последние поиски ({shownRecent.length}/3)
                </span>
                {shownRecent.length > 0 && onClearRecentSearches && (
                  <button
                    type="button"
                    onClick={onClearRecentSearches}
                    className="text-[11px] text-ink-muted hover:text-danger transition-colors"
                  >
                    Очистить
                  </button>
                )}
              </div>

              {shownRecent.length > 0 ? (
                <div className="flex flex-col gap-1.5">
                  {shownRecent.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => restoreRecentSearch(item)}
                      title={item.freeText || item.label}
                      className="text-left px-2.5 py-1.5 text-[12px] border border-hairline bg-canvas hover:border-ink-primary hover:bg-paper rounded-[2px] text-ink-primary truncate transition-colors"
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
              ) : (
                <p className="text-[11px] text-ink-muted">
                  После запуска подбора здесь появятся до 3 ваших последних запросов для быстрого повтора.
                </p>
              )}
            </div>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-6 gap-2.5 pt-3 border-t border-hairline">
            <label className="block sm:col-span-2">
              <span className="text-[11px] font-medium text-ink-muted">Город</span>
              <select
                value={draft.city}
                onChange={(e) =>
                  setDraft((p) => ({ ...p, city: e.target.value as CityKey }))
                }
                className="mt-1 w-full h-9 px-2.5 py-0 text-[12px] text-ink-primary bg-canvas border border-hairline focus:border-ink-primary rounded-[2px]"
              >
                <option value="saint_petersburg">Санкт-Петербург</option>
                <option value="moscow">Москва</option>
              </select>
            </label>

            <label className="block col-span-2 sm:col-span-3">
              <span className="text-[11px] font-medium text-ink-muted">Ниша</span>
              <input
                type="text"
                value={draft.targetBusiness}
                onChange={(e) =>
                  setDraft((p) => ({ ...p, targetBusiness: e.target.value }))
                }
                className="mt-1 w-full h-9 px-2.5 py-0 text-[12px] text-ink-primary bg-canvas border border-hairline focus:border-ink-primary rounded-[2px]"
              />
            </label>

            <label className="block sm:col-span-1">
              <span className="text-[11px] font-medium text-ink-muted">Этажи</span>
              <input
                type="text"
                value={draft.floorInput}
                onChange={(e) =>
                  setDraft((p) => ({ ...p, floorInput: e.target.value }))
                }
                placeholder="1"
                className="mt-1 w-full h-9 px-2.5 py-0 font-mono text-[12px] text-ink-primary bg-canvas border border-hairline focus:border-ink-primary rounded-[2px]"
              />
            </label>

            <label className="block col-span-2 sm:col-span-2">
              <span className="text-[11px] font-medium text-ink-muted">Локация</span>
              <input
                type="text"
                value={draft.locationQuery}
                onChange={(e) =>
                  setDraft((p) => ({ ...p, locationQuery: e.target.value }))
                }
                className="mt-1 w-full h-9 px-2.5 py-0 text-[12px] text-ink-primary bg-canvas border border-hairline focus:border-ink-primary rounded-[2px]"
              />
            </label>

            <label className="block sm:col-span-1">
              <span className="text-[11px] font-medium text-ink-muted">От, м²</span>
              <input
                type="number"
                value={draft.minAreaSqm ?? ""}
                onChange={(e) =>
                  setDraft((p) => ({
                    ...p,
                    minAreaSqm: e.target.value ? Number(e.target.value) : null
                  }))
                }
                placeholder="20"
                className="mt-1 w-full h-9 px-2.5 py-0 font-mono text-[12px] text-ink-primary bg-canvas border border-hairline focus:border-ink-primary rounded-[2px] tabular-nums"
              />
            </label>

            <label className="block sm:col-span-1">
              <span className="text-[11px] font-medium text-ink-muted">До, м²</span>
              <input
                type="number"
                value={draft.maxAreaSqm ?? ""}
                onChange={(e) =>
                  setDraft((p) => ({
                    ...p,
                    maxAreaSqm: e.target.value ? Number(e.target.value) : null
                  }))
                }
                className="mt-1 w-full h-9 px-2.5 py-0 font-mono text-[12px] text-ink-primary bg-canvas border border-hairline focus:border-ink-primary rounded-[2px] tabular-nums"
              />
            </label>

            <label className="block col-span-2 sm:col-span-2">
              <span className="text-[11px] font-medium text-ink-muted">Аренда до, ₽</span>
              <input
                type="number"
                value={draft.maxPriceMonth ?? ""}
                onChange={(e) =>
                  setDraft((p) => ({
                    ...p,
                    maxPriceMonth: e.target.value ? Number(e.target.value) : null
                  }))
                }
                className="mt-1 w-full h-9 px-2.5 py-0 font-mono text-[12px] text-ink-primary bg-canvas border border-hairline focus:border-ink-primary rounded-[2px] tabular-nums"
              />
            </label>
          </div>

          <div>
            <span className="block text-[12px] font-semibold text-ink-secondary mb-1.5">
              Доступ в помещение
            </span>
            <div className="flex flex-wrap gap-1.5">
              {ACCESS_OPTIONS.map((opt) => {
                const active = draft.accessPreference === opt.key;
                return (
                  <button
                    key={opt.key}
                    type="button"
                    onClick={() =>
                      setDraft((p) => ({ ...p, accessPreference: opt.key }))
                    }
                    className={`px-2.5 py-1 text-[12px] border rounded-[2px] ${
                      active
                        ? "bg-ink-primary text-[#f5f3ee] border-ink-primary font-semibold"
                        : "bg-canvas text-ink-primary border-hairline hover:border-ink-primary"
                    }`}
                  >
                    {opt.label}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="space-y-2">
            <label
              htmlFor="custom-priority-input"
              className="block text-[12px] font-semibold text-ink-secondary"
            >
              Приоритетные характеристики при скоринге (введите свои)
            </label>

            <div className="flex items-center gap-2">
              <input
                id="custom-priority-input"
                type="text"
                value={customPriorityInput}
                onChange={(e) => setCustomPriorityInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    addCustomPriorities(customPriorityInput);
                  }
                }}
                placeholder="Напр.: витринные окна, высокий трафик от метро, мощность 15 кВт..."
                className="flex-1 min-w-0 h-9 px-2.5 py-0 text-[12px] text-ink-primary bg-canvas border border-hairline focus:border-ink-primary rounded-[2px]"
              />
              <button
                type="button"
                onClick={() => addCustomPriorities(customPriorityInput)}
                disabled={!customPriorityInput.trim()}
                className="h-9 px-3 border border-ink-primary bg-paper hover:bg-ink-primary hover:text-[#f5f3ee] disabled:opacity-45 disabled:hover:bg-paper disabled:hover:text-ink-primary text-[12px] font-semibold rounded-[2px] inline-flex items-center gap-1 transition-colors shrink-0"
              >
                <Plus className="w-3.5 h-3.5" />
                <span>Добавить</span>
              </button>
            </div>

            {draft.priorities.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {draft.priorities.map((p) => (
                  <span
                    key={p}
                    className="inline-flex items-center gap-1.5 pl-2.5 pr-1.5 py-1 text-[12px] bg-signal-tint text-signal border border-signal rounded-[2px] font-medium"
                  >
                    <span>{p}</span>
                    <button
                      type="button"
                      onClick={() => removePriority(p)}
                      aria-label={`Удалить приоритет ${p}`}
                      className="p-0.5 text-signal hover:text-ink-primary"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}

            <div>
              <input
                type="text"
                value={draft.emphasisText}
                onChange={(e) =>
                  setDraft((p) => ({ ...p, emphasisText: e.target.value }))
                }
                aria-label="Свободный акцент для расчёта"
                placeholder="Доп. акцент для скоринга (необязательно): что важнее всего при сравнении..."
                className="w-full h-9 px-2.5 py-0 text-[12px] text-ink-primary bg-canvas border border-hairline focus:border-ink-primary rounded-[2px]"
              />
            </div>
          </div>
        </div>

        <div className="px-4 py-3 border-t border-hairline bg-canvas flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="h-9 px-3 border border-hairline bg-paper text-[12px] font-medium rounded-[2px] hover:border-ink-primary"
          >
            Отмена
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            className="h-9 px-4 bg-signal hover:bg-signal-hover text-[#fffdf9] text-[13px] font-semibold rounded-[2px]"
          >
            Применить параметры в панель
          </button>
        </div>
      </div>
    </div>
  );
}
