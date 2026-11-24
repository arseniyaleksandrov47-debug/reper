import { useEffect, useRef } from "react";
import { RefreshCw } from "lucide-react";
import type { CollectResult, JobState, SearchReport } from "../types/geoestate";

export interface PipelineLogsViewProps {
  readonly searchJob: JobState<SearchReport> | null;
  readonly collectJob: JobState<CollectResult> | null;
  readonly report: SearchReport | null;
  readonly cityName: string;
  readonly onRunCollect: (limitPerSource: number) => void;
}

const STAGE_LABELS: Record<string, string> = {
  concept_profile: "Профиль концепции и весов",
  unit_details: "Обогащение карточек и фото",
  mandatory_checks: "Проверка фильтров, этажа и допуска",
  environment: "Окружение, метро и конкуренты",
  company_accounts: "Бухгалтерия и выручка ФНС (DaData)",
  scoring: "Скоринг и интервалы достоверности",
  model_sensitivity: "Анализ чувствительности весов",
  confirmation: "Финальная верификация условий",
  filter_db: "Фильтрация и отбор БД",
  collect: "Сбор объявлений",
  geocode: "Геокодирование",
  traffic_and_population: "Трафик и жилой фонд",
  fns: "Проверка ФНС / DaData"
};

export function PipelineLogsView({
  searchJob,
  collectJob,
  report,
  cityName,
  onRunCollect
}: PipelineLogsViewProps) {
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const logs = searchJob?.logs ?? [];
  const collectLogs = collectJob?.logs ?? [];

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "auto" });
  }, [logs.length, collectLogs.length]);

  const timings = report?.stage_timings_ms
    ? Object.entries(report.stage_timings_ms)
    : [];
  const maxTiming = timings.reduce((acc, [, v]) => Math.max(acc, v), 1);

  return (
    <div className="h-full bg-canvas overflow-y-auto p-5 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-ink-primary pb-3">
        <div>
          <h1 className="text-[18px] font-bold text-ink-primary tracking-tight">
            Журнал прогона и диагностика конвейера
          </h1>
          <p className="text-[12px] text-ink-secondary mt-0.5">
            Реальное время этапов геоанализа, тайминги стадий и ручная догрузка базы ({cityName}).
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={collectJob?.status === "running"}
            onClick={() => onRunCollect(120)}
            className="h-8 px-3 border border-ink-primary bg-paper hover:bg-subtle disabled:opacity-50 text-[12px] font-semibold rounded-[2px] inline-flex items-center gap-1.5"
          >
            <RefreshCw
              className={`w-3.5 h-3.5 ${
                collectJob?.status === "running" ? "animate-spin text-signal" : ""
              }`}
            />
            <span>
              {collectJob?.status === "running"
                ? "Сбор объявлений..."
                : `Догрузить базу ${cityName}`}
            </span>
          </button>
        </div>
      </div>

      {/* Status + Timings Row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <section className="bg-paper border border-hairline rounded-[2px] p-4 space-y-2">
          <h2 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary">
            Состояние последнего расчёта
          </h2>
          {searchJob ? (
            <div className="space-y-1.5 text-[13px]">
              <div className="flex justify-between">
                <span className="text-ink-muted">Статус:</span>
                <strong
                  className={`font-mono ${
                    searchJob.status === "failed"
                      ? "text-danger"
                      : searchJob.status === "cancelled"
                        ? "text-warn"
                        : searchJob.status === "running"
                          ? "text-signal"
                          : "text-verified"
                  }`}
                >
                  {searchJob.status === "running"
                    ? "Выполняется"
                    : searchJob.status === "failed"
                      ? "Ошибка"
                      : searchJob.status === "cancelled"
                        ? "Остановлено"
                        : "Завершено"}
                </strong>
              </div>
              <div className="flex justify-between">
                <span className="text-ink-muted">Текущий этап:</span>
                <strong className="text-ink-primary">{searchJob.stage || "—"}</strong>
              </div>
              {report && (
                <div className="flex justify-between">
                  <span className="text-ink-muted">Общее время:</span>
                  <strong className="font-mono tabular-nums">
                    {(report.total_search_time_ms / 1000).toFixed(2)} с
                  </strong>
                </div>
              )}
              {searchJob.error && (
                <div className="mt-2 p-2.5 bg-[#fef1f1] border border-danger/40 text-danger text-[12px] rounded-[2px]">
                  {searchJob.error}
                </div>
              )}
            </div>
          ) : (
            <p className="text-[13px] text-ink-muted">
              Прогон в этой сессии ещё не запускался.
            </p>
          )}
        </section>

        <section className="bg-paper border border-hairline rounded-[2px] p-4 space-y-2.5">
          <h2 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary">
            Тайминги стадий (мс)
          </h2>
          {timings.length === 0 ? (
            <p className="text-[13px] text-ink-muted">Тайминги появятся после завершения подбора.</p>
          ) : (
            <div className="space-y-2">
              {timings.map(([k, ms]) => {
                const pct = Math.max(3, Math.round((ms / maxTiming) * 100));
                return (
                  <div key={k} className="space-y-1 text-[12px]">
                    <div className="flex justify-between">
                      <span className="text-ink-primary">{STAGE_LABELS[k] || k}</span>
                      <span className="font-mono text-ink-muted tabular-nums">{ms} мс</span>
                    </div>
                    <div className="h-1.5 bg-subtle border border-hairline">
                      <div className="h-full bg-ink-primary" style={{ width: `${pct}%` }} />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>
      </div>

      {/* Warnings if any */}
      {report && report.ranking_warnings.length > 0 && (
        <section className="bg-hatch-warn border border-warn/60 rounded-[2px] p-4 space-y-1.5 text-[12px]">
          <h3 className="font-mono text-[11px] font-bold uppercase tracking-wider text-warn">
            Предупреждения отбора и полноты данных
          </h3>
          {report.ranking_warnings.map((w, i) => (
            <p key={i} className="text-ink-primary">
              · {w}
            </p>
          ))}
        </section>
      )}

      {/* Raw Monospace Stream */}
      <section className="bg-[#181b1f] text-[#f5f3ee] border border-ink-primary rounded-[2px] overflow-hidden">
        <div className="px-4 py-2 border-b border-[#2c313a] font-mono text-[11px] uppercase tracking-wider text-[#adb4bf] flex justify-between">
          <span>Поток логов конвейера</span>
          <span>{logs.length} записей</span>
        </div>
        <div className="p-4 font-mono text-[12px] leading-relaxed max-h-[340px] overflow-y-auto space-y-1">
          {logs.length === 0 ? (
            <div className="text-[#adb4bf]">Ожидание событий прогона...</div>
          ) : (
            logs.map((line, i) => (
              <div key={i} className="text-[#e6e9ef]">
                <span className="text-[#7c8490] mr-2 select-none">
                  {String(i + 1).padStart(3, "0")}
                </span>
                {line}
              </div>
            ))
          )}
          <div ref={bottomRef} />
        </div>
      </section>

      {collectJob && (
        <section className="bg-paper border border-hairline rounded-[2px] p-4 space-y-2">
          <div className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary">
            Сбор объявлений (
            {collectJob.status === "running"
              ? "выполняется"
              : collectJob.status === "completed"
                ? "завершено"
                : collectJob.status === "cancelled"
                  ? "остановлено"
                  : "ошибка"}
            )
          </div>
          <div className="font-mono text-[12px] text-ink-primary space-y-1 max-h-40 overflow-y-auto">
            {collectLogs.map((l, i) => (
              <div key={i}>{l}</div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
