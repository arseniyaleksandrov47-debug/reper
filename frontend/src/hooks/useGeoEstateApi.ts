import { useCallback, useEffect, useRef, useState } from "react";
import type {
  CityKey,
  CollectResult,
  HealthResponse,
  JobState,
  ListingPreview,
  MapKeysConfig,
  SearchReport,
  SearchRequest,
  SecretsStatus
} from "../types/geoestate";

interface ContinuationPayload {
  action: "continue_unverified" | "find_more" | "supply_references";
  extra_excludes?: string[];
  full_cost_limit?: number | null;
}

export interface PreviewFilterParams {
  readonly min_area_sqm?: number | null;
  readonly max_area_sqm?: number | null;
  readonly max_price_month?: number | null;
  readonly allowed_floors?: number[] | null;
  readonly include_unverified?: boolean;
  readonly location_query?: string | null;
  readonly center_lat?: number | null;
  readonly center_lng?: number | null;
  readonly search_radius_m?: number | null;
  readonly limit?: number;
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data?.detail) detail = String(data.detail);
    } catch {
      // ignore non-json error body
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

/** Manages API communication, active search/collect polling, and city preview state. */
export function useGeoEstateApi() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [mapKeys, setMapKeys] = useState<MapKeysConfig>({
    yandex_maps_key: "",
    dgis_maps_key: ""
  });
  const [previewListings, setPreviewListings] = useState<ListingPreview[]>([]);
  const [previewTotal, setPreviewTotal] = useState<number>(0);
  const [previewLoaded, setPreviewLoaded] = useState<boolean>(false);
  const [previewLoading, setPreviewLoading] = useState<boolean>(true);

  const [searchJob, setSearchJob] = useState<JobState<SearchReport> | null>(null);
  const [collectJob, setCollectJob] = useState<JobState<CollectResult> | null>(null);
  const [lastReport, setLastReport] = useState<SearchReport | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const searchTimerRef = useRef<number | null>(null);
  const collectTimerRef = useRef<number | null>(null);
  const previewSeqRef = useRef<number>(0);
  const previewAbortRef = useRef<AbortController | null>(null);
  const lastPreviewFiltersRef = useRef<PreviewFilterParams | undefined>(undefined);

  const refreshMapKeys = useCallback(async () => {
    try {
      const keys = await fetchJson<MapKeysConfig>("/api/map-keys");
      setMapKeys(keys);
    } catch {
      // optional when running outside loopback
    }
  }, []);

  const refreshHealth = useCallback(async () => {
    try {
      const data = await fetchJson<HealthResponse>("/api/health");
      setHealth(data);
      setHealthError(null);
      void refreshMapKeys();
      return data;
    } catch (err) {
      setHealthError(err instanceof Error ? err.message : "Ошибка подключения к локальному API");
      return null;
    }
  }, [refreshMapKeys]);

  const loadCityPreview = useCallback(
    async (city: CityKey, filters?: PreviewFilterParams) => {
      const effectiveFilters = filters !== undefined ? filters : lastPreviewFiltersRef.current;
      if (filters !== undefined) {
        lastPreviewFiltersRef.current = filters;
      }

      previewAbortRef.current?.abort();
      const controller = new AbortController();
      previewAbortRef.current = controller;
      previewSeqRef.current += 1;
      const reqSeq = previewSeqRef.current;

      setPreviewLoading(true);
      try {
        const params = new URLSearchParams();
        params.set("limit", String(effectiveFilters?.limit ?? 600));
        if (effectiveFilters?.min_area_sqm != null) {
          params.set("min_area", String(effectiveFilters.min_area_sqm));
        }
        if (effectiveFilters?.max_area_sqm != null) {
          params.set("max_area", String(effectiveFilters.max_area_sqm));
        }
        if (effectiveFilters?.max_price_month != null) {
          params.set("max_price", String(effectiveFilters.max_price_month));
        }
        if (effectiveFilters?.allowed_floors && effectiveFilters.allowed_floors.length > 0) {
          params.set("floors", effectiveFilters.allowed_floors.join(","));
        }
        if (effectiveFilters?.include_unverified !== undefined) {
          params.set("include_unverified", String(effectiveFilters.include_unverified));
        }
        if (effectiveFilters?.location_query?.trim()) {
          params.set("location_query", effectiveFilters.location_query.trim());
        }
        if (
          effectiveFilters?.center_lat != null &&
          effectiveFilters?.center_lng != null
        ) {
          params.set("center_lat", String(effectiveFilters.center_lat));
          params.set("center_lng", String(effectiveFilters.center_lng));
          if (effectiveFilters?.search_radius_m != null) {
            params.set("radius_m", String(effectiveFilters.search_radius_m));
          }
        }

        const res = await fetchJson<{
          city: CityKey;
          total_matched?: number;
          items: ListingPreview[];
        }>(`/api/cities/${city}/listings?${params.toString()}`, {
          signal: controller.signal
        });
        if (reqSeq !== previewSeqRef.current) return;
        setPreviewListings(res.items || []);
        setPreviewTotal(res.total_matched ?? (res.items?.length || 0));
        setPreviewLoaded(true);
      } catch (err) {
        if ((err as Error)?.name === "AbortError" || reqSeq !== previewSeqRef.current) {
          return;
        }
        setPreviewListings([]);
        setPreviewTotal(0);
        setPreviewLoaded(true);
      } finally {
        if (reqSeq === previewSeqRef.current) {
          setPreviewLoading(false);
        }
      }
    },
    []
  );

  const clearReport = useCallback(() => {
    setLastReport(null);
  }, []);

  const pollJob = useCallback(
    <T extends SearchReport | CollectResult>(
      jobId: string,
      jobMeta: { type: "search" | "collect"; city: CityKey },
      onUpdate: (value: React.SetStateAction<JobState<T> | null>) => void,
      timerRef: React.MutableRefObject<number | null>,
      onCompleted?: (job: JobState<T>) => void
    ) => {
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
      }
      let consecutiveErrors = 0;
      let stopped = false;

      const tick = async () => {
        if (stopped) return;
        try {
          const data = await fetchJson<JobState<T>>(`/api/jobs/${jobId}`);
          if (stopped) return;
          consecutiveErrors = 0;
          onUpdate(data);
          if (data.status !== "running") {
            stopped = true;
            if (timerRef.current !== null) {
              window.clearInterval(timerRef.current);
              timerRef.current = null;
            }
            if (data.status === "completed") {
              onCompleted?.(data);
            }
          }
        } catch (err) {
          consecutiveErrors += 1;
          if (consecutiveErrors < 5) {
            return;
          }
          stopped = true;
          if (timerRef.current !== null) {
            window.clearInterval(timerRef.current);
            timerRef.current = null;
          }
          const msg = err instanceof Error ? err.message : "Потеряна связь с задачей";
          onUpdate((prev) =>
            prev
              ? { ...prev, status: "failed", stage: "Ошибка связи", error: msg }
              : ({
                  job_id: jobId,
                  type: jobMeta.type,
                  city: jobMeta.city,
                  status: "failed",
                  stage: "Ошибка связи",
                  logs: [],
                  result: null,
                  error: msg
                } as JobState<T>)
          );
          setActionError(msg);
        }
      };
      void tick();
      timerRef.current = window.setInterval(tick, 900);
    },
    []
  );

  const runSearch = useCallback(
    async (req: SearchRequest) => {
      setActionError(null);
      try {
        const { job_id } = await fetchJson<{ job_id: string }>("/api/search", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(req)
        });
        setSearchJob({
          job_id,
          type: "search",
          city: req.city,
          status: "running",
          stage: "Инициализация…",
          logs: [],
          result: null,
          error: null
        });
        pollJob<SearchReport>(
          job_id,
          { type: "search", city: req.city },
          setSearchJob,
          searchTimerRef,
          (finished) => {
            if (finished.result) {
              setLastReport(finished.result);
            }
            if (!req.skip_collect) {
              void refreshHealth();
            }
          }
        );
        return job_id;
      } catch (err) {
        setActionError(err instanceof Error ? err.message : "Не удалось запустить подбор");
        return null;
      }
    },
    [pollJob, refreshHealth]
  );

  const continueJob = useCallback(
    async (jobId: string, payload: ContinuationPayload) => {
      setActionError(null);
      try {
        const { job_id } = await fetchJson<{ job_id: string }>(`/api/jobs/${jobId}/continue`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const cityKey: CityKey = lastReport?.city ?? searchJob?.city ?? "saint_petersburg";
        setSearchJob((prev) => ({
          job_id,
          type: "search",
          city: prev?.city ?? cityKey,
          status: "running",
          stage: "Инициализация…",
          logs: [],
          result: prev?.result ?? null,
          error: null
        }));
        pollJob<SearchReport>(
          job_id,
          { type: "search", city: cityKey },
          setSearchJob,
          searchTimerRef,
          (finished) => {
            if (finished.result) {
              setLastReport(finished.result);
            }
          }
        );
        return job_id;
      } catch (err) {
        setActionError(err instanceof Error ? err.message : "Не удалось продолжить подбор");
        return null;
      }
    },
    [lastReport?.city, pollJob, searchJob?.city]
  );

  const cancelSearch = useCallback(async () => {
    const activeId = searchJob?.status === "running" ? searchJob.job_id : null;
    if (!activeId) return;
    if (searchTimerRef.current !== null) {
      window.clearInterval(searchTimerRef.current);
      searchTimerRef.current = null;
    }
    try {
      await fetchJson<{ job_id: string; status: string }>(`/api/jobs/${activeId}/cancel`, {
        method: "POST"
      });
    } catch {
      // ignore if job finished right before cancel landed
    }
    setSearchJob((prev) =>
      prev && prev.job_id === activeId
        ? {
            ...prev,
            status: "cancelled",
            stage: "Подбор остановлен",
            error: null
          }
        : prev
    );
  }, [searchJob?.job_id, searchJob?.status]);

  const runCollect = useCallback(
    async (city: CityKey, limitPerSource: number) => {
      setActionError(null);
      try {
        const { job_id } = await fetchJson<{ job_id: string }>("/api/collect", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ city, limit_per_source: limitPerSource })
        });
        setCollectJob({
          job_id,
          type: "collect",
          city,
          status: "running",
          stage: "Инициализация…",
          logs: [],
          result: null,
          error: null
        });
        pollJob<CollectResult>(
          job_id,
          { type: "collect", city },
          setCollectJob,
          collectTimerRef,
          () => {
            void refreshHealth();
            void loadCityPreview(city);
          }
        );
        return job_id;
      } catch (err) {
        setActionError(err instanceof Error ? err.message : "Не удалось запустить сбор БД");
        return null;
      }
    },
    [loadCityPreview, pollJob, refreshHealth]
  );

  const saveSecrets = useCallback(
    async (payload: {
      nvidia_key?: string;
      dadata_key?: string;
      yandex_maps_key?: string;
      dgis_maps_key?: string;
    }): Promise<SecretsStatus> => {
      const secrets = await fetchJson<SecretsStatus>("/api/secrets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      setHealth((prev) => (prev ? { ...prev, secrets } : prev));
      await refreshMapKeys();
      return secrets;
    },
    [refreshMapKeys]
  );

  useEffect(() => {
    void refreshHealth();
    return () => {
      if (searchTimerRef.current !== null) window.clearInterval(searchTimerRef.current);
      if (collectTimerRef.current !== null) window.clearInterval(collectTimerRef.current);
    };
  }, [refreshHealth]);

  return {
    health,
    healthError,
    mapKeys,
    previewListings,
    previewTotal,
    previewLoaded,
    previewLoading,
    searchJob,
    collectJob,
    lastReport,
    clearReport,
    actionError,
    setActionError,
    refreshHealth,
    loadCityPreview,
    runSearch,
    continueJob,
    cancelSearch,
    runCollect,
    saveSecrets
  };
}
