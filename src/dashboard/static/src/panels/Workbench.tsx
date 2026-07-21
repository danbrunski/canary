import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { CompanionSettings } from "../hooks/useCompanionSettings";
import type { ScoresBySource } from "../hooks/useScoreStream";
import { DEFAULT_ITEM, NAV, defaultItemForGroup } from "../nav";
import {
  navGroupLabelClass,
  navItemClass,
  responsiveGridClass,
  sidebarClass,
  workbenchClass,
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
import { SystemPanel } from "./SystemPanel";
import { TestSessionPanel } from "./TestSessionPanel";
import { TrainingPanel } from "./TrainingPanel";
import { TrendsPanel } from "./TrendsPanel";
import { TripPanel } from "./TripPanel";
import { TuningPanel } from "./TuningPanel";
import { TpmsPanel } from "./TpmsPanel";

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
  token: string;
  onTokenChange: (token: string) => void;
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
                className={navItemClass(active === item.id)}
                aria-current={active === item.id ? "page" : undefined}
                onClick={() => onNavigate(item.id)}
              >
                {item.label}
                {item.placeholder && (
                  <span className="ml-1.5 text-[0.6rem] text-fg-faint">soon</span>
                )}
              </button>
            ))}
          </div>
        ))}
      </nav>
      <div className={`${responsiveGridClass} grid-cols-1`}>
        {renderItem(active, data, onNavigate)}
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
    case "diag-data":
      return <DataSourcesPanel channels={d.channels} status={d.status} />;
    case "diag-anomalies":
      return (
        <AnomalyPanel events={d.events} token={d.token} liveScores={d.liveScores} />
      );
    case "diag-system":
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
    case "control-main":
      return (
        <ControlPanel
          token={d.token}
          onTokenChange={d.onTokenChange}
          companion={d.companion}
        />
      );
    case "control-test":
      return (
        <TestSessionPanel token={d.token} onTokenChange={d.onTokenChange} />
      );
    case "control-profiles":
      return <ProfilesPanel token={d.token} />;
    case "control-power":
      return <PowerPanel token={d.token} />;
    case "live-charts":
      return <ChartsPanel channels={d.channels} token={d.token} />;
    case "live-alarms":
      return <AlarmsPanel channels={d.channels} />;
    case "live-builder":
      return <CustomDashboardPanel channels={d.channels} />;
    case "live-trip":
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
    case "models-tuning":
      return <TuningPanel token={d.token} />;
    case "models-health":
      return (
        <ModelHealthPanel status={d.status} statusError={d.statusError} />
      );
    case "data-sessions":
      return (
        <PlaceholderPanel
          title="Sessions"
          task="6.13"
          endpoint="GET /control/sessions"
          summary="Browse / pin / export / delete captured sessions. The Control Plane panel can already drive these via the authenticated API."
        />
      );
    case "data-health":
      return <DatasetHealthPanel token={d.token} />;
    case "data-custom-pids":
      return <CustomPidsPanel token={d.token} />;
    case "data-export":
      return <ExportPanel token={d.token} />;
    default:
      return <PlaceholderPanel title="Not found" task="—" summary="Unknown view." />;
  }
}

export { DEFAULT_ITEM, defaultItemForGroup };
