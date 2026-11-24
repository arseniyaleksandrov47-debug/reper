import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, X } from "lucide-react";
import { CommandStrip } from "./components/CommandStrip";
import { ComparisonMatrixView } from "./components/ComparisonMatrixView";
import { ConceptHelperModal } from "./components/ConceptHelperModal";
import { GeoMapViewport } from "./components/GeoMapViewport";
import { IntelligenceSheet } from "./components/IntelligenceSheet";
import { PipelineLogsView } from "./components/PipelineLogsView";
import { SourcesAndSecretsModal } from "./components/SourcesAndSecretsModal";
import { SupplyReferencesModal } from "./components/SupplyReferencesModal";
import { WorkspaceRail, type WorkspaceTab } from "./components/WorkspaceRail";
import { useGeoEstateApi } from "./hooks/useGeoEstateApi";
import {
  clearRecentSearches,
  loadRecentSearches,
  recordRecentSearch,
  type RecentSearchEntry
} from "./utils/recentSearches";
import type {
  CityKey,
  RankedCandidate,
  SearchRequest,
  SelectedLeadTarget
} from "./types/geoestate";

declare global {
  interface Window {
    pywebview?: {
      api?: {
        minimize?: () => void;
        toggle_maximize?: () => Promise<boolean>;
        is_maximized?: () => Promise<boolean>;
        close?: () => void;
        begin_resize?: (edge: string) => void;
      };
    };
  }
}

const INITIAL_REQUEST: SearchRequest = {
  city: "saint_petersburg",
  target_business: "Кофейня",
  location_query: "весь город",
  min_area_sqm: 40,
  max_area_sqm: 100,
  target_area_sqm: 65,
  max_price_month: 150000,
  max_price_sqm: null,
  price_sqm_period: "month",
  full_cost_limit: null,
  allowed_floors: [1],
  business_concept:
    "Кофейня / спешелти точка 40–100 м² с удобным входом с улицы, хорошим пешеходным потоком и умеренной конкуренцией.",
  max_candidates: 24,
  analysis_limit: 6,
  include_unverified: false,
  skip_collect: true,
  exclude_property_keys: [],
  center_lat: null,
  center_lng: null,
  search_radius_m: null
};

