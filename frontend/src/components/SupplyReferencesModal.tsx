import { Sliders, X } from "lucide-react";
import { useEffect, useId, useState } from "react";

export interface SupplyReferencesModalProps {
  readonly open: boolean;
  readonly initialCostLimit: number | null;
  readonly onClose: () => void;
  readonly onSubmit: (fullCostLimit: number) => void;
}

export function SupplyReferencesModal({
  open,
  initialCostLimit,
  onClose,
  onSubmit
}: SupplyReferencesModalProps) {
  const [costLimit, setCostLimit] = useState<string>(
    initialCostLimit != null ? String(initialCostLimit) : "240000"
  );
  const uid = useId();

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const num = Number(costLimit.replace(/\s+/g, "").replace(",", "."));
    if (!Number.isFinite(num) || num <= 0) return;
    onSubmit(num);
    onClose();
  };

  return (
    <div
      className="fixed inset-0 z-[1100] flex items-center justify-center bg-[#181b1f]/60 p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${uid}-title`}
        className="w-full max-w-md bg-paper border border-ink-primary rounded-[2px] overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-4 py-3 border-b border-ink-primary bg-canvas flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Sliders className="w-4 h-4 text-signal" />
            <h2 id={`${uid}-title`} className="font-bold text-[14px] text-ink-primary">
              Уточнить потолок общих затрат
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Закрыть окно"
            className="p-1 text-ink-muted hover:text-ink-primary"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-4 space-y-4 text-[12px]">
          <p className="text-ink-secondary leading-relaxed">
            Для полного пересчёта скоринга по стоимости помещения (аренда + коммунальные
            платежи и эксплуатация в месяц) задайте допустимый потолок в рублях.
          </p>

          <div className="space-y-1.5">
            <label htmlFor={`${uid}-cost`} className="font-semibold text-ink-primary block">
              Максимум полных затрат на помещение (₽/мес)
            </label>
            <input
              id={`${uid}-cost`}
              name="full_cost_limit"
              type="number"
              min={10000}
              step={5000}
              required
              autoComplete="off"
              value={costLimit}
              onChange={(e) => setCostLimit(e.target.value)}
              className="w-full h-9 px-3 font-mono text-[14px] font-bold bg-canvas border border-ink-primary rounded-[2px] text-ink-primary tabular-nums"
            />
          </div>

          <div className="flex items-center justify-end gap-2 pt-2 border-t border-hairline">
            <button
              type="button"
              onClick={onClose}
              className="h-9 px-3 border border-hairline bg-canvas hover:border-ink-primary rounded-[2px] text-[12px] font-medium"
            >
              Отмена
            </button>
            <button
              type="submit"
              className="h-9 px-4 bg-signal hover:bg-signal-hover text-[#fffdf9] rounded-[2px] text-[12px] font-semibold"
            >
              Пересчитать
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
