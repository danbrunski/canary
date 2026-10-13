import { Component, type ErrorInfo, type ReactNode } from "react";

import { buttonClass, cn, noticeClass } from "../styles";

interface PanelErrorBoundaryProps {
  /** Changing it (the page id) clears a caught error: a crash on one page
   *  must not follow the user to the next. */
  resetKey: string;
  children: ReactNode;
}

interface PanelErrorBoundaryState {
  error: Error | null;
}

/** Contains a panel's render crash to that panel.
 *
 *  Without it, one malformed daemon response (an empty `/control/power`, an
 *  empty `/obd/dtc`) threw during render and React unmounted the whole app:
 *  a blank screen, worst on a phone where reload is the only way back. */
export class PanelErrorBoundary extends Component<
  PanelErrorBoundaryProps,
  PanelErrorBoundaryState
> {
  state: PanelErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): PanelErrorBoundaryState {
    return { error };
  }

  componentDidUpdate(previous: PanelErrorBoundaryProps): void {
    if (previous.resetKey !== this.props.resetKey && this.state.error !== null) {
      this.setState({ error: null });
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("panel crashed", error, info.componentStack);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (error === null) return this.props.children;
    return (
      <div role="alert" className={cn(noticeClass("error"), "m-3")}>
        <p className="m-0">This page hit an error and was stopped: {error.message}</p>
        <p className="mt-1 mb-0 text-[0.8rem]">The rest of the dashboard still works.</p>
        <button
          type="button"
          className={cn(buttonClass, "mt-2")}
          onClick={() => this.setState({ error: null })}
        >
          Try again
        </button>
      </div>
    );
  }
}
