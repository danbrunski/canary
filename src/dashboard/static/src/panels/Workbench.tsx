import { AreaBar } from "../components/AreaBar";
import { PanelErrorBoundary } from "../components/PanelErrorBoundary";
import { memo, useEffect, useRef } from "react";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { CompanionSettings } from "../hooks/useCompanionSettings";
import type { ScoresBySource } from "../hooks/useScoreStream";
import { useTuningContext, type TuningContext } from "../hooks/useTuningContext";
import { useAreaMemory, useSidebarCollapsed } from "../hooks/useAreaMemory";
import { useIsNarrow } from "../hooks/useMediaQuery";
import {
  DEFAULT_ITEM,
  NAV,
  defaultItemForGroup,
  findItem,
  groupOf,
  itemForGroup,
  phoneTabFor,
} from "../nav";
import {
  cn,
  navItemClass,
  pageChipClass,
  pageStripClass,
  phoneWorkbenchClass,
  responsiveGridClass,
  scrollPaneClass,
  sidebarClass,
  sidebarToggleClass,
  workbenchBodyClass,
  workbenchClass,
  workbenchContentClass,
} from "../styles";
import type {
  AnomalyEvent,
  AppMode,
  BehaviorLiveResponse,
  BehaviorModelsResponse,
  BindingsByChannel,
  StatusResponse,
  WorkbenchItem,
} from "../types";
import { AlarmsPanel } from "./AlarmsPanel";
import { AnomalyPanel } from "./AnomalyPanel";
import { BehaviorModelsPanel } from "./BehaviorModelsPanel";
import { BrainPage } from "./BrainPage";
import { ChartsPanel } from "./ChartsPanel";
import { CaptureProfileEditor } from "./CaptureProfileEditor";
import { ControlPanel } from "./ControlPanel";
import { CustomDashboardPanel } from "./CustomDashboardPanel";
import { CustomPidsPanel } from "./CustomPidsPanel";
import { DataSourcesPanel } from "./DataSourcesPanel";
import { DatasetHealthPanel } from "./DatasetHealthPanel";
import { DiagnosticFunctionsPage } from "./DiagnosticFunctionsPage";
import { DtcPanel } from "./DtcPanel";
import { ExplorePanel } from "./ExplorePanel";
import { ExportPanel } from "./ExportPanel";
import { LlmAssistPanel } from "./LlmAssistPanel";
import { ModelHealthPanel } from "./ModelHealthPanel";
import { ModelPanel } from "./ModelPanel";
import { MonitorsPanel } from "./MonitorsPanel";
import { NetworkPanel } from "./NetworkPanel";
import { PlaceholderPanel } from "./PlaceholderPanel";
import { PairedDevicesPanel } from "./PairedDevicesPanel";
import { PowerPanel } from "./PowerPanel";
import { ProfilesPanel } from "./ProfilesPanel";
import { SessionsPanel } from "./SessionsPanel";
import { SystemPanel } from "./SystemPanel";
import { TestSessionPanel } from "./TestSessionPanel";
import { TrainingPanel } from "./TrainingPanel";
import { TrendsPanel } from "./TrendsPanel";
import { TripPanel } from "./TripPanel";
import { TuningPanel } from "./TuningPanel";
import { TpmsPanel } from "./TpmsPanel";
import { TreadWearCard } from "./TreadWearCard";
import { UpdateCard } from "./UpdateCard";
import { VehicleLedgerPage } from "./VehicleLedgerPage";
import { AnalysisPage } from "./tuning/AnalysisPage";
import { LoggingPage } from "./tuning/LoggingPage";
import { LogsPage } from "./tuning/LogsPage";
import { CalibrationPage } from "./tuning/CalibrationPage";
import { RomEditorPage } from "./tuning/RomEditorPage";
import { RomLibraryPage } from "./tuning/RomLibraryPage";
import { TuningContextBar } from "./tuning/TuningContextBar";

