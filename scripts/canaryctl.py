#!/usr/bin/env python3
"""Laptop CLI for CANary's headless control API.

Examples:
  scripts/canaryctl.py --host http://192.168.0.230:8000 status
  scripts/canaryctl.py --host http://192.168.0.230:8000 capture start --protocol drive
  scripts/canaryctl.py --host http://192.168.0.230:8000 tag highway_cruise
  scripts/canaryctl.py --host http://192.168.0.230:8000 capture stop

Set CANARY_HOST and CANARY_TOKEN to avoid repeating flags.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="canaryctl",
        description="Remote control client for a headless CANary daemon.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("CANARY_HOST") or os.environ.get("CANARY_URL"),
        help="CANary base URL, e.g. http://192.168.0.230:8000. Defaults to CANARY_HOST.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CANARY_TOKEN") or os.environ.get("CANARY_CONTROL_TOKEN"),
        help="Bearer token for /control endpoints. Defaults to CANARY_TOKEN.",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Fetch /status.")

    capture = sub.add_parser("capture", help="Capture-window commands.")
    capture_sub = capture.add_subparsers(dest="capture_command", required=True)

    start = capture_sub.add_parser("start", help="Start a labeled capture window.")
    start.add_argument("--purpose", default=None)
    start.add_argument("--protocol", default=None)
    start.add_argument("--vehicle-state", default=None)
    start.add_argument("--field", action="append", default=[], metavar="KEY=VALUE")

    capture_sub.add_parser("current", help="Show the current capture window.")

    stop = capture_sub.add_parser("stop", help="Stop the active capture window.")
    stop.add_argument("--reason", default=None)
    stop.add_argument("--field", action="append", default=[], metavar="KEY=VALUE")

    tag = sub.add_parser("tag", help="Tag the active capture window.")
    tag.add_argument("label")
    tag.add_argument("--kind", default="event")
    tag.add_argument("--timestamp", type=float, default=None)
    tag.add_argument("--field", action="append", default=[], metavar="KEY=VALUE")

    sessions = sub.add_parser("sessions", help="Session discovery/export commands.")
    sessions_sub = sessions.add_subparsers(dest="sessions_command", required=True)
    sessions_sub.add_parser("list", help="List sessions on the Pi.")
    show = sessions_sub.add_parser("show", help="Show one session.")
    show.add_argument("session_id")
    export = sessions_sub.add_parser("export", help="Download one session as a zip archive.")
    export.add_argument("session_id")
    export.add_argument("--out", default=None, help="Output zip path. Default: <session_id>.zip")

    jobs = sub.add_parser("jobs", help="Background maintenance jobs.")
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=True)
    start_job = jobs_sub.add_parser("start", help="Start a restricted background job.")
    start_job.add_argument(
        "kind",
        choices=[
            "retrain_if",
            "retrain_lstm",
            "retrain_all",
            "refit_behavior_models",
            "corpus_report",
        ],
    )
    start_job.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    start_job.add_argument("--days", type=int, default=None)
    start_job.add_argument("--all", action="store_true", dest="use_all_sessions")
    start_job.add_argument("--session-id", action="append", default=[])
    start_job.add_argument("--device", default=None)
    start_job.add_argument("--recipe", action="append", default=[])
    start_job.add_argument("--dry-run", action="store_true")
    jobs_sub.add_parser("list", help="List background jobs.")
    show_job = jobs_sub.add_parser("show", help="Show one job.")
    show_job.add_argument("job_id")
    logs_job = jobs_sub.add_parser("logs", help="Show one job's captured log.")
    logs_job.add_argument("job_id")

    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.host:
        print("error: --host or CANARY_HOST is required", file=sys.stderr)
        return 2

    try:
        payload = _dispatch(args)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    host = _base_url(args.host)
    if args.command == "status":
        return _request_json(host, "GET", "/status", token=None)

    if args.command == "capture":
        if args.capture_command == "start":
            body = {
                "purpose": args.purpose,
                "protocol": args.protocol,
                "vehicle_state": args.vehicle_state,
                "fields": _parse_fields(args.field),
            }
            return _request_json(host, "POST", "/control/captures", token=args.token, body=body)
        if args.capture_command == "current":
            return _request_json(host, "GET", "/control/captures/current", token=args.token)
        if args.capture_command == "stop":
            body = {"reason": args.reason, "fields": _parse_fields(args.field)}
            return _request_json(
                host, "POST", "/control/captures/current/stop", token=args.token, body=body
            )

    if args.command == "tag":
        body = {
            "label": args.label,
            "kind": args.kind,
            "timestamp": args.timestamp,
            "fields": _parse_fields(args.field),
        }
        return _request_json(
            host, "POST", "/control/captures/current/tags", token=args.token, body=body
        )

    if args.command == "sessions":
        if args.sessions_command == "list":
            return _request_json(host, "GET", "/control/sessions", token=args.token)
        if args.sessions_command == "show":
            return _request_json(
                host, "GET", f"/control/sessions/{args.session_id}", token=args.token
            )
        if args.sessions_command == "export":
            content = _request_bytes(
                host, "GET", f"/control/sessions/{args.session_id}/export", token=args.token
            )
            out = args.out or f"{args.session_id}.zip"
            with open(out, "wb") as handle:
                handle.write(content)
            return {"saved": out, "bytes": len(content)}

    if args.command == "jobs":
        if args.jobs_command == "start":
            params = _job_params(args)
            return _request_json(
                host,
                "POST",
                "/control/jobs",
                token=args.token,
                body={"kind": args.kind, "params": params},
            )
        if args.jobs_command == "list":
            return _request_json(host, "GET", "/control/jobs", token=args.token)
        if args.jobs_command == "show":
            return _request_json(host, "GET", f"/control/jobs/{args.job_id}", token=args.token)
        if args.jobs_command == "logs":
            return _request_json(host, "GET", f"/control/jobs/{args.job_id}/logs", token=args.token)

    raise RuntimeError(f"unsupported command: {args.command}")


def _base_url(host: str) -> str:
    base = host.rstrip("/")
    if not base.startswith(("http://", "https://")):
        return f"http://{base}"
    return base


def _parse_fields(raw: list[str]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for item in raw:
        if "=" not in item:
            raise ValueError(f"--field {item!r}: expected KEY=VALUE")
        key, value = item.split("=", 1)
        try:
            fields[key] = json.loads(value)
        except json.JSONDecodeError:
            fields[key] = value
    return fields


def _job_params(args: argparse.Namespace) -> dict[str, Any]:
    params = _parse_fields(args.param)
    if args.days is not None:
        params["days"] = args.days
    if args.use_all_sessions:
        params["all"] = True
    if args.session_id:
        params["session_ids"] = args.session_id
    if args.device is not None:
        params["device"] = args.device
    if args.recipe:
        params["recipes"] = args.recipe
    if args.dry_run:
        params["dry_run"] = True
    return params


def _request_json(
    host: str,
    method: str,
    path: str,
    *,
    token: str | None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(f"{host}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as response:  # noqa: S310 - operator-provided LAN URL
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path} failed: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc.reason}") from exc
    if not raw:
        return {}
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{method} {path} returned non-object JSON")
    return parsed


def _request_bytes(host: str, method: str, path: str, *, token: str | None) -> bytes:
    headers = {"Accept": "application/octet-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(f"{host}{path}", headers=headers, method=method)
    try:
        with urlopen(req, timeout=120) as response:  # noqa: S310 - operator-provided LAN URL
            return response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path} failed: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc.reason}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
