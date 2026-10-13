#!/usr/bin/env python3
"""Laptop CLI for CANary's headless control API.

Examples:
  scripts/canaryctl.py --host http://192.168.0.230:8000 status
  scripts/canaryctl.py --host http://192.168.0.230:8000 capture start --protocol drive
  scripts/canaryctl.py --host http://192.168.0.230:8000 tag highway_cruise
  scripts/canaryctl.py --host http://192.168.0.230:8000 capture stop
  scripts/canaryctl.py --host http://localhost:8000 pair    # on the Pi, over SSH

Set CANARY_HOST and CANARY_CONTROL_TOKEN to avoid repeating flags.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _env_file_path() -> Path:
    """Where to look for persisted CANARY_* defaults.

    ``CANARY_ENV_FILE`` overrides the location (handy for tests); otherwise
    ``~/.canary/env`` so the operator doesn't re-export host/token each shell.
    """
    override = os.environ.get("CANARY_ENV_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".canary" / "env"


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv-style ``KEY=VALUE`` file. Missing/unreadable → {}.

    Tolerates blank lines, ``#`` comments, a leading ``export``, and single-
    or double-quoted values, so the same file can also be ``source``-d.
    """
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return {}
    # The file holds a bearer token; nudge if it's group/other-readable.
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            print(
                f"warning: {path} is accessible by other users; "
                f"chmod 600 it (it holds CANARY_CONTROL_TOKEN).",
                file=sys.stderr,
            )
    except OSError:
        pass
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def _resolve_default(file_env: dict[str, str], *names: str) -> str | None:
    """First non-empty value across the process env, then the env file.

    A real environment variable always wins over the file, so an inline
    ``CANARY_CONTROL_TOKEN=... canaryctl ...`` or an explicit export still overrides.
    """
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    for name in names:
        value = file_env.get(name)
        if value:
            return value
    return None


#: Where ``install_pi.sh`` puts the shared token (root:canary, 0640).
DEFAULT_TOKEN_FILE = Path("/etc/canary/control.token")


