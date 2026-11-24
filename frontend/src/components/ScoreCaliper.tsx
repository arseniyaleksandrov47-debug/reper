import type { ConfirmationData } from "../types/geoestate";

export interface ScoreCaliperProps {
  readonly minScore: number;
  readonly maxScore: number;
  readonly coverage: number;
  readonly selected?: boolean;
  readonly confirmation?: ConfirmationData | null;
  readonly compact?: boolean;
}

/** Architectural calibrated score & uncertainty caliper ([min --●-- max / 100]). */
export function ScoreCaliper({
  minScore,
  maxScore,
  coverage,
  selected = false,
  confirmation,
  compact = false
}: ScoreCaliperProps) {
  const lo = Math.max(0, Math.min(100, minScore));
  const hi = Math.max(lo, Math.min(100, maxScore));
  const spread = Math.max(1.5, hi - lo);
  const mid = (lo + hi) / 2;
  const covPct = Math.round(Math.max(0, Math.min(1, coverage)) * 100);
  const status = confirmation?.status ?? "requires_confirmation";

  const isVerified =
    status === "source_conditions_checked" || status === "verified";
  const isRejected =
    status === "not_suitable" || status === "rejected";

  const statusLabel = isVerified
    ? "Допуск ок"
    : isRejected
      ? "Не допущен"
      : "Уточнить доступ";

  const statusColor = isVerified
    ? "text-verified border-verified/40 bg-[#eef7f1]"
    : isRejected
      ? "text-danger border-danger/40 bg-[#fef1f1]"
      : "text-warn border-warn/50 bg-hatch-warn";

  const loText = lo.toFixed(1).replace(".", ",");
  const hiText = hi.toFixed(1).replace(".", ",");
  const rangeText = Math.abs(hi - lo) >= 0.05 ? `${loText}–${hiText}` : loText;

  return (
    <div className={compact ? "space-y-1.5" : "space-y-2"}>
      <div
        className={`flex items-center justify-between gap-x-2 gap-y-1 ${
          compact ? "flex-wrap text-[11px]" : "text-[12px]"
        }`}
      >
        <div className="flex items-baseline gap-1.5 min-w-0 whitespace-nowrap">
          <span
            className={`font-mono font-bold tabular-nums tracking-tight whitespace-nowrap ${
              selected ? "text-signal" : "text-ink-primary"
            }`}
          >
            {rangeText}
            <span className="text-ink-muted font-normal"> / 100</span>
          </span>
          <span
            className={`font-mono text-ink-muted tabular-nums whitespace-nowrap ${
              compact ? "text-[11px]" : "text-[12px]"
            }`}
          >
            · {covPct}%{compact ? "" : " факторов"}
          </span>
        </div>
        {confirmation && (
          <span
            className={`px-1.5 py-0.5 text-[11px] font-medium border rounded-[2px] whitespace-nowrap ${statusColor}`}
          >
            {compact && !isVerified && !isRejected ? "Уточнить" : statusLabel}
          </span>
        )}
      </div>

      {/* Drafting Caliper Rail */}
      <div
        className="relative h-2 bg-subtle border border-hairline rounded-[1px]"
        aria-label={`Оценка от ${loText} до ${hiText} из 100, покрытие факторов ${covPct}%`}
      >
        {/* Quarter reference ticks */}
        <span className="absolute left-1/4 top-0 bottom-0 w-px bg-hairline/80" />
        <span className="absolute left-1/2 top-0 bottom-0 w-px bg-hairline/80" />
        <span className="absolute left-3/4 top-0 bottom-0 w-px bg-hairline/80" />

        {/* Baseline fill up to guaranteed floor */}
        <div
          className={`absolute left-0 top-0 bottom-0 transition-all duration-200 ${
            selected ? "bg-ink-primary" : "bg-[#343942]"
          }`}
          style={{ width: `${lo}%` }}
        />

        {/* Uncertainty interval band (hatched if upper differs cleanly from lower) */}
        {hi - lo > 0.2 && (
          <div
            className="absolute top-0 bottom-0 transition-all duration-200"
            style={{
              left: `${lo}%`,
              width: `${Math.min(100 - lo, spread)}%`,
              backgroundColor: selected ? "rgba(209, 59, 39, 0.25)" : "rgba(24, 27, 31, 0.22)",
              backgroundImage:
                "repeating-linear-gradient(-45deg, transparent, transparent 2px, rgba(24,27,31,0.28) 2px, rgba(24,27,31,0.28) 3px)"
            }}
          />
        )}

        {/* Active midpoint needle */}
        <div
          className={`absolute top-[-2px] bottom-[-2px] w-[2px] ${
            selected ? "bg-signal" : "bg-ink-primary"
          }`}
          style={{ left: `calc(${mid}% - 1px)` }}
        />
      </div>
    </div>
  );
}
