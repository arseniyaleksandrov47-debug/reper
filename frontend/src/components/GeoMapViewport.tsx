import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import { Crosshair, LocateFixed, Play, X } from "lucide-react";
import type {
  CityInfo,
  CompetitorRecord,
  ListingPreview,
  RankedCandidate,
  SearchReport,
  SelectedLeadTarget,
  SelectedMapPoint
} from "../types/geoestate";
import { formatNumber } from "../utils/formatters";
import {
  buildDeferredPinHtml,
  buildRankPinHtml,
  buildSearchAnchorPinHtml,
  candidateTooltipHtml,
  collectReportLatLngs,
  competitorTooltipHtml,
  deferredTooltipHtml,
  extractCompetitorGroups,
  previewTooltipHtml
} from "../utils/mapMarkers";
import { MapPointCard } from "./MapPointCard";

export interface GeoMapViewportProps {
  readonly cityInfo: CityInfo | undefined;
  readonly report: SearchReport | null;
  readonly previewListings: readonly ListingPreview[];
  readonly previewTotal: number;
  readonly locationQuery?: string | null;
  readonly selectedCandidateRank: number | null;
  readonly selectedLead: SelectedLeadTarget | null;
  readonly focusedCoord: { lat: number; lng: number; label?: string; seq: number } | null;
  readonly pinnedCenter: { lat: number; lng: number; radiusM: number } | null;
  readonly pinPickMode: boolean;
  readonly isActive?: boolean;
  readonly isRunning?: boolean;
  readonly onTogglePinPickMode: (next?: boolean) => void;
  readonly onSelectCandidate: (cand: RankedCandidate) => void;
  readonly onSelectLead: (lead: SelectedLeadTarget) => void;
  readonly onOpenPassport: (rank: number) => void;
  readonly onSearchAroundPoint: (lat: number, lng: number, label?: string) => void;
  readonly onRunPinnedSearch?: () => void;
  readonly onClearPinnedCenter: () => void;
}

type MapProvider = "osm" | "satellite";

interface DisplayCoordEntry {
  readonly lat: number;
  readonly lng: number;
  readonly groupCount: number;
}

/**
 * Compute deterministic spiral micro-offsets when multiple listings share identical building coordinates
 * so overlapping markers can each be seen and clicked individually.
 */
function buildOffsetCoordLookup<T extends object>(
  entries: ReadonlyArray<{ item: T; lat: number | null | undefined; lng: number | null | undefined }>
): Map<object, DisplayCoordEntry> {
  const buckets = new Map<
    string,
    Array<{ item: T; lat: number; lng: number }>
  >();

  for (const e of entries) {
    if (e.lat == null || e.lng == null) continue;
    const key = `${e.lat.toFixed(5)}:${e.lng.toFixed(5)}`;
    const list = buckets.get(key) ?? [];
    list.push({ item: e.item, lat: e.lat, lng: e.lng });
    buckets.set(key, list);
  }

  const resolved = new Map<object, DisplayCoordEntry>();
  for (const group of buckets.values()) {
    if (group.length === 1) {
      const only = group[0];
      resolved.set(only.item, { lat: only.lat, lng: only.lng, groupCount: 1 });
      continue;
    }
    const radiusDeg = Math.min(0.00022, 0.0001 + group.length * 0.000015);
    const cosLat = Math.max(0.35, Math.cos((group[0].lat * Math.PI) / 180));
    group.forEach((entry, idx) => {
      const angle = (2 * Math.PI * idx) / group.length - Math.PI / 2;
      const dLat = radiusDeg * Math.sin(angle);
      const dLng = (radiusDeg / cosLat) * Math.cos(angle);
      resolved.set(entry.item, {
        lat: entry.lat + dLat,
        lng: entry.lng + dLng,
        groupCount: group.length
      });
    });
  }
  return resolved;
}

function confirmationTone(status?: string): "emerald" | "amber" | "crimson" {
  if (status === "source_conditions_checked" || status === "verified") return "emerald";
  if (status === "not_suitable" || status === "rejected") return "crimson";
  return "amber";
}

