import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
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
  BehaviorModelsResponse,
  BindingsByChannel,
  StatusResponse,
  WorkbenchItem,
} from "../types";
import { AnomalyPanel } from "./AnomalyPanel";
import { BehaviorModelsPanel } from "./BehaviorModelsPanel";
import { ControlPanel } from "./ControlPanel";
import { DatasetHealthPanel } from "./DatasetHealthPanel";
import { ModelPanel } from "./ModelPanel";
import { ObdCoveragePanel } from "./ObdCoveragePanel";
import { PlaceholderPanel } from "./PlaceholderPanel";
import { TrainingPanel } from "./TrainingPanel";
import { TuningPanel } from "./TuningPanel";
import { SystemPanel } from "./SystemPanel";
import { TelemetryPanel } from "./TelemetryPanel";
import { TpmsPanel } from "./TpmsPanel";

export interface DashboardData {
  mode: AppMode;
  channels: Map<string, ChannelSnapshot>;
  bindings: BindingsByChannel;
  events: AnomalyEvent[];
  status: StatusResponse | null;
  statusError: Error | null;
  behaviorModels: BehaviorModelsResponse | null;
  behaviorModelsError: Error | null;
  token: string;
  onTokenChange: (token: string) => void;
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
        {renderItem(active, data)}
      </div>
    </div>
  );
}

function renderItem(item: WorkbenchItem, d: DashboardData) {
  switch (item) {
    case "live-gauges":
      return <TelemetryPanel channels={d.channels} bindings={d.bindings} />;
    case "diag-anomalies":
      return <AnomalyPanel events={d.events} token={d.token} />;
    case "diag-system":
      return (
        <SystemPanel
          status={d.status}
          channels={d.channels}
          statusError={d.statusError}
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
        />
      );
    case "control-main":
      return (
        <ControlPanel token={d.token} onTokenChange={d.onTokenChange} />
      );
    case "live-charts":
      return (
        <PlaceholderPanel
          title="Advanced charting"
          task="6.10"
          summary="Multi-channel trend charts with selectable axes, zoom, time-window selection, min/max/avg overlays and window export. Until then, Gauges carries the live sparkline + chart."
        />
      );
    case "live-alarms":
      return (
        <PlaceholderPanel
          title="User alarms"
          task="6.11"
          summary="Per-channel threshold alarms with debounce / hysteresis / cool-down, audible alerts and history, visually distinct from ML anomalies."
        />
      );
    case "diag-coverage":
      return <ObdCoveragePanel />;
    case "diag-dtc":
      return (
        <PlaceholderPanel
          title="Trouble codes"
          task="6.4"
          summary="Stored / pending / permanent DTCs with described, timestamped, exportable snapshots (curated + structural code lookup). Live Mode 03/07/0A read is hardware-gated."
        />
      );
    case "diag-did":
      return (
        <PlaceholderPanel
          title="Nissan DID workbench"
          task="6.16"
          summary="Known DIDs with confidence, units, ranges, live value, raw hex, evidence and last-seen, plus a parked-only sweep launcher. Gated on the 6.15 live DID profile."
        />
      );
    case "models-training":
      return <TrainingPanel token={d.token} />;
    case "models-tuning":
      return <TuningPanel token={d.token} />;
    case "models-health":
      return (
        <PlaceholderPanel
          title="Drift & inference health"
          task="6.27"
          endpoint="GET /status.health"
          summary="Feature/recon drift, inference latency, missing-feature rate, model-swap outcomes and host load. A live summary is already in the status strip above."
        />
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
    case "data-export":
      return (
        <PlaceholderPanel
          title="Dataset export"
          task="6.20"
          summary="Mark training candidates, pick channels, resample, include/exclude tag windows and export CSV/JSONL/Parquet/feature-matrix/ZIP with a provenance manifest."
        />
      );
    default:
      return <PlaceholderPanel title="Not found" task="—" summary="Unknown view." />;
  }
}

export { DEFAULT_ITEM, defaultItemForGroup };
