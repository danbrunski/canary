import { useEffect, useRef } from "react";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { CompanionSettings } from "../hooks/useCompanionSettings";
import type { ScoresBySource } from "../hooks/useScoreStream";
import { DEFAULT_ITEM, NAV, defaultItemForGroup } from "../nav";
import {
  cn,
  navGroupLabelClass,
  navItemClass,
  responsiveGridClass,
  sidebarClass,
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
import { ChartsPanel } from "./ChartsPanel";
import { CaptureProfilesPanel } from "./CaptureProfilesPanel";
import { ControlPanel } from "./ControlPanel";
import { CustomDashboardPanel } from "./CustomDashboardPanel";
import { CustomPidsPanel } from "./CustomPidsPanel";
import { DataSourcesPanel } from "./DataSourcesPanel";
import { DatasetHealthPanel } from "./DatasetHealthPanel";
import { DtcPanel } from "./DtcPanel";
import { ExplorePanel } from "./ExplorePanel";
import { ExportPanel } from "./ExportPanel";
import { LlmAssistPanel } from "./LlmAssistPanel";
import { ModelHealthPanel } from "./ModelHealthPanel";
import { ModelPanel } from "./ModelPanel";
import { MonitorsPanel } from "./MonitorsPanel";
import { PlaceholderPanel } from "./PlaceholderPanel";
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
import { RomMapsPanel } from "./RomMapsPanel";
import { LoggingPage } from "./tuning/LoggingPage";

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
}

/** The parked workspace: grouped sidebar nav + the active panel. Every
 *  panel — current and Phase-6 — has exactly one home here (see `nav.ts`),
 *  so adding the next panel never reignites the "which tab?" question. */
export function Workbench({ active, onNavigate, data }: WorkbenchProps) {
  const content = useRef<HTMLDivElement>(null);
  // The panel column keeps its own scroll offset, so a new panel would open
  // mid-scroll at wherever the previous one was left.
  useEffect(() => {
    if (content.current) content.current.scrollTop = 0;
  }, [active]);
  return (
    <div className={workbenchClass}>
      <nav className={sidebarClass} aria-label="Workbench sections">
        {NAV.map((group) => (
          <div key={group.id} className="flex flex-col gap-1">
            <span className={navGroupLabelClass}>{group.label}</span>
            {group.items.map((item) => (
              <button
                key={item.id}
                type="button"
                className={cn(
                  navItemClass(active === item.id),
                  item.planned && "text-fg-faint",
                )}
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
          </div>
        ))}
      </nav>
      <div ref={content} className={workbenchContentClass}>
        <div className={cn(responsiveGridClass, "grid-cols-1")}>
          {renderItem(active, data, onNavigate)}
        </div>
      </div>
    </div>
  );
}

function renderItem(
  item: WorkbenchItem,
  d: DashboardData,
  onNavigate: (item: WorkbenchItem) => void,
) {
  switch (item) {
    case "tuning-rom-maps":
      return <RomMapsPanel token={d.token} channels={d.channels} status={d.status} />;
    case "system-data-sources":
      return <DataSourcesPanel channels={d.channels} status={d.status} />;
    case "diag-anomalies":
      return (
        <AnomalyPanel events={d.events} token={d.token} liveScores={d.liveScores} />
      );
    case "system-info":
      return (
        <SystemPanel
          status={d.status}
          channels={d.channels}
          statusError={d.statusError}
          onNavigate={onNavigate}
        />
      );
    case "diag-tpms":
      return <TpmsPanel channels={d.channels} />;
    case "models-overview":
      return <ModelPanel status={d.status} events={d.events} />;
    case "models-behavior":
      return (
        <BehaviorModelsPanel
          data={d.behaviorModels}
          error={d.behaviorModelsError}
          live={d.behaviorLive}
        />
      );
    case "system-control":
      return <ControlPanel token={d.token} companion={d.companion} />;
    case "system-test":
      return <TestSessionPanel token={d.token} />;
    case "system-profiles":
      return <ProfilesPanel token={d.token} />;
    case "system-capture-profiles":
      return <CaptureProfilesPanel token={d.token} status={d.status} channels={d.channels} />;
    case "system-power":
      return <PowerPanel token={d.token} />;
    case "observe-charts":
      return <ChartsPanel channels={d.channels} token={d.token} />;
    case "observe-alarms":
      return <AlarmsPanel channels={d.channels} />;
    case "observe-builder":
      return <CustomDashboardPanel channels={d.channels} />;
    case "observe-trip":
      return <TripPanel channels={d.channels} status={d.status} token={d.token} />;
    case "diag-dtc":
      return <DtcPanel />;
    case "diag-monitors":
      return <MonitorsPanel />;
    case "diag-trends":
      return <TrendsPanel token={d.token} />;
    case "diag-explore":
      return <ExplorePanel />;
    case "diag-llm-assist":
      return <LlmAssistPanel />;
    case "models-training":
      return <TrainingPanel token={d.token} companion={d.companion} />;
    case "model-thresholds":
      return <TuningPanel token={d.token} />;
    case "models-health":
      return (
        <ModelHealthPanel status={d.status} statusError={d.statusError} />
      );
    case "system-sessions":
      return <SessionsPanel token={d.token} companion={d.companion} />;
    case "model-dataset-health":
      return <DatasetHealthPanel token={d.token} />;
    case "system-custom-pids":
      return <CustomPidsPanel token={d.token} />;
    case "system-export":
      return <ExportPanel token={d.token} />;
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
      return <PlaceholderPanel title="Logs" task="W04" summary="Recorded tuning-log tables and charts will live here." />;
    case "tuning-analysis":
      return <PlaceholderPanel title="Analysis" task="W04" summary="Delivery-error, trim, and timing analysis will live here." />;
    case "tuning-calibration":
      return <PlaceholderPanel title="Calibration" task="W06" summary="Flash history and memory-channel verification will live here." />;
    case "tuning-rom-editor":
      return <PlaceholderPanel title="ROM Editor" task="planned" summary="ROM editing is reserved for a later implementation." />;
    case "tuning-flash":
      return <PlaceholderPanel title="Flash" task="W07" summary="Module programming waits on the server-side flash interlocks." />;
  }
}

export { DEFAULT_ITEM, defaultItemForGroup };