export function GeoMapViewport({
  cityInfo,
  report,
  previewListings,
  previewTotal,
  locationQuery,
  selectedCandidateRank,
  selectedLead,
  focusedCoord,
  pinnedCenter,
  pinPickMode,
  isActive = true,
  isRunning = false,
  onTogglePinPickMode,
  onSelectCandidate,
  onSelectLead,
  onOpenPassport,
  onSearchAroundPoint,
  onRunPinnedSearch,
  onClearPinnedCenter
}: GeoMapViewportProps) {
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<L.Map | null>(null);
  const tileRef = useRef<L.TileLayer | null>(null);
  const overlayRef = useRef<L.LayerGroup | null>(null);
  const selectionLayerRef = useRef<L.LayerGroup | null>(null);
  const previewRendererRef = useRef<L.Canvas | null>(null);

  const [provider, setProvider] = useState<MapProvider>("osm");
  const [showRadii, setShowRadii] = useState(true);
  const [showCompetitors, setShowCompetitors] = useState(true);
  const [showDeferred, setShowDeferred] = useState(true);
  const [fitTrigger, setFitTrigger] = useState(0);
  const [activePoint, setActivePoint] = useState<SelectedMapPoint | null>(null);
  const [tileNotice, setTileNotice] = useState<string | null>(null);

  const selectedCand = useMemo(
    () =>
      report?.ranked_candidates.find((c) => c.rank === selectedCandidateRank) ||
      report?.ranked_candidates[0] ||
      null,
    [report, selectedCandidateRank]
  );

  const displayCoordMap = useMemo(() => {
    if (!report) {
      return buildOffsetCoordLookup(
        previewListings.map((item) => ({
          item,
          lat: item.lat,
          lng: item.lng
        }))
      );
    }
    return buildOffsetCoordLookup<object>([
      ...report.ranked_candidates.map((c) => ({
        item: c,
        lat: c.property_data.lat,
        lng: c.property_data.lng
      })),
      ...report.floor_verification_candidates.map((d) => ({
        item: d,
        lat: d.lat,
        lng: d.lng
      }))
    ]);
  }, [previewListings, report]);

  // Track available pane dimensions so Leaflet always fills 100% cleanly
  useEffect(() => {
    const el = wrapperRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      mapRef.current?.invalidateSize({ animate: false });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Re-measure Leaflet viewport immediately after switching back from Table/Logs tabs
  useEffect(() => {
    if (!isActive) return;
    const rafId = window.requestAnimationFrame(() => {
      mapRef.current?.invalidateSize({ animate: false });
    });
    return () => window.cancelAnimationFrame(rafId);
  }, [isActive]);

  // Dismiss stale map quick-card when a new calculation begins or finishes
  useEffect(() => {
    if (isRunning) {
      setActivePoint(null);
    }
  }, [isRunning]);

  useEffect(() => {
    setActivePoint(null);
  }, [report?.job_id]);

  // Keep refs fresh inside Leaflet click listeners
  const pinModeRef = useRef(pinPickMode);
  pinModeRef.current = pinPickMode;
  const searchAroundRef = useRef(onSearchAroundPoint);
  searchAroundRef.current = onSearchAroundPoint;
  const togglePinModeRef = useRef(onTogglePinPickMode);
  togglePinModeRef.current = onTogglePinPickMode;
  const selectCandidateRef = useRef(onSelectCandidate);
  selectCandidateRef.current = onSelectCandidate;
  const selectLeadRef = useRef(onSelectLead);
  selectLeadRef.current = onSelectLead;
  const openPassportRef = useRef(onOpenPassport);
  openPassportRef.current = onOpenPassport;

  const providerTabs = useMemo(
    () => [
      { id: "osm" as const, label: "Схема", hint: "OpenStreetMap" },
      { id: "satellite" as const, label: "Спутник", hint: "Спутниковая съёмка" }
    ],
    []
  );

  // Initialize Leaflet instance once
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const initialCenter: [number, number] = report
      ? [report.center_lat, report.center_lng]
      : cityInfo?.center ?? [59.9391, 30.3159];

    const map = L.map(containerRef.current, {
      center: initialCenter,
      zoom: report ? 13 : 11,
      zoomControl: false,
      attributionControl: false
    });

    map.on("click", (ev: L.LeafletMouseEvent) => {
      if (pinModeRef.current) {
        searchAroundRef.current(
          Number(ev.latlng.lat.toFixed(6)),
          Number(ev.latlng.lng.toFixed(6)),
          "Точка на карте"
        );
        togglePinModeRef.current(false);
        return;
      }
      setActivePoint(null);
    });

    L.control.zoom({ position: "bottomright" }).addTo(map);
    previewRendererRef.current = L.canvas({ padding: 0.5 });
    overlayRef.current = L.layerGroup().addTo(map);
    selectionLayerRef.current = L.layerGroup().addTo(map);
    mapRef.current = map;

    return () => {
      map.remove();
      mapRef.current = null;
      tileRef.current = null;
      overlayRef.current = null;
      selectionLayerRef.current = null;
      previewRendererRef.current = null;
    };
  }, []);

  // Switch tile layers cleanly
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    if (tileRef.current) {
      map.removeLayer(tileRef.current);
      tileRef.current = null;
    }
    setTileNotice(null);

    if (provider === "satellite") {
      tileRef.current = L.tileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        { maxZoom: 19 }
      ).addTo(map);
      return;
    }

    // Primary: OSM standard tiles; Fallback: ArcGIS Street tiles if OSM tile rate-limited
    let errors = 0;
    const primary = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19
    });
    primary.on("tileerror", () => {
      errors += 1;
      if (errors === 2 && mapRef.current) {
        mapRef.current.removeLayer(primary);
        tileRef.current = L.tileLayer(
          "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
          { maxZoom: 19 }
        ).addTo(mapRef.current);
        setTileNotice("Переключено на резервный картографический слой");
      }
    });
    tileRef.current = primary.addTo(map);
  }, [provider]);

  const isWholeCityZone = useMemo(() => {
    const q = (locationQuery ?? "").trim().toLowerCase().replace(/ё/g, "е");
    return (
      !q ||
      /^(?:весь город|любой|все|all|спб|мск|москва|санкт-петербург|петербург|питер)$/.test(
        q
      )
    );
  }, [locationQuery]);

  // Recenter when city changes before first report
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !cityInfo || report) return;
    map.setView(cityInfo.center, 11, { animate: false });
    setActivePoint(null);
  }, [cityInfo?.key]);

  // Frame report candidates, pinned circle, narrowed zone preview, or whole city
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    if (report) {
      const pts = collectReportLatLngs(report);
      if (pts.length > 1) {
        map.fitBounds(L.latLngBounds(pts).pad(0.18), { maxZoom: 15, animate: true });
      } else {
        map.setView([report.center_lat, report.center_lng], 13, { animate: true });
      }
      return;
    }

    if (pinnedCenter) {
      const bounds = L.latLng(pinnedCenter.lat, pinnedCenter.lng).toBounds(
        Math.max(450, pinnedCenter.radiusM * 2.2)
      );
      map.fitBounds(bounds, { maxZoom: 15, animate: true });
      return;
    }

    if (!isWholeCityZone) {
      const zonePts = previewListings
        .filter((x) => x.lat != null && x.lng != null)
        .map((x) => [x.lat!, x.lng!] as [number, number]);
      if (zonePts.length > 1) {
        map.fitBounds(L.latLngBounds(zonePts).pad(0.2), { maxZoom: 14, animate: true });
        return;
      }
      if (zonePts.length === 1) {
        map.setView(zonePts[0], 14, { animate: true });
        return;
      }
    }

    if (cityInfo) {
      map.setView(cityInfo.center, 11, { animate: true });
    }
  }, [
    report?.job_id,
    fitTrigger,
    pinnedCenter?.lat,
    pinnedCenter?.lng,
    pinnedCenter?.radiusM,
    isWholeCityZone,
    locationQuery,
    previewListings.length
  ]);

  // Smooth pan to selected dossier / focused competitor
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !focusedCoord) return;
    map.panTo([focusedCoord.lat, focusedCoord.lng], { animate: true });
  }, [focusedCoord?.seq]);

  // Keep bottom-left map quick-card synchronized when a lead is selected externally (from list/table)
  useEffect(() => {
    if (!selectedLead) return;
    if (selectedLead.kind === "preview") {
      setActivePoint({ kind: "preview", listing: selectedLead.listing });
    } else if (selectedLead.kind === "deferred") {
      setActivePoint({ kind: "deferred", candidate: selectedLead.candidate });
    }
  }, [selectedLead]);

  // Draw highlighted target halo over the actively inspected map marker
  useEffect(() => {
    const selGroup = selectionLayerRef.current;
    if (!selGroup) return;
    selGroup.clearLayers();

    let lat: number | null | undefined = null;
    let lng: number | null | undefined = null;
    let targetObj: object | null = null;
    if (selectedLead) {
      const target =
        selectedLead.kind === "preview" ? selectedLead.listing : selectedLead.candidate;
      targetObj = target;
      lat = target.lat;
      lng = target.lng;
    } else if (activePoint?.kind === "preview") {
      targetObj = activePoint.listing;
      lat = activePoint.listing.lat;
      lng = activePoint.listing.lng;
    } else if (activePoint?.kind === "deferred") {
      targetObj = activePoint.candidate;
      lat = activePoint.candidate.lat;
      lng = activePoint.candidate.lng;
    }

    const offsetPt = targetObj ? displayCoordMap.get(targetObj) : undefined;
    const resolvedLat = offsetPt?.lat ?? lat;
    const resolvedLng = offsetPt?.lng ?? lng;

    if (resolvedLat != null && resolvedLng != null) {
      L.circleMarker([resolvedLat, resolvedLng], {
        radius: 11,
        color: "#d13b27",
        weight: 2,
        fillColor: "#d13b27",
        fillOpacity: 0.22,
        interactive: false
      }).addTo(selGroup);
      L.circleMarker([resolvedLat, resolvedLng], {
        radius: 6.5,
        color: "#181b1f",
        weight: 2,
        fillColor: "#d13b27",
        fillOpacity: 1,
        interactive: false
      }).addTo(selGroup);
    }
  }, [selectedLead, activePoint, displayCoordMap]);

  // Draw all spatial overlays
  useEffect(() => {
    const map = mapRef.current;
    const group = overlayRef.current;
    if (!map || !group) return;
    group.clearLayers();

    // Pinned search anchor preview circle even before search runs
    if (pinnedCenter) {
      L.circle([pinnedCenter.lat, pinnedCenter.lng], {
        radius: pinnedCenter.radiusM,
        color: "#d13b27",
        weight: 1.5,
        dashArray: "6 5",
        fillColor: "#d13b27",
        fillOpacity: 0.06
      }).addTo(group);

      const anchorIcon = L.divIcon({
        className: "geoestate-anchor-pin",
        html: buildSearchAnchorPinHtml(),
        iconSize: [22, 22],
        iconAnchor: [11, 11]
      });
      L.marker([pinnedCenter.lat, pinnedCenter.lng], { icon: anchorIcon, interactive: false }).addTo(
        group
      );
    }

    if (!report) {
      for (const item of previewListings) {
        if (item.lat == null || item.lng == null) continue;
        const pt = displayCoordMap.get(item) ?? {
          lat: item.lat,
          lng: item.lng,
          groupCount: 1
        };
        const marker = L.circleMarker([pt.lat, pt.lng], {
          renderer: previewRendererRef.current ?? undefined,
          radius: 5.5,
          color: "#181b1f",
          weight: 1.4,
          fillColor: "#fffdf9",
          fillOpacity: 0.95,
          bubblingMouseEvents: false
        });
        marker.bindTooltip(
          pt.groupCount > 1
            ? `${previewTooltipHtml(item)}<div class="mt-0.5 text-[10px] text-[#d13b27] font-mono">В здании лотов: ${pt.groupCount}</div>`
            : previewTooltipHtml(item),
          { direction: "top" }
        );
        marker.on("click", (ev) => {
          L.DomEvent.stopPropagation(ev);
          setActivePoint({ kind: "preview", listing: item });
          selectLeadRef.current({ kind: "preview", listing: item });
        });
        marker.addTo(group);
      }
      return;
    }

    if (showRadii && report.search_radius_m > 0 && report.search_radius_m < 15000) {
      L.circle([report.center_lat, report.center_lng], {
        radius: report.search_radius_m,
        color: "#181b1f",
        weight: 1.5,
        dashArray: "6 5",
        fillColor: "#181b1f",
        fillOpacity: 0.03
      }).addTo(group);
    }

    if (showDeferred) {
      for (const def of report.floor_verification_candidates) {
        if (def.lat == null || def.lng == null) continue;
        const pt = displayCoordMap.get(def) ?? {
          lat: def.lat,
          lng: def.lng,
          groupCount: 1
        };
        const icon = L.divIcon({
          className: "geoestate-def-pin",
          html: buildDeferredPinHtml(false),
          iconSize: [22, 22],
          iconAnchor: [11, 11]
        });
        const m = L.marker([pt.lat, pt.lng], {
          icon,
          bubblingMouseEvents: false
        });
        m.bindTooltip(deferredTooltipHtml(def), { direction: "top" });
        m.on("click", (ev) => {
          L.DomEvent.stopPropagation(ev);
          setActivePoint({ kind: "deferred", candidate: def });
          selectLeadRef.current({ kind: "deferred", candidate: def });
        });
        m.addTo(group);
      }
    }

    if (selectedCand?.property_data.lat != null && selectedCand?.property_data.lng != null) {
      const slat = selectedCand.property_data.lat;
      const slng = selectedCand.property_data.lng;

      if (showRadii) {
        L.circle([slat, slng], {
          radius: Math.max(100, (report.competition_diameter_m || 400) / 2),
          color: "#d13b27",
          weight: 1.5,
          dashArray: "3 4",
          fillColor: "#d13b27",
          fillOpacity: 0.06
        }).addTo(group);
      }

      if (showCompetitors && selectedCand.competition_report) {
        for (const grp of extractCompetitorGroups(selectedCand.competition_report)) {
          for (const comp of grp.items) {
            if (comp.lat == null || comp.lng == null) continue;
            const cMarker = L.circleMarker([comp.lat, comp.lng], {
              radius: 4.5,
              color: "#fffdf9",
              weight: 1.5,
              fillColor: grp.color,
              fillOpacity: 0.92,
              bubblingMouseEvents: false
            });
            cMarker.bindTooltip(competitorTooltipHtml(comp, grp.label), { direction: "top" });
            cMarker.on("click", (ev) => {
              L.DomEvent.stopPropagation(ev);
              setActivePoint({
                kind: "competitor",
                competitor: comp as CompetitorRecord,
                groupLabel: grp.label,
                groupColor: grp.color
              });
            });
            cMarker.addTo(group);
          }
        }
      }
    }

    for (const c of report.ranked_candidates) {
      if (c.property_data.lat == null || c.property_data.lng == null) continue;
      const pt = displayCoordMap.get(c) ?? {
        lat: c.property_data.lat,
        lng: c.property_data.lng,
        groupCount: 1
      };
      const isSelected = c.rank === selectedCandidateRank;
      const tone = confirmationTone(c.confirmation?.status);
      const icon = L.divIcon({
        className: "geoestate-rank-pin",
        html: buildRankPinHtml(c.rank, tone, isSelected),
        iconSize: isSelected ? [36, 26] : [30, 22],
        iconAnchor: isSelected ? [18, 13] : [15, 11]
      });
      const m = L.marker([pt.lat, pt.lng], {
        icon,
        zIndexOffset: isSelected ? 1000 : 500 - c.rank,
        bubblingMouseEvents: false
      });
      m.bindTooltip(candidateTooltipHtml(c), { direction: "top" });
      m.on("click", (ev) => {
        L.DomEvent.stopPropagation(ev);
        selectCandidateRef.current(c);
        openPassportRef.current(c.rank);
        setActivePoint({ kind: "ranked", candidate: c });
      });
      m.addTo(group);
    }
  }, [
    previewListings,
    report,
    selectedCandidateRank,
    selectedCand,
    pinnedCenter,
    showRadii,
    showCompetitors,
    showDeferred,
    displayCoordMap
  ]);

  return (
    <div
      ref={wrapperRef}
      className={`relative w-full h-full bg-subtle overflow-hidden ${
        pinPickMode ? "cursor-crosshair" : ""
      }`}
    >
      {/* Active Map Surface */}
      <div ref={containerRef} className="w-full h-full" />

      {/* Unified Non-Overlapping Top Drafting HUD */}
      <div className="absolute top-3 left-3 right-3 z-[400] flex flex-wrap items-start justify-between gap-2 pointer-events-none">
        <div className="flex flex-wrap items-center gap-1.5 min-w-0">
          <div className="pointer-events-auto bg-paper border border-ink-primary rounded-[2px] px-2.5 h-[32px] flex items-center gap-2 text-[12px]">
            <span className="font-mono font-bold text-ink-primary tabular-nums whitespace-nowrap">
              {cityInfo?.key === "moscow" ? "МСК" : "СПб"} · {formatNumber(previewTotal)}{" "}
              {report ? "в базе" : "по фильтру"}
            </span>
            {report && (
              <>
                <span className="h-4 w-px bg-hairline" />
                <span className="font-mono font-bold text-signal tabular-nums whitespace-nowrap">
                  Отбор {report.evaluated_count}/{report.numerical_matching_count}
                </span>
              </>
            )}
          </div>

          <button
            type="button"
            onClick={() => onTogglePinPickMode()}
            className={`pointer-events-auto h-[32px] px-2.5 border rounded-[2px] text-[12px] font-medium inline-flex items-center gap-1.5 transition-colors whitespace-nowrap ${
              pinPickMode
                ? "bg-signal text-[#fffdf9] border-ink-primary font-semibold"
                : "bg-paper text-ink-primary border-ink-primary hover:bg-subtle"
            }`}
            title="Кликните на карту, чтобы указать центр поиска по радиусу"
          >
            <Crosshair className="w-3.5 h-3.5" />
            <span>{pinPickMode ? "Клик по карте…" : "Точка R"}</span>
          </button>

          {pinnedCenter && (
            <div className="pointer-events-auto bg-[#181b1f] text-[#f8f9fa] border border-[#181b1f] rounded-[2px] pl-2.5 pr-1.5 h-[32px] text-[12px] font-mono inline-flex items-center gap-2 tabular-nums">
              <span className="w-2 h-2 bg-signal inline-block shrink-0" />
              <span className="whitespace-nowrap">
                R {pinnedCenter.radiusM >= 1000 ? `${(pinnedCenter.radiusM / 1000).toFixed(1)} км` : `${pinnedCenter.radiusM} м`}
              </span>
              {onRunPinnedSearch && !isRunning && (
                <button
                  type="button"
                  onClick={onRunPinnedSearch}
                  title="Запустить подбор в выбранной точке"
                  className="px-1.5 py-0.5 rounded-[2px] bg-signal hover:bg-signal-hover text-[#fffdf9] font-sans text-[11px] font-semibold inline-flex items-center gap-1 transition-colors"
                >
                  <Play className="w-2.5 h-2.5 fill-current" />
                  Искать
                </button>
              )}
              <button
                type="button"
                onClick={onClearPinnedCenter}
                aria-label="Сбросить точку поиска"
                className="p-0.5 text-[#adb4bf] hover:text-white"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>

        {/* Provider & Layer Controls */}
        <div className="flex flex-col items-end gap-1.5 ml-auto">
          <div className="flex flex-wrap items-center justify-end gap-1.5">
            <div
              role="group"
              aria-label="Слой карты"
              className="pointer-events-auto bg-paper border border-ink-primary rounded-[2px] p-0.5 flex items-center text-[11px]"
            >
              {providerTabs.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  title={item.hint}
                  onClick={() => setProvider(item.id)}
                  aria-pressed={provider === item.id}
                  className={`px-2.5 py-1 rounded-[1px] font-semibold transition-colors whitespace-nowrap ${
                    provider === item.id
                      ? "bg-ink-primary text-[#f5f3ee]"
                      : "text-ink-primary hover:bg-subtle"
                  }`}
                >
                  {item.label}
                </button>
              ))}
            </div>

            {report && (
              <div className="pointer-events-auto bg-paper border border-ink-primary rounded-[2px] p-0.5 flex items-center text-[11px]">
                <button
                  type="button"
                  onClick={() => setShowRadii((v) => !v)}
                  aria-pressed={showRadii}
                  className={`px-2 py-1 rounded-[1px] font-medium whitespace-nowrap ${
                    showRadii ? "bg-subtle text-ink-primary font-semibold" : "text-ink-muted"
                  }`}
                >
                  Радиусы
                </button>
                <button
                  type="button"
                  onClick={() => setShowCompetitors((v) => !v)}
                  aria-pressed={showCompetitors}
                  className={`px-2 py-1 rounded-[1px] font-medium whitespace-nowrap ${
                    showCompetitors ? "bg-subtle text-ink-primary font-semibold" : "text-ink-muted"
                  }`}
                >
                  Конкуренты
                </button>
                <button
                  type="button"
                  onClick={() => setShowDeferred((v) => !v)}
                  aria-pressed={showDeferred}
                  className={`px-2 py-1 rounded-[1px] font-medium whitespace-nowrap ${
                    showDeferred ? "bg-subtle text-ink-primary font-semibold" : "text-ink-muted"
                  }`}
                >
                  ? ({report.floor_verification_candidates.length})
                </button>
              </div>
            )}

            <button
              type="button"
              onClick={() => setFitTrigger((v) => v + 1)}
              aria-label="Вместить все маркеры"
              title="Вместить все маркеры"
              className="pointer-events-auto h-[30px] w-[30px] bg-paper border border-ink-primary rounded-[2px] flex items-center justify-center text-ink-primary hover:bg-subtle"
            >
              <LocateFixed className="w-4 h-4" />
            </button>
          </div>

          {tileNotice && (
            <div className="pointer-events-auto bg-paper border border-ink-primary rounded-[2px] px-2.5 py-1 text-[11px] font-medium text-ink-secondary">
              {tileNotice}
            </div>
          )}
        </div>
      </div>

      {/* Bottom-Left: Active Inspection Card stacked cleanly above Architectural Legend */}
      <div className="absolute bottom-3 left-3 right-14 z-[400] flex flex-col items-start gap-2 pointer-events-none">
        {activePoint && (
          <div className="pointer-events-auto max-w-full">
            <MapPointCard
              point={activePoint}
              radiusMeters={pinnedCenter?.radiusM ?? report?.search_radius_m ?? 1200}
              onClose={() => setActivePoint(null)}
              onOpenPassport={(rank) => {
                onOpenPassport(rank);
                setActivePoint(null);
              }}
              onOpenLeadCard={(lead) => onSelectLead(lead)}
              onSearchAroundPoint={(lat, lng, label) => {
                onSearchAroundPoint(lat, lng, label);
                setActivePoint(null);
              }}
            />
          </div>
        )}

        <div className="pointer-events-auto bg-paper border border-ink-primary rounded-[2px] px-3 py-1.5 flex flex-wrap items-center gap-3 text-[11px] font-medium text-ink-secondary">
          <span className="inline-flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-[1px] bg-signal border border-ink-primary" />
            Выбранный лот
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-[1px] bg-ink-primary border border-verified" />
            Допуск ок
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-[1px] bg-ink-primary border border-warn" />
            Уточнить вход
          </span>
          {report && (
            <>
              <span className="inline-flex items-center gap-1.5">
                <span className="w-2 h-2 rounded-full bg-signal" />
                Конкурент
              </span>
              <span className="inline-flex items-center gap-1.5">
                <span className="w-2 h-2 rounded-full bg-verified" />
                Синергия
              </span>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
