import { useEffect, useId, useState } from "react";
import { AlertTriangle, CheckCircle2, Database, KeyRound, RefreshCw, ShieldCheck, X } from "lucide-react";
import type {
  CityKey,
  CollectResult,
  HealthResponse,
  JobState,
  SecretsStatus
} from "../types/geoestate";
import { formatNumber, sourceDisplayName } from "../utils/formatters";

export interface SourcesAndSecretsModalProps {
  readonly open: boolean;
  readonly onClose: () => void;
  readonly health: HealthResponse | null;
  readonly activeCity: CityKey;
  readonly collectJob: JobState<CollectResult> | null;
  readonly onRunCollect: (city: CityKey, limitPerSource: number) => void;
  readonly onSaveSecrets: (payload: {
    nvidia_key?: string;
    dadata_key?: string;
  }) => Promise<SecretsStatus>;
}

export function SourcesAndSecretsModal({
  open,
  onClose,
  health,
  activeCity,
  collectJob,
  onRunCollect,
  onSaveSecrets
}: SourcesAndSecretsModalProps) {
  const uid = useId();
  const [nvidiaInput, setNvidiaInput] = useState("");
  const [dadataInput, setDadataInput] = useState("");
  const [editingNvidia, setEditingNvidia] = useState(false);
  const [editingDadata, setEditingDadata] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [limitPerSource, setLimitPerSource] = useState<number>(120);

  useEffect(() => {
    if (!open) return;
    setSaveMessage(null);
    const onKeyDown = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const secrets = health?.secrets;
  const nvidiaConfigured = Boolean(secrets?.nvidia_configured);
  const dadataConfigured = Boolean(secrets?.dadata_configured);
  const isCollectRunning = collectJob?.status === "running";

  const showNvidiaInput = !nvidiaConfigured || editingNvidia;
  const showDadataInput = !dadataConfigured || editingDadata;

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!nvidiaInput.trim() && !dadataInput.trim()) {
      setSaveMessage("Введите новый ключ или закройте окно — действующие ключи уже подключены.");
      return;
    }
    setSaving(true);
    setSaveMessage(null);
    try {
      await onSaveSecrets({
        nvidia_key: nvidiaInput.trim() || undefined,
        dadata_key: dadataInput.trim() || undefined
      });
      setNvidiaInput("");
      setDadataInput("");
      setEditingNvidia(false);
      setEditingDadata(false);
      setSaveMessage("Ключи сохранены в защищённом хранилище Windows и активны.");
    } catch (err) {
      setSaveMessage(err instanceof Error ? err.message : "Ошибка сохранения ключей");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={`${uid}-title`}
      onClick={onClose}
      className="fixed inset-0 z-[1200] bg-[#181b1f]/60 flex items-center justify-center p-4"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-[740px] bg-paper border border-ink-primary rounded-[2px] flex flex-col max-h-[90vh] overflow-hidden"
      >
        <div className="px-5 py-3.5 border-b border-ink-primary bg-canvas flex items-center justify-between">
          <div>
            <h2 id={`${uid}-title`} className="text-[16px] font-bold text-ink-primary">
              База объявлений и ключи сервисов
            </h2>
            <p className="text-[12px] text-ink-muted font-mono mt-0.5 truncate max-w-[540px]">
              Локальное хранилище Windows · данные обновляются локально
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Закрыть окно источников"
            className="p-1 text-ink-muted hover:text-ink-primary"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-5 space-y-5">
          {/* City DB Stats & Manual Collection */}
          <section className="space-y-3">
            <div className="flex items-center justify-between gap-2">
              <h3 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary inline-flex items-center gap-1.5">
                <Database className="w-3.5 h-3.5" />
                Состояние базы объявлений
              </h3>

              <div className="flex items-center gap-2 text-[12px]">
                <label className="inline-flex items-center gap-1.5 text-ink-secondary">
                  <span>Лимит / источник:</span>
                  <select
                    value={limitPerSource}
                    onChange={(e) => setLimitPerSource(Number(e.target.value))}
                    className="h-7 px-2 py-0 leading-[26px] font-mono text-[12px] bg-canvas border border-hairline rounded-[2px]"
                  >
                    <option value={60}>60</option>
                    <option value={120}>120</option>
                    <option value={250}>250</option>
                  </select>
                </label>

                <button
                  type="button"
                  disabled={isCollectRunning}
                  onClick={() => onRunCollect(activeCity, limitPerSource)}
                  className="h-7 px-3 bg-signal hover:bg-signal-hover disabled:opacity-50 text-[#fffdf9] text-[12px] font-semibold rounded-[2px] inline-flex items-center gap-1.5"
                >
                  <RefreshCw
                    className={`w-3 h-3 ${isCollectRunning ? "animate-spin" : ""}`}
                  />
                  <span>
                    {isCollectRunning
                      ? "Сбор..."
                      : `Обновить ${activeCity === "moscow" ? "МСК" : "СПб"}`}
                  </span>
                </button>
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {(health?.cities ?? []).map((city) => (
                <div
                  key={city.key}
                  className={`p-3 border rounded-[2px] ${
                    city.key === activeCity
                      ? "border-ink-primary bg-canvas"
                      : "border-hairline bg-paper"
                  }`}
                >
                  <div className="flex items-baseline justify-between">
                    <strong className="text-[14px] text-ink-primary">{city.name}</strong>
                    <span className="font-mono text-[13px] font-bold text-ink-primary tabular-nums">
                      {formatNumber(city.stats.total)} лотов
                    </span>
                  </div>
                  <div className="text-[11px] font-mono text-ink-muted mt-1">
                    Координат: {formatNumber(city.stats.unique_locations)} · Торги:{" "}
                    {sourceDisplayName(city.municipal_source)}
                  </div>
                  <div className="mt-2 pt-2 border-t border-hairline flex flex-wrap gap-3 text-[11px] font-mono text-ink-secondary">
                    {Object.entries(city.stats.by_source ?? {}).map(([src, cnt]) => (
                      <span key={src}>
                        {sourceDisplayName(src)}: <b>{cnt}</b>
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </div>

            {collectJob && (
              <div
                className={`p-3 border rounded-[2px] text-[12px] space-y-1 ${
                  collectJob.status === "failed"
                    ? "bg-[#fef1f1] border-danger text-danger"
                    : collectJob.status === "completed"
                      ? "bg-[#eef7f1] border-verified/50 text-ink-primary"
                      : "bg-canvas border-ink-primary text-ink-primary"
                }`}
              >
                <div className="flex items-center justify-between gap-2 font-semibold">
                  <span className="inline-flex items-center gap-1.5">
                    {collectJob.status === "running" && (
                      <RefreshCw className="w-3.5 h-3.5 text-signal animate-spin" />
                    )}
                    {collectJob.status === "completed" && (
                      <CheckCircle2 className="w-3.5 h-3.5 text-verified" />
                    )}
                    {collectJob.status === "failed" && (
                      <AlertTriangle className="w-3.5 h-3.5 text-danger" />
                    )}
                    <span>
                      {collectJob.status === "running"
                        ? collectJob.stage || "Обновляем базу объявлений..."
                        : collectJob.status === "completed"
                          ? "База объявлений обновлена"
                          : "Ошибка обновления базы"}
                    </span>
                  </span>
                </div>
                {collectJob.error && (
                  <p className="text-[11px] font-mono break-words">{collectJob.error}</p>
                )}
                {!collectJob.error && collectJob.logs.length > 0 && (
                  <p className="text-[11px] font-mono text-ink-secondary truncate">
                    {collectJob.logs[collectJob.logs.length - 1]}
                  </p>
                )}
              </div>
            )}
          </section>

          {/* Active API Secrets (NVIDIA & DaData only) */}
          <form onSubmit={handleSave} className="space-y-3 pt-4 border-t border-ink-primary">
            <div className="flex items-center justify-between">
              <h3 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-ink-secondary inline-flex items-center gap-1.5">
                <KeyRound className="w-3.5 h-3.5" />
                Ключи подключённых сервисов
              </h3>
              <span className="inline-flex items-center gap-1 text-[11px] text-verified font-medium">
                <ShieldCheck className="w-3.5 h-3.5" /> Локальное шифрование Windows
              </span>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {/* NVIDIA Key */}
              <div className="border border-hairline bg-canvas p-3 rounded-[2px] space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <label
                    htmlFor={`${uid}-nvidia`}
                    className="text-[12px] font-bold text-ink-primary"
                  >
                    NVIDIA / LLM Key
                  </label>
                  {nvidiaConfigured ? (
                    <span className="inline-flex items-center gap-1 text-[11px] font-mono font-semibold text-verified bg-[#eef7f1] border border-verified/35 px-1.5 py-0.5 rounded-[2px]">
                      <CheckCircle2 className="w-3 h-3" />
                      Активен
                    </span>
                  ) : (
                    <span className="text-[11px] font-mono text-signal font-semibold">
                      Не задан
                    </span>
                  )}
                </div>

                {nvidiaConfigured && !editingNvidia ? (
                  <div className="flex items-center justify-between gap-2 bg-paper border border-ink-primary/25 px-2.5 h-8 rounded-[2px]">
                    <span className="font-mono text-[12px] font-semibold text-ink-primary tabular-nums">
                      {secrets?.nvidia_masked || "Подключён"}
                    </span>
                    <button
                      type="button"
                      onClick={() => setEditingNvidia(true)}
                      className="text-[11px] font-semibold text-signal hover:underline"
                    >
                      Заменить
                    </button>
                  </div>
                ) : (
                  <div className="flex items-center gap-1.5">
                    <input
                      id={`${uid}-nvidia`}
                      name="nvidia_key"
                      type="password"
                      value={nvidiaInput}
                      onChange={(e) => setNvidiaInput(e.target.value)}
                      placeholder={
                        nvidiaConfigured
                          ? `Текущий: ${secrets?.nvidia_masked} (введите новый)`
                          : "Вставьте ключ nvapi-..."
                      }
                      autoComplete="off"
                      className="flex-1 min-w-0 h-8 px-2.5 text-[12px] font-mono bg-paper border border-ink-primary rounded-[2px]"
                    />
                    {nvidiaConfigured && (
                      <button
                        type="button"
                        onClick={() => {
                          setEditingNvidia(false);
                          setNvidiaInput("");
                        }}
                        className="h-8 px-2 text-[11px] border border-hairline bg-paper rounded-[2px] text-ink-secondary hover:border-ink-primary"
                      >
                        Отмена
                      </button>
                    )}
                  </div>
                )}
              </div>

              {/* DaData Key */}
              <div className="border border-hairline bg-canvas p-3 rounded-[2px] space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <label
                    htmlFor={`${uid}-dadata`}
                    className="text-[12px] font-bold text-ink-primary"
                  >
                    DaData Key (ИНН + ФНС)
                  </label>
                  {dadataConfigured ? (
                    <span className="inline-flex items-center gap-1 text-[11px] font-mono font-semibold text-verified bg-[#eef7f1] border border-verified/35 px-1.5 py-0.5 rounded-[2px]">
                      <CheckCircle2 className="w-3 h-3" />
                      Активен
                    </span>
                  ) : (
                    <span className="text-[11px] font-mono text-signal font-semibold">
                      Не задан
                    </span>
                  )}
                </div>

                {dadataConfigured && !editingDadata ? (
                  <div className="flex items-center justify-between gap-2 bg-paper border border-ink-primary/25 px-2.5 h-8 rounded-[2px]">
                    <span className="font-mono text-[12px] font-semibold text-ink-primary tabular-nums">
                      {secrets?.dadata_masked || "Подключён"}
                    </span>
                    <button
                      type="button"
                      onClick={() => setEditingDadata(true)}
                      className="text-[11px] font-semibold text-signal hover:underline"
                    >
                      Заменить
                    </button>
                  </div>
                ) : (
                  <div className="flex items-center gap-1.5">
                    <input
                      id={`${uid}-dadata`}
                      name="dadata_key"
                      type="password"
                      value={dadataInput}
                      onChange={(e) => setDadataInput(e.target.value)}
                      placeholder={
                        dadataConfigured
                          ? `Текущий: ${secrets?.dadata_masked} (введите новый)`
                          : "Вставьте токен DaData"
                      }
                      autoComplete="off"
                      className="flex-1 min-w-0 h-8 px-2.5 text-[12px] font-mono bg-paper border border-ink-primary rounded-[2px]"
                    />
                    {dadataConfigured && (
                      <button
                        type="button"
                        onClick={() => {
                          setEditingDadata(false);
                          setDadataInput("");
                        }}
                        className="h-8 px-2 text-[11px] border border-hairline bg-paper rounded-[2px] text-ink-secondary hover:border-ink-primary"
                      >
                        Отмена
                      </button>
                    )}
                  </div>
                )}
              </div>
            </div>

            {saveMessage && (
              <div className="text-[12px] font-medium text-ink-primary bg-subtle border border-hairline px-3 py-2 rounded-[2px]">
                {saveMessage}
              </div>
            )}

            <div className="flex items-center justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={onClose}
                className="h-9 px-3 border border-hairline bg-canvas hover:border-ink-primary text-[12px] font-medium rounded-[2px]"
              >
                Закрыть
              </button>
              {(showNvidiaInput || showDadataInput) && (
                <button
                  type="submit"
                  disabled={saving}
                  className="h-9 px-4 bg-ink-primary hover:bg-[#2a2f36] text-[#f8f9fa] text-[12px] font-semibold rounded-[2px] disabled:opacity-50"
                >
                  {saving ? "Сохранение..." : "Сохранить ключи"}
                </button>
              )}
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