export interface DashboardData {
  mode: AppMode;
  channels: Map<string, ChannelSnapshot>;
  bindings: BindingsByChannel;
  events: AnomalyEvent[];
  liveScores: ScoresBySource;
  status: StatusResponse | null;
  statusError: Error | null;
  behaviorModels: BehaviorModelsResponse | null;
  behaviorModelsError: Error | null;
  behaviorLive: BehaviorLiveResponse | null;
  /** Read-only here — entry itself lives in the shell header (Task 5.1). */
  token: string;
  companion: CompanionSettings;
}

interface WorkbenchProps {
  active: WorkbenchItem;
  onNavigate: (item: WorkbenchItem) => void;
  data: DashboardData;
  /** Phone layout (below 600px): no sidebar, the shell's bottom bar
   *  navigates instead. */
  phone?: boolean;
}

/** The pages that read no live channel, memoised. Telemetry replaces
 *  `data.channels` up to once a frame and `App` re-renders on each, so an
 *  unmemoised page re-renders at frame rate whether or not it shows a channel.
 *  Their other props (token, status, tuning context, `onNavigate`) are stable
 *  between the updates that matter to them. A page that reads `channels` is
 *  not here: it has to render with them. */
const Pure = {
  AnomalyPanel: memo(AnomalyPanel),
  UpdateCard: memo(UpdateCard),
  TreadWearCard: memo(TreadWearCard),
  VehicleLedgerPage: memo(VehicleLedgerPage),
  BrainPage: memo(BrainPage),
  ModelPanel: memo(ModelPanel),
  BehaviorModelsPanel: memo(BehaviorModelsPanel),
  ControlPanel: memo(ControlPanel),
  TestSessionPanel: memo(TestSessionPanel),
  ProfilesPanel: memo(ProfilesPanel),
  CaptureProfileEditor: memo(CaptureProfileEditor),
  PowerPanel: memo(PowerPanel),
  NetworkPanel: memo(NetworkPanel),
  PairedDevicesPanel: memo(PairedDevicesPanel),
  DtcPanel: memo(DtcPanel),
  DiagnosticFunctionsPage: memo(DiagnosticFunctionsPage),
  MonitorsPanel: memo(MonitorsPanel),
  TrendsPanel: memo(TrendsPanel),
  ExplorePanel: memo(ExplorePanel),
  LlmAssistPanel: memo(LlmAssistPanel),
  TrainingPanel: memo(TrainingPanel),
  TuningPanel: memo(TuningPanel),
  ModelHealthPanel: memo(ModelHealthPanel),
  SessionsPanel: memo(SessionsPanel),
  DatasetHealthPanel: memo(DatasetHealthPanel),
  CustomPidsPanel: memo(CustomPidsPanel),
  ExportPanel: memo(ExportPanel),
  LogsPage: memo(LogsPage),
  AnalysisPage: memo(AnalysisPage),
  RomLibraryPage: memo(RomLibraryPage),
  CalibrationPage: memo(CalibrationPage),
  PlaceholderPanel: memo(PlaceholderPanel),
};

/** The parked workspace: the area bar (one tab per NAV group, W10), the
 *  active area's pages in the sidebar, and the active panel. Every panel —
 *  current and Phase-6 — has exactly one home here (see `nav.ts`), so adding
 *  the next panel never reignites the "which tab?" question. The sidebar
 *  collapses to a rail for pages that want the width. */
