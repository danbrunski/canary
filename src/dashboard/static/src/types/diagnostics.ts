/** `/control/diagnostics/*`: one-shot diagnostic functions a scan tool runs
 *  on a module, offered where the vehicle profile records how. */

export interface DiagnosticFunction {
  kind: "clear_codes";
  transport: "kline";
  /** The module's name in the vehicle's K-line profile ("SRS"). */
  module: string;
  /** KWP2000 address, "0x58". */
  address: string;
  /** The request CANary sends, as the scan tool was seen to send it. */
  request_hex: string;
  evidence: string;
}

export interface DiagnosticFunctionsResponse {
  diagnostic_functions: {
    functions: DiagnosticFunction[];
    /** Whether a K-line collector is running to carry a function out. */
    kline_available: boolean;
    /** Why the K-line is not CANary's right now (passive, another tester). */
    blocked_reason: string | null;
  };
}

export interface ClearCodesResult {
  kind: "clear_codes";
  module: string;
  address: string;
  ok: boolean;
  reply_hex: string | null;
  detail: string;
  started_at: number;
  finished_at: number;
  duration_s: number;
}
