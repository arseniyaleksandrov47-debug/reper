import {
  Database,
  FileText,
  Layers,
  Map as MapIcon,
  Sparkles,
  Table2
} from "lucide-react";

export type WorkspaceTab = "workbench" | "table" | "logs";

export interface WorkspaceRailProps {
  readonly activeTab: WorkspaceTab;
  readonly onSelectTab: (tab: WorkspaceTab) => void;
  readonly rankedCount: number;
  readonly deferredCount: number;
  readonly isSearchRunning: boolean;
  readonly isCollectRunning: boolean;
  readonly hasReport: boolean;
  readonly dbTotalCount: number;
  readonly keysConfiguredCount: number;
  readonly onOpenSources: () => void;
  readonly onOpenConceptHelper: () => void;
}

export function WorkspaceRail({
  activeTab,
  onSelectTab,
  rankedCount,
  deferredCount,
  isSearchRunning,
  isCollectRunning,
  hasReport,
  dbTotalCount,
  keysConfiguredCount,
  onOpenSources,
  onOpenConceptHelper
}: WorkspaceRailProps) {
  const totalBadge = rankedCount + deferredCount;

  const navItems: Array<{
    id: WorkspaceTab;
    label: string;
    shortLabel: string;
    icon: typeof MapIcon;
    badge?: string | null;
    pulse?: boolean;
  }> = [
    {
      id: "workbench",
      label: "Карта и подбор",
      shortLabel: "Карта",
      icon: MapIcon
    },
    {
      id: "table",
      label: "Сравнительная таблица",
      shortLabel: "Таблица",
      icon: Table2,
      badge: hasReport && totalBadge > 0 ? String(totalBadge) : null
    },
    {
      id: "logs",
      label: "Журнал и этапы расчёта",
      shortLabel: "Журнал",
      icon: FileText,
      pulse: isSearchRunning || isCollectRunning
    }
  ];

  return (
    <aside
      aria-label="Навигация рабочих режимов"
      className="w-[58px] shrink-0 h-full bg-[#181b1f] text-[#f5f3ee] border-r border-[#181b1f] flex flex-col items-center py-3 select-none z-30"
    >
      {/* Reper Cut-Mark Brand Emblem */}
      <button
        type="button"
        onClick={() => onSelectTab("workbench")}
        title="Reper — На главную карту"
        aria-label="Reper — На главную карту"
        className="w-10 h-10 bg-[#181b1f] border border-[#f5f3ee]/25 rounded-[2px] flex items-center justify-center hover:border-[#d13b27] transition-colors mb-4"
      >
        <svg viewBox="0 0 32 32" className="w-7 h-7" aria-hidden="true">
          <line x1="6.8" y1="8" x2="25.2" y2="8" stroke="#F5F3EE" strokeWidth="2.4" strokeLinecap="butt" />
          <polyline
            points="7.2,24.8 16,14.5 24.8,24.8"
            fill="none"
            stroke="#F5F3EE"
            strokeWidth="2.7"
            strokeLinejoin="miter"
            strokeLinecap="butt"
          />
          <line x1="16" y1="18" x2="16" y2="25.2" stroke="#F5F3EE" strokeWidth="2.1" strokeLinecap="butt" />
          <rect x="13.6" y="9.5" width="4.8" height="4.8" fill="#D13B27" />
        </svg>
      </button>

      {/* Workspace Modes */}
      <nav className="flex flex-col items-center gap-1.5 w-full px-1.5">
        {navItems.map((item) => {
          const Icon = item.icon;
          const active = activeTab === item.id;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => onSelectTab(item.id)}
              title={item.label}
              aria-current={active ? "page" : undefined}
              className={`relative w-full py-2 px-1 rounded-[2px] flex flex-col items-center justify-center gap-1 transition-colors ${
                active
                  ? "bg-[#252930] text-[#f5f3ee]"
                  : "text-[#adb4bf] hover:text-[#f5f3ee] hover:bg-[#20242a]"
              }`}
            >
              {active && (
                <span
                  aria-hidden="true"
                  className="absolute left-0 top-1 bottom-1 w-[3px] bg-[#d13b27]"
                />
              )}
              <Icon className="w-4 h-4 shrink-0" strokeWidth={active ? 2.2 : 1.75} />
              <span className="text-[11px] font-medium leading-none tracking-tight">
                {item.shortLabel}
              </span>
              {item.badge && (
                <span className="font-mono text-[11px] leading-none px-1 py-0.5 bg-[#d13b27] text-[#fffdf9] rounded-[2px]">
                  {item.badge}
                </span>
              )}
              {item.pulse && (
                <span
                  aria-label="Идёт расчёт"
                  className="w-2 h-2 rounded-full bg-[#d13b27] animate-pulse"
                />
              )}
            </button>
          );
        })}
      </nav>

      {/* Drag handle spacer for frameless desktop shell */}
      <div
        className="pywebview-drag-region flex-1 w-full min-h-[24px]"
        aria-hidden="true"
      />

      {/* Bottom Tools */}
      <div className="flex flex-col items-center gap-1.5 w-full px-1.5 pt-3 border-t border-[#2c313a]">
        <button
          type="button"
          onClick={onOpenConceptHelper}
          title="Разобрать параметры из свободного описания бизнеса"
          className="w-full py-2 px-1 rounded-[2px] flex flex-col items-center justify-center gap-1 text-[#adb4bf] hover:text-[#f5f3ee] hover:bg-[#20242a] transition-colors"
        >
          <Sparkles className="w-4 h-4" strokeWidth={1.75} />
          <span className="text-[11px] leading-none">Текст</span>
        </button>

        <button
          type="button"
          onClick={onOpenSources}
          title={`Локальная база (${dbTotalCount} лотов) и ключи (${keysConfiguredCount}/2)`}
          className="relative w-full py-2 px-1 rounded-[2px] flex flex-col items-center justify-center gap-1 text-[#adb4bf] hover:text-[#f5f3ee] hover:bg-[#20242a] transition-colors"
        >
          <span
            aria-label={keysConfiguredCount >= 2 ? "Ключи активны" : "Проверьте ключи"}
            className={`absolute top-1.5 right-2 w-2 h-2 rounded-full ${
              keysConfiguredCount >= 2 ? "bg-[#22c55e]" : "bg-[#d13b27]"
            }`}
          />
          <Database className="w-4 h-4" strokeWidth={1.75} />
          <span className="text-[11px] leading-none">База</span>
          <span className="font-mono text-[11px] text-[#adb4bf] leading-none tabular-nums">
            {dbTotalCount > 0 ? dbTotalCount : <Layers className="w-2.5 h-2.5 inline" />}
          </span>
        </button>
      </div>
    </aside>
  );
}
