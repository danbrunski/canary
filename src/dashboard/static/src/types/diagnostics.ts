/** `/control/diagnostics/*`: one-shot diagnostic functions a scan tool runs
 *  on a module, offered where the vehicle profile records how. */

export type DiagnosticFunctionKind = "read_codes" | "clear_codes";

/** One request of a code read and which codes its reply holds. */
export interface CodeReadRequest {
  request_hex: string;
  status: CodeStatus;
  /** "inferred": read off a scan tool's display beside one observation. */
  status_confidence: "confirmed" | "inferred";
}

export type CodeStatus = "current" | "past";

export interface DiagnosticFunction {
  kind: DiagnosticFunctionKind;
  transport: "kline";
  /** The module's name in the vehicle's K-line profile ("SRS"). */
  module: string;
  /** KWP2000 address, "0x58". */
  address: string;
  /** The request(s) CANary sends, as the scan tool was seen to send them
   *  ("a2 00, a2 01" for a code read). */
  request_hex: string;
  /** A code read's requests, in order (absent for a clear). */
  requests?: CodeReadRequest[];
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

/** One request sent after StartCommunication and its reply (responsePending
 *  frames are not listed). */
export interface DiagnosticExchange {
  request_hex: string;
  reply_hex: string | null;
}

interface DiagnosticFunctionResult {
  module: string;
  address: string;
  ok: boolean;
  reply_hex: string | null;
  detail: string;
  started_at: number;
  finished_at: number;
  duration_s: number;
  raw?: DiagnosticExchange[];
}

export interface ClearCodesResult extends DiagnosticFunctionResult {
  kind: "clear_codes";
}

export interface StoredCode {
  /** SAE J2012 code, "B1023". */
  code: string;
  /** The two-byte word it decodes from, "0x9023". */
  word: string;
  status: CodeStatus;
  status_confidence: "confirmed" | "inferred";
}

export interface ReadCodesResult extends DiagnosticFunctionResult {
  kind: "read_codes";
  /** `[]`: read, none stored. `null`: the read failed (see `detail`). */
  codes: StoredCode[] | null;
}