export function Workbench({ active, onNavigate, data, phone = false }: WorkbenchProps) {
  const content = useRef<HTMLDivElement>(null);
  const area = groupOf(active);
  const memory = useAreaMemory(active);
  const [collapsed, setCollapsed] = useSidebarCollapsed();
  // Below 900px the sidebar is a strip of the area's pages and never a rail.
  const narrow = useIsNarrow();
  const railed = collapsed && !narrow;
  // Engine Tuning group state (W02.5): owned here, above the pages, so the
  // selected log and loaded ROM survive navigation between them.
  const tuning = useTuningContext();
  // The panel column keeps its own scroll offset, so a new panel would open
  // mid-scroll at wherever the previous one was left.
  useEffect(() => {
    if (content.current) content.current.scrollTop = 0;
  }, [active]);
  const body = (
    <>
      {active.startsWith("tuning-") && (
        <div className="mb-[1.15rem] max-[600px]:mb-3">
          <TuningContextBar status={data.status} context={tuning} onNavigate={onNavigate} />
        </div>
      )}
      <div className={cn(responsiveGridClass, "grid-cols-1")}>
        <PanelErrorBoundary resetKey={active}>
          {renderItem(active, data, onNavigate, tuning)}
        </PanelErrorBoundary>
      </div>
    </>
  );
  if (phone) {
    // Phone (P2.2): the bottom bar and More sheet replace the sidebar. A
    // bottom-bar entry owning several pages gets a strip to move between them.
    const tab = phoneTabFor("parked", active);
    return (
      <div className={phoneWorkbenchClass}>
        {tab && tab.items.length > 1 && (
          <nav aria-label={`${tab.label} pages`} className={pageStripClass}>
            {tab.items.map((id) => (
              <button
                key={id}
                type="button"
                className={pageChipClass(id === active)}
                aria-current={id === active ? "page" : undefined}
                onClick={() => onNavigate(id)}
              >
                {findItem(id).label}
              </button>
            ))}
          </nav>
        )}
        <div ref={content} className={scrollPaneClass}>
          {body}
        </div>
      </div>
    );
  }
  const pages = NAV.find((group) => group.id === area)?.items ?? [];
  return (
    <div className={workbenchClass}>
      <AreaBar active={area} onChoose={(group) => onNavigate(itemForGroup(group, memory))} />
      <div className={workbenchBodyClass(railed)}>
        <nav className={cn(sidebarClass, railed && "px-1")} aria-label="Workbench sections">
          {!railed &&
            pages.map((item) => (
              <button
                key={item.id}
                type="button"
                className={cn(navItemClass(active === item.id), item.planned && "text-fg-faint")}
                aria-current={active === item.id ? "page" : undefined}
                onClick={() => onNavigate(item.id)}
              >
                {item.label}
                {item.placeholder && (
                  <span className="ml-1.5 text-[0.6rem] text-fg-faint">soon</span>
                )}
                {item.planned && (
                  <span className="ml-1.5 text-[0.6rem] text-fg-faint">planned</span>
                )}
              </button>
            ))}
          <button
            type="button"
            className={cn(sidebarToggleClass, railed && "text-center")}
            aria-expanded={!railed}
            aria-label={railed ? "Expand sidebar" : "Collapse sidebar"}
            title={railed ? "Expand sidebar" : "Collapse sidebar"}
            onClick={() => setCollapsed(!railed)}
          >
            {railed ? "»" : "« Collapse"}
          </button>
        </nav>
        <div ref={content} className={workbenchContentClass}>
          {body}
        </div>
      </div>
    </div>
  );
}