def _token_file_value() -> str | None:
    """The shared token from the Pi's own token file, when run on the Pi.

    Lets ``canaryctl pair`` over SSH work without copying the token around.
    ``CANARY_CONTROL_TOKEN_FILE`` overrides the path, as in ``install_pi.sh``.
    """
    path = Path(os.environ.get("CANARY_CONTROL_TOKEN_FILE") or DEFAULT_TOKEN_FILE)
    try:
        return path.read_text().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="canaryctl",
        description="Remote control client for a headless CANary daemon.",
    )
    file_env = _load_env_file(_env_file_path())
    parser.add_argument(
        "--host",
        default=_resolve_default(file_env, "CANARY_HOST", "CANARY_URL"),
        help=(
            "CANary base URL, e.g. http://192.168.0.230:8000. Defaults to "
            "$CANARY_HOST, then CANARY_HOST in ~/.canary/env."
        ),
    )
    parser.add_argument(
        "--token",
        default=(
            _resolve_default(file_env, "CANARY_CONTROL_TOKEN", "CANARY_TOKEN")
            or _token_file_value()
        ),
        help=(
            "Bearer token for /control endpoints. Defaults to "
            "$CANARY_CONTROL_TOKEN, then CANARY_CONTROL_TOKEN in "
            "~/.canary/env (CANARY_TOKEN accepted as a fallback), then "
            "/etc/canary/control.token when run on the Pi."
        ),
    )

    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="Fetch /status.")
    status.add_argument(
        "--compact",
        action="store_true",
        help=(
            "Fetch the pollable projection (GET /status?view=compact): "
            "vehicle_state plus the loss/queue/tick/OBD counters and a "
            "served_at stamp, ~1/10th the time and 1/20th the bytes. Use it "
            "for a watchdog loop or to bracket a counter measurement."
        ),
    )
    sub.add_parser("preflight", help="Run control-plane preflight checks.")
    sub.add_parser("version", help="Fetch control API version/build info.")

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
    metadata = sessions_sub.add_parser("metadata", help="Update one session's metadata.")
    metadata.add_argument("session_id")
    metadata.add_argument(
        "--replace", action="store_true", help="Replace metadata instead of merging."
    )
    metadata.add_argument("--field", action="append", default=[], metavar="KEY=VALUE")
    pin = sessions_sub.add_parser("pin", help="Pin a session so retention does not delete it.")
    pin.add_argument("session_id")
    pin.add_argument("--reason", default=None)
    pin.add_argument("--field", action="append", default=[], metavar="KEY=VALUE")
    unpin = sessions_sub.add_parser("unpin", help="Unpin a session.")
    unpin.add_argument("session_id")
    delete = sessions_sub.add_parser("delete", help="Delete a session.")
    delete.add_argument("session_id")
    delete.add_argument("--force", action="store_true", help="Allow deleting a pinned session.")

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
    cancel_job = jobs_sub.add_parser("cancel", help="Cancel a queued/running job.")
    cancel_job.add_argument("job_id")

    models = sub.add_parser("models", help="Model artifact operations.")
    models_sub = models.add_subparsers(dest="models_command", required=True)
    upload = models_sub.add_parser("upload", help="Upload an artifact + manifest to the Pi.")
    upload.add_argument("model_type", choices=["isolation_forest", "lstm_autoencoder"])
    upload.add_argument("--artifact", required=True)
    upload.add_argument("--manifest", required=True)

    pair = sub.add_parser(
        "pair",
        help="Approve a device waiting to pair (a bench Pi with no key to turn).",
        description=(
            "List devices waiting to pair. With exactly one waiting, ask to approve it "
            "(--yes skips the question); with several, name one with --approve ID."
        ),
    )
    pair.add_argument("--approve", metavar="ID", default=None, help="Approve this request.")
    pair.add_argument("--yes", action="store_true", help="Approve a lone request unasked.")

    devices = sub.add_parser("devices", help="List paired devices, or remove one.")
    devices.add_argument("--remove", metavar="ID", default=None, help="Remove this device.")

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
        path = "/status?view=compact" if args.compact else "/status"
        return _request_json(host, "GET", path, token=None)
    if args.command == "preflight":
        return _request_json(host, "GET", "/control/preflight", token=args.token)
    if args.command == "version":
        return _request_json(host, "GET", "/control/version", token=args.token)

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
        if args.sessions_command == "metadata":
            body = {"metadata": _parse_fields(args.field), "merge": not args.replace}
            return _request_json(
                host,
                "POST",
                f"/control/sessions/{args.session_id}/metadata",
                token=args.token,
                body=body,
            )
        if args.sessions_command == "pin":
            body = {"reason": args.reason, "metadata": _parse_fields(args.field)}
            return _request_json(
                host,
                "POST",
                f"/control/sessions/{args.session_id}/pin",
                token=args.token,
                body=body,
            )
        if args.sessions_command == "unpin":
            return _request_json(
                host,
                "POST",
                f"/control/sessions/{args.session_id}/unpin",
                token=args.token,
                body={},
            )
        if args.sessions_command == "delete":
            suffix = "?force=true" if args.force else ""
            return _request_json(
                host, "DELETE", f"/control/sessions/{args.session_id}{suffix}", token=args.token
            )

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
        if args.jobs_command == "cancel":
            return _request_json(
                host,
                "POST",
                f"/control/jobs/{args.job_id}/cancel",
                token=args.token,
                body={},
            )

    if args.command == "models":
        if args.models_command == "upload":
            with open(args.artifact, "rb") as handle:
                artifact = base64.b64encode(handle.read()).decode("ascii")
            with open(args.manifest) as handle:
                manifest = json.load(handle)
            return _request_json(
                host,
                "POST",
                f"/control/models/{args.model_type}/artifact",
                token=args.token,
                body={"artifact_b64": artifact, "manifest": manifest},
            )

    if args.command == "pair":
        return _pair(host, args)
    if args.command == "devices":
        if args.remove is not None:
            return _request_json(
                host, "DELETE", f"/control/devices/{args.remove}", token=args.token
            )
        return _request_json(host, "GET", "/control/devices", token=args.token)

    raise RuntimeError(f"unsupported command: {args.command}")


def _confirm(question: str) -> bool:
    """Ask on the terminal; stdout stays the JSON result. No terminal is a no."""
    if not sys.stdin.isatty():
        return False
    print(f"{question} [y/N] ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def _pair(host: str, args: argparse.Namespace) -> dict[str, Any]:
    """Approve a waiting pairing request with the shared token.

    The bench path (docs/ble-provisioning.md): a Pi with no vehicle has no key
    to turn, so whoever holds the shared token vouches for the device instead.
    A lone request still needs a yes, because a stranger on the same network
    can make one too: read the name before approving it.
    """
    if args.approve is not None:
        return _approve(host, args.approve, token=args.token)
    waiting = _request_json(host, "GET", "/control/pairing/requests", token=args.token)
    requests = waiting.get("requests", [])
    if not requests:
        return {
            "requests": [],
            "detail": "No device is waiting to pair. Tap 'Pair this device' on it first.",
        }
    if len(requests) > 1:
        return {
            "requests": requests,
            "detail": f"{len(requests)} devices are waiting; approve one with --approve ID.",
        }
    (only,) = requests
    question = f"Approve {only['name']!r} (request {only['id']}, {only['age_s']:.0f} s old)?"
    if not (args.yes or _confirm(question)):
        return {"requests": requests, "approved": None, "detail": "Not approved."}
    return _approve(host, str(only["id"]), token=args.token)


def _approve(host: str, request_id: str, *, token: str | None) -> dict[str, Any]:
    return _request_json(
        host, "POST", f"/control/pairing/requests/{request_id}/approve", token=token, body={}
    )


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