export function App() {
  const {
    health,
    healthError,
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
  } = useGeoEstateApi();

  const [request, setRequest] = useState<SearchRequest>(INITIAL_REQUEST);
  const [floorInput, setFloorInput] = useState<string>("1");
  const [activeTab, setActiveTab] = useState<WorkspaceTab>("workbench");
  const [selectedRank, setSelectedRank] = useState<number | null>(null);
  const [dossierOpen, setDossierOpen] = useState(false);
  const [selectedLead, setSelectedLead] = useState<SelectedLeadTarget | null>(null);
  const [pinPickMode, setPinPickMode] = useState(false);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [conceptHelperOpen, setConceptHelperOpen] = useState(false);
  const [supplyRefJobId, setSupplyRefJobId] = useState<string | null>(null);
  const [focusedCoord, setFocusedCoord] = useState<{
    lat: number;
    lng: number;
    label?: string;
    seq: number;
  } | null>(null);
  const coordSeqRef = useRef(0);
  const prePinLocationRef = useRef<string>("весь город");
  const lastConceptFreeTextRef = useRef<{
    concept: string;
    freeText: string;
  } | null>(null);
  const [recentSearches, setRecentSearches] = useState<RecentSearchEntry[]>(() =>
    loadRecentSearches()
  );

  const [elapsedSec, setElapsedSec] = useState(0);
  const [isDesktopShell, setIsDesktopShell] = useState<boolean>(() =>
    Boolean(typeof window !== "undefined" && window.pywebview)
  );
  const [isMaximized, setIsMaximized] = useState(false);
  const isSearchRunning = searchJob?.status === "running";
  const requestRef = useRef(request);
  requestRef.current = request;
  const floorKey = (request.allowed_floors ?? []).join(",");

  useEffect(() => {
    refreshHealth();
  }, []);

  // Dynamically refresh filtered citywide map preview whenever search filters or city change
  useEffect(() => {
    const delayMs = previewLoaded ? 180 : 0;
    const timer = window.setTimeout(() => {
      void loadCityPreview(request.city, {
        min_area_sqm: request.min_area_sqm,
        max_area_sqm: request.max_area_sqm,
        max_price_month: request.max_price_month,
        allowed_floors: request.allowed_floors,
        include_unverified: request.include_unverified,
        location_query: request.location_query,
        center_lat: request.center_lat,
        center_lng: request.center_lng,
        search_radius_m: request.search_radius_m
      });
    }, delayMs);
    return () => window.clearTimeout(timer);
  }, [
    request.city,
    request.min_area_sqm,
    request.max_area_sqm,
    request.max_price_month,
    floorKey,
    request.include_unverified,
    request.location_query,
    request.center_lat,
    request.center_lng,
    request.search_radius_m,
    loadCityPreview
  ]);

  useEffect(() => {
    const syncDesktopApi = () => {
      const hasBridge = Boolean(window.pywebview);
      setIsDesktopShell(hasBridge);
      if (hasBridge && window.pywebview?.api?.is_maximized) {
        void window.pywebview.api.is_maximized().then(setIsMaximized).catch(() => {});
      }
    };
    syncDesktopApi();
    window.addEventListener("pywebviewready", syncDesktopApi);
    return () => window.removeEventListener("pywebviewready", syncDesktopApi);
  }, []);

  useEffect(() => {
    if (!isSearchRunning) {
      setElapsedSec(0);
      return;
    }
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      setElapsedSec(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [isSearchRunning, searchJob?.job_id]);

  // Auto-select #1 ranked lot when a fresh report completes
  useEffect(() => {
    if (!lastReport) return;
    if (lastReport.ranked_candidates.length > 0) {
      setSelectedRank(lastReport.ranked_candidates[0].rank);
    } else {
      setSelectedRank(null);
      setDossierOpen(false);
    }
  }, [lastReport?.job_id]);

  const activeCityInfo = useMemo(
    () => (health?.cities ?? []).find((c) => c.key === request.city),
    [health?.cities, request.city]
  );

  const effectivePreviewTotal = previewLoaded
    ? previewTotal
    : activeCityInfo?.stats?.total ?? 0;

  const dbTotalCount = useMemo(
    () => (health?.cities ?? []).reduce((acc, c) => acc + (c.stats?.total ?? 0), 0),
    [health?.cities]
  );

  const keysConfiguredCount = useMemo(() => {
    const s = health?.secrets;
    if (!s) return 0;
    return [s.nvidia_configured, s.dadata_configured].filter(Boolean).length;
  }, [health?.secrets]);

  const focusMapCoord = (lat: number, lng: number, label?: string) => {
    coordSeqRef.current += 1;
    setFocusedCoord({ lat, lng, label, seq: coordSeqRef.current });
  };

  const resetStaleReportSelection = () => {
    if (lastReport) {
      clearReport();
    }
    setDossierOpen(false);
    setSelectedRank(null);
    setSelectedLead(null);
  };

  const handleCitySwitch = (city: CityKey) => {
    prePinLocationRef.current = "весь город";
    resetStaleReportSelection();
    setRequest((prev) => ({
      ...prev,
      city,
      location_query: "весь город",
      center_lat: null,
      center_lng: null,
      search_radius_m: null,
      exclude_property_keys: []
    }));
  };

  const handleUpdateRequest = (
    updater: SearchRequest | ((prev: SearchRequest) => SearchRequest)
  ) => {
    const current = requestRef.current;
    const candidateNext = typeof updater === "function" ? updater(current) : updater;
    const premisesFilterChanged =
      candidateNext.city !== current.city ||
      (candidateNext.location_query ?? "") !== (current.location_query ?? "") ||
      candidateNext.center_lat !== current.center_lat ||
      candidateNext.center_lng !== current.center_lng ||
      candidateNext.search_radius_m !== current.search_radius_m ||
      candidateNext.min_area_sqm !== current.min_area_sqm ||
      candidateNext.max_area_sqm !== current.max_area_sqm ||
      candidateNext.max_price_month !== current.max_price_month ||
      candidateNext.include_unverified !== current.include_unverified ||
      (candidateNext.allowed_floors ?? []).join(",") !==
        (current.allowed_floors ?? []).join(",");

    if (premisesFilterChanged && !isSearchRunning && lastReport) {
      resetStaleReportSelection();
    }

    setRequest((prev) => {
      const next = typeof updater === "function" ? updater(prev) : updater;
      const isStockConcept =
        next.business_concept === INITIAL_REQUEST.business_concept ||
        /^.+?\s+—\s+подбор коммерческого помещения по заданным параметрам\.$/i.test(
          next.business_concept.trim()
        );
      if (next.target_business.trim() !== prev.target_business.trim() && isStockConcept) {
        const niche = next.target_business.trim();
        if (!niche) return next;
        return {
          ...next,
          business_concept:
            niche.toLowerCase() === "кофейня"
              ? INITIAL_REQUEST.business_concept
              : `${niche} — подбор коммерческого помещения по заданным параметрам.`
        };
      }
      return next;
    });
  };

  const handleRunSearch = (
    overrideReq?: SearchRequest,
    options?: { readonly recordHistory?: boolean; readonly floorOverride?: string }
  ) => {
    const targetReq = overrideReq ?? request;
    if (!targetReq.target_business.trim()) return;
    const isWholeCity =
      targetReq.center_lat == null &&
      /^(?:|весь город|любой|все|спб|мск|москва|санкт-петербург)$/i.test(
        (targetReq.location_query ?? "").trim()
      );
    const normalizedReq: SearchRequest = isWholeCity
      ? { ...targetReq, search_radius_m: null }
      : targetReq;
    if (options?.recordHistory !== false) {
      const effectiveFloor =
        options?.floorOverride ??
        (overrideReq
          ? overrideReq.allowed_floors?.join(", ") ?? ""
          : floorInput);
      const matchedFreeText =
        lastConceptFreeTextRef.current?.concept === normalizedReq.business_concept
          ? lastConceptFreeTextRef.current.freeText
          : undefined;
      setRecentSearches(
        recordRecentSearch(normalizedReq, effectiveFloor, matchedFreeText)
      );
    }
    setActionError(null);
    setDossierOpen(false);
    setSelectedLead(null);
    setActiveTab("workbench");
    runSearch(normalizedReq);
  };

  const handleSearchAroundPoint = (
    lat: number,
    lng: number,
    label?: string,
    autoRun?: boolean
  ) => {
    const radius = request.search_radius_m ?? 1200;
    if (request.center_lat == null && request.location_query?.trim()) {
      prePinLocationRef.current = request.location_query.trim();
    }
    const nextReq: SearchRequest = {
      ...request,
      center_lat: lat,
      center_lng: lng,
      location_query: label || `Точка (${lat.toFixed(4)}, ${lng.toFixed(4)})`,
      search_radius_m: radius
    };
    setRequest(nextReq);
    focusMapCoord(lat, lng, label);
    const shouldStart = autoRun ?? !pinPickMode;
    if (shouldStart) {
      handleRunSearch(nextReq);
    } else if (lastReport && !isSearchRunning) {
      resetStaleReportSelection();
    }
  };

  const handleClearMapCenter = () => {
    const fallbackLoc =
      prePinLocationRef.current &&
      prePinLocationRef.current !== request.location_query
        ? prePinLocationRef.current
        : "весь город";
    if (lastReport && !isSearchRunning) {
      resetStaleReportSelection();
    }
    setRequest((prev) => ({
      ...prev,
      center_lat: null,
      center_lng: null,
      search_radius_m:
        /^(?:|весь город|любой|все|спб|мск|москва|санкт-петербург)$/i.test(
          fallbackLoc.trim()
        )
          ? null
          : prev.search_radius_m,
      location_query: fallbackLoc
    }));
  };

  const handleSelectRank = (rank: number) => {
    setSelectedLead(null);
    setSelectedRank(rank);
    const found = lastReport?.ranked_candidates.find((c) => c.rank === rank);
    if (found?.property_data.lat != null && found?.property_data.lng != null) {
      focusMapCoord(
        found.property_data.lat,
        found.property_data.lng,
        found.property_data.address || undefined
      );
    }
  };

  const handleOpenDossier = (rank: number) => {
    handleSelectRank(rank);
    setSelectedLead(null);
    setDossierOpen(true);
    setActiveTab("workbench");
  };

  const handleSelectLead = (lead: SelectedLeadTarget) => {
    setSelectedLead(lead);
    setDossierOpen(false);
    const target = lead.kind === "preview" ? lead.listing : lead.candidate;
    if (target.lat != null && target.lng != null) {
      focusMapCoord(target.lat, target.lng, target.address || target.title || undefined);
    }
  };

  const handleFocusLeadOnMap = (lead: SelectedLeadTarget) => {
    handleSelectLead(lead);
    setActiveTab("workbench");
  };

  const handleExcludeCandidate = (excludeKey: string) => {
    const nextExcludes = Array.from(
      new Set([...(request.exclude_property_keys ?? []), excludeKey])
    );
    const nextReq: SearchRequest = {
      ...request,
      exclude_property_keys: nextExcludes,
      skip_collect: true
    };
    setRequest(nextReq);
    setDossierOpen(false);
    handleRunSearch(nextReq, { recordHistory: false });
  };

  const handleExport = async (format: "markdown" | "json") => {
    const jobId = lastReport?.job_id;
    if (!jobId) return;
    try {
      const resp = await fetch(`/api/jobs/${jobId}/export?format=${format}`);
      if (!resp.ok) throw new Error(`Ошибка экспорта (${resp.status})`);
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `reper-${lastReport.city}.${format === "json" ? "json" : "md"}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Не удалось выгрузить отчёт");
    }
  };

  const pinnedCenter =
    request.center_lat != null && request.center_lng != null
      ? {
          lat: request.center_lat,
          lng: request.center_lng,
          radiusM: request.search_radius_m ?? 1200
        }
      : null;

  const errorBanner = actionError || healthError || (searchJob?.status === "failed" ? searchJob.error : null);

  const handleMinimizeWindow = () => {
    window.pywebview?.api?.minimize?.();
  };

  const handleToggleMaximizeWindow = () => {
    const fn = window.pywebview?.api?.toggle_maximize;
    if (!fn) return;
    void fn()
      .then((next) => setIsMaximized(Boolean(next)))
      .catch(() => {});
  };

  const handleCloseWindow = () => {
    window.pywebview?.api?.close?.();
  };

  const startResize = (edge: string) => (ev: React.MouseEvent) => {
    ev.preventDefault();
    window.pywebview?.api?.begin_resize?.(edge);
  };

  return (
    <div className="relative h-screen w-screen overflow-hidden bg-canvas text-ink-primary flex">
      {/* Frameless Edge & Corner Native Resize Strips (invisible, zero visual border) */}
      {isDesktopShell && !isMaximized && (
        <>
          <div onMouseDown={startResize("top")} className="fixed top-0 left-2 right-2 h-[4px] z-[2000] cursor-n-resize" />
          <div onMouseDown={startResize("bottom")} className="fixed bottom-0 left-2 right-2 h-[5px] z-[2000] cursor-s-resize" />
          <div onMouseDown={startResize("left")} className="fixed top-2 bottom-2 left-0 w-[4px] z-[2000] cursor-w-resize" />
          <div onMouseDown={startResize("right")} className="fixed top-2 bottom-2 right-0 w-[5px] z-[2000] cursor-e-resize" />
          <div onMouseDown={startResize("top-left")} className="fixed top-0 left-0 w-2 h-2 z-[2001] cursor-nw-resize" />
          <div onMouseDown={startResize("top-right")} className="fixed top-0 right-0 w-2 h-2 z-[2001] cursor-ne-resize" />
          <div onMouseDown={startResize("bottom-left")} className="fixed bottom-0 left-0 w-2 h-2 z-[2001] cursor-sw-resize" />
          <div onMouseDown={startResize("bottom-right")} className="fixed bottom-0 right-0 w-2.5 h-2.5 z-[2001] cursor-se-resize" />
        </>
      )}
      {/* Left Navigation Rail (58px, #181B1F) */}
      <WorkspaceRail
        activeTab={activeTab}
        onSelectTab={setActiveTab}
        rankedCount={lastReport?.ranked_candidates.length ?? 0}
        deferredCount={lastReport?.floor_verification_candidates.length ?? 0}
        isSearchRunning={isSearchRunning}
        isCollectRunning={collectJob?.status === "running"}
        hasReport={Boolean(lastReport)}
        dbTotalCount={dbTotalCount}
        keysConfiguredCount={keysConfiguredCount}
        onOpenSources={() => setSourcesOpen(true)}
        onOpenConceptHelper={() => setConceptHelperOpen(true)}
      />

      {/* Right Workspace Column (Top Command Strip + Active Workspace) */}
      <div className="flex-1 min-w-0 h-full flex flex-col overflow-hidden">
        <CommandStrip
          request={request}
          cities={health?.cities ?? []}
          floorInput={floorInput}
          onChangeFloorInput={setFloorInput}
          onChangeRequest={handleUpdateRequest}
          onCitySwitch={handleCitySwitch}
          onRunSearch={() => handleRunSearch()}
          onCancelSearch={cancelSearch}
          onOpenConceptHelper={() => setConceptHelperOpen(true)}
          onExport={handleExport}
          hasReport={Boolean(lastReport?.job_id)}
          isRunning={isSearchRunning}
          elapsedSec={elapsedSec}
          pinPickMode={pinPickMode}
          onTogglePinPickMode={() => setPinPickMode((v) => !v)}
          onClearMapCenter={handleClearMapCenter}
          dbPreviewCount={effectivePreviewTotal}
          isDesktopShell={isDesktopShell}
          isMaximized={isMaximized}
          onMinimizeWindow={handleMinimizeWindow}
          onToggleMaximizeWindow={handleToggleMaximizeWindow}
          onCloseWindow={handleCloseWindow}
        />

        {errorBanner && (
          <div
            role="alert"
            className="shrink-0 bg-[#fef1f1] border-b border-danger px-4 py-2 flex items-center justify-between gap-3 text-[12px] text-danger font-medium"
          >
            <div className="flex items-center gap-2 min-w-0">
              <AlertTriangle className="w-4 h-4 shrink-0" />
              <span className="truncate">{errorBanner}</span>
            </div>
            <button
              type="button"
              onClick={() => setActionError(null)}
              aria-label="Скрыть ошибку"
              className="p-0.5 hover:bg-danger/10 rounded-[2px]"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        )}

        {/* Active Workspace Pane (isolated stacking context keeps Leaflet layers below CommandStrip popovers) */}
        <main className="relative z-0 isolate flex-1 min-h-0 flex overflow-hidden">
          <div
            className={
              activeTab === "workbench"
                ? "flex-1 min-w-0 h-full flex overflow-hidden"
                : "hidden"
            }
          >
            {/* 60% Full-Height Interactive Map */}
            <div className="flex-1 min-w-0 h-full">
              <GeoMapViewport
                cityInfo={activeCityInfo}
                report={lastReport}
                previewListings={previewListings}
                previewTotal={effectivePreviewTotal}
                locationQuery={request.location_query}
                selectedCandidateRank={selectedRank}
                selectedLead={selectedLead}
                focusedCoord={focusedCoord}
                pinnedCenter={pinnedCenter}
                pinPickMode={pinPickMode}
                isActive={activeTab === "workbench"}
                isRunning={isSearchRunning}
                onTogglePinPickMode={(next) =>
                  setPinPickMode((prev) => (next === undefined ? !prev : next))
                }
                onSelectCandidate={(cand: RankedCandidate) =>
                  handleSelectRank(cand.rank)
                }
                onSelectLead={handleSelectLead}
                onOpenPassport={handleOpenDossier}
                onSearchAroundPoint={(lat, lng, label) =>
                  handleSearchAroundPoint(lat, lng, label)
                }
                onRunPinnedSearch={() => handleRunSearch()}
                onClearPinnedCenter={handleClearMapCenter}
              />
            </div>

            {/* 40% Full-Height Synchronized Intelligence & Dossier Sheet */}
            <IntelligenceSheet
              cityName={activeCityInfo?.name || "Санкт-Петербург"}
              cityTotalCount={activeCityInfo?.stats?.total ?? effectivePreviewTotal}
              previewListings={previewListings}
              previewTotal={effectivePreviewTotal}
              previewLoading={previewLoading}
              report={lastReport}
              searchJob={searchJob}
              elapsedSec={elapsedSec}
              selectedRank={selectedRank}
              dossierOpen={dossierOpen}
              selectedLead={selectedLead}
              onSelectRank={handleSelectRank}
              onOpenDossier={handleOpenDossier}
              onCloseDossier={() => setDossierOpen(false)}
              onSelectLead={handleSelectLead}
              onCloseSelectedLead={() => setSelectedLead(null)}
              onExcludeCandidate={handleExcludeCandidate}
              onContinueJob={(id, payload) => {
                void continueJob(id, payload);
              }}
              onCancelSearch={cancelSearch}
              onFocusCoord={focusMapCoord}
              onSearchAroundPoint={(lat, lng, label) =>
                handleSearchAroundPoint(lat, lng, label)
              }
              onOpenLogsTab={() => setActiveTab("logs")}
              onOpenSources={() => setSourcesOpen(true)}
              onRequestSupplyReferences={(jobId) => setSupplyRefJobId(jobId)}
            />
          </div>

          {activeTab === "table" && (
            <div className="flex-1 min-w-0 h-full">
              <ComparisonMatrixView
                cityName={activeCityInfo?.name || "Санкт-Петербург"}
                previewListings={previewListings}
                report={lastReport}
                selectedRank={selectedRank}
                onOpenInWorkbench={handleOpenDossier}
                onFocusLeadOnMap={handleFocusLeadOnMap}
                onSearchAroundPoint={handleSearchAroundPoint}
                onExcludeCandidate={handleExcludeCandidate}
              />
            </div>
          )}

          {activeTab === "logs" && (
            <div className="flex-1 min-w-0 h-full">
              <PipelineLogsView
                searchJob={searchJob}
                collectJob={collectJob}
                report={lastReport}
                cityName={activeCityInfo?.name || "Санкт-Петербург"}
                onRunCollect={(limit) => runCollect(request.city, limit)}
              />
            </div>
          )}
        </main>
      </div>

      {/* Modals for On-Demand Configuration */}
      <SourcesAndSecretsModal
        open={sourcesOpen}
        onClose={() => setSourcesOpen(false)}
        health={health}
        activeCity={request.city}
        collectJob={collectJob}
        onRunCollect={runCollect}
        onSaveSecrets={saveSecrets}
      />

      <ConceptHelperModal
        open={conceptHelperOpen}
        currentRequest={request}
        currentFloorInput={floorInput}
        recentSearches={recentSearches}
        onClearRecentSearches={() => setRecentSearches(clearRecentSearches())}
        onApply={(nextReq, nextFloor, sourceFreeText) => {
          lastConceptFreeTextRef.current = {
            concept: nextReq.business_concept,
            freeText: sourceFreeText?.trim() || nextReq.business_concept
          };
          if (nextReq.center_lat == null && nextReq.location_query?.trim()) {
            prePinLocationRef.current = nextReq.location_query.trim();
          }
          if (!isSearchRunning) {
            resetStaleReportSelection();
          }
          setRequest(nextReq);
          setFloorInput(nextFloor);
        }}
        onClose={() => setConceptHelperOpen(false)}
      />

      <SupplyReferencesModal
        open={Boolean(supplyRefJobId)}
        initialCostLimit={request.full_cost_limit}
        onClose={() => setSupplyRefJobId(null)}
        onSubmit={(limit) => {
          setRequest((prev) => ({ ...prev, full_cost_limit: limit }));
          if (supplyRefJobId) {
            void continueJob(supplyRefJobId, {
              action: "supply_references",
              full_cost_limit: limit
            });
          }
        }}
      />
    </div>
  );
}