function renderItem(
  item: WorkbenchItem,
  d: DashboardData,
  onNavigate: (item: WorkbenchItem) => void,
  tuning: TuningContext,
) {
  switch (item) {
    case "system-data-sources":
      return <DataSourcesPanel channels={d.channels} status={d.status} />;
    case "diag-anomalies":
      return <Pure.AnomalyPanel events={d.events} token={d.token} liveScores={d.liveScores} />;
    case "system-info":
      return (
        <>
          <SystemPanel
            status={d.status}
            channels={d.channels}
            statusError={d.statusError}
            onNavigate={onNavigate}
          />
          <Pure.UpdateCard token={d.token} />
        </>
      );
    case "diag-tpms":
      return (
        <>
          <TpmsPanel channels={d.channels} />
          <Pure.TreadWearCard token={d.token} />
        </>
      );
    case "diag-ledger":
      return <Pure.VehicleLedgerPage token={d.token} />;
    case "diag-brain":
      return <Pure.BrainPage token={d.token} />;
    case "models-overview":
      return <Pure.ModelPanel status={d.status} events={d.events} />;
    case "models-behavior":
      return (
        <Pure.BehaviorModelsPanel
          data={d.behaviorModels}
          error={d.behaviorModelsError}
          live={d.behaviorLive}
        />
      );
    case "system-control":
      return <Pure.ControlPanel token={d.token} companion={d.companion} />;
    case "system-test":
      return <Pure.TestSessionPanel token={d.token} />;
    case "system-profiles":
      return <Pure.ProfilesPanel token={d.token} />;
    case "system-capture-profiles":
      return <Pure.CaptureProfileEditor token={d.token} status={d.status} />;
    case "system-power":
      return <Pure.PowerPanel token={d.token} />;
    case "system-network":
      return <Pure.NetworkPanel token={d.token} status={d.status} />;
    case "system-paired-devices":
      return <Pure.PairedDevicesPanel token={d.token} />;
    case "observe-charts":
      return <ChartsPanel channels={d.channels} token={d.token} />;
    case "observe-alarms":
      return <AlarmsPanel channels={d.channels} />;
    case "observe-builder":
      return <CustomDashboardPanel channels={d.channels} />;
    case "observe-trip":
      return <TripPanel channels={d.channels} status={d.status} token={d.token} />;
    case "diag-dtc":
      return <Pure.DtcPanel />;
    case "diag-functions":
      return <Pure.DiagnosticFunctionsPage token={d.token} />;
    case "diag-monitors":
      return <Pure.MonitorsPanel />;
    case "diag-trends":
      return <Pure.TrendsPanel token={d.token} />;
    case "diag-explore":
      return <Pure.ExplorePanel />;
    case "diag-llm-assist":
      return <Pure.LlmAssistPanel />;
    case "models-training":
      return <Pure.TrainingPanel token={d.token} companion={d.companion} />;
    case "model-thresholds":
      return <Pure.TuningPanel token={d.token} />;
    case "models-health":
      return <Pure.ModelHealthPanel status={d.status} statusError={d.statusError} />;
    case "system-sessions":
      return <Pure.SessionsPanel token={d.token} companion={d.companion} />;
    case "model-dataset-health":
      return <Pure.DatasetHealthPanel token={d.token} />;
    case "system-custom-pids":
      return <Pure.CustomPidsPanel token={d.token} />;
    case "system-export":
      return <Pure.ExportPanel token={d.token} />;
    case "tuning-logging":
      return (
        <LoggingPage
          token={d.token}
          status={d.status}
          channels={d.channels}
          onNavigate={onNavigate}
        />
      );
    case "tuning-logs":
      return <Pure.LogsPage token={d.token} context={tuning} />;
    case "tuning-analysis":
      return <Pure.AnalysisPage token={d.token} status={d.status} context={tuning} />;
    case "tuning-rom-library":
      return <Pure.RomLibraryPage token={d.token} status={d.status} />;
    case "tuning-calibration":
      return <Pure.CalibrationPage token={d.token} status={d.status} />;
    case "tuning-rom-editor":
      return (
        <RomEditorPage
          token={d.token}
          status={d.status}
          channels={d.channels}
          selectedLogId={tuning.selectedLogId}
          onNavigate={onNavigate}
          onLoadedChange={tuning.setLoadedRom}
        />
      );
    case "tuning-flash":
      return (
        <Pure.PlaceholderPanel
          title="Flash"
          task="W07"
          summary="Module programming waits on the server-side flash interlocks."
        />
      );
  }
}

export { DEFAULT_ITEM, defaultItemForGroup };
