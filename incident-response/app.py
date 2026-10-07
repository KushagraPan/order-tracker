"""Grafana webhook receiver.

Receives alerts at POST /alerts on port 8001, saves the alert plus
related evidence (affected endpoint, recent logs, traces, metrics query)
to incident-response/incidents/<id>/, then launches the bundled
investigate.py evidence collector automatically in headless mode
(detached subprocess, no TTY).

The genuine coding-agent loop runs on the host: see
incident-response/watch-and-fix.ps1, which dispatches real headless
``opencode.cmd run`` sessions with the incident directory as context.
Point INCIDENT_AGENT_CMD at a different headless command to swap the
in-container default without changing this receiver.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

BASE_DIR = Path(__file__).parent
INCIDENTS_DIR = Path(os.getenv("INCIDENTS_DIR", str(BASE_DIR / "incidents")))
INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)

# Headless assistant command. Default is the bundled investigator, run
# detached (headless: no stdin/TTY, own process group, output to file).
# Example override:
#   INCIDENT_AGENT_CMD="opencode run --headless investigate this incident"
AGENT_CMD = os.getenv("INCIDENT_AGENT_CMD", f"{sys.executable} investigate.py")

app = FastAPI(title="Incident Responder")


@app.get("/healthz")
def health():
    return {"status": "ok"}


def _alert_id(alert: dict, index: int) -> str:
    name = (alert.get("labels") or {}).get("alertname", "alert")
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in name)[:40]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{safe}-{index}"


def _launch_headless_agent(incident_dir: Path) -> dict:
    """Start the coding assistant in headless mode for one incident."""
    log_path = incident_dir / "agent_stdout.log"
    log_file = open(log_path, "a")
    cmd = f"{AGENT_CMD} --incident {incident_dir}"
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=str(BASE_DIR),
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # detached: survives the HTTP request
    )
    launch_info = {
        "cmd": cmd,
        "pid": proc.pid,
        "headless": True,
        "log": str(log_path),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    (incident_dir / "agent_launch.json").write_text(json.dumps(launch_info, indent=2))
    return launch_info


@app.post("/alerts")
async def receive_alerts(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "invalid JSON"}, status_code=400)
    if isinstance(payload, dict) and isinstance(payload.get("alerts"), list):
        alerts = payload["alerts"]
    elif isinstance(payload, dict):
        alerts = [payload]
    elif isinstance(payload, list):
        alerts = payload
    else:
        return JSONResponse({"detail": "expected Grafana webhook payload"}, status_code=400)
    if not alerts:
        return JSONResponse({"detail": "no alerts in payload"}, status_code=400)

    incidents = []
    for i, alert in enumerate(alerts):
        if not isinstance(alert, dict):
            continue
        incident_id = _alert_id(alert, i)
        incident_dir = INCIDENTS_DIR / incident_id
        incident_dir.mkdir(parents=True, exist_ok=True)
        (incident_dir / "alert.json").write_text(json.dumps(alert, indent=2))
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        context = {
            "incident_id": incident_id,
            "status": alert.get("status"),
            "alertname": labels.get("alertname"),
            "severity": labels.get("severity"),
            "endpoint": (
                annotations.get("endpoint")
                or labels.get("endpoint")
                or labels.get("http_route")
                or "/api/orders/{order_id}"
            ),
            "window": annotations.get("window", "5m"),
            "dashboard": annotations.get("dashboard", "http://localhost:3000/d/order-tracker-requests"),
            "summary": annotations.get("summary", ""),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        (incident_dir / "context.json").write_text(json.dumps(context, indent=2))
        launch = _launch_headless_agent(incident_dir)
        incidents.append({"incident_id": incident_id, "agent_pid": launch["pid"]})
    return JSONResponse({"incidents": incidents}, status_code=202)


@app.get("/responses/latest", response_class=PlainTextResponse)
def latest_response():
    candidates = sorted(
        INCIDENTS_DIR.glob("*/agent_response.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return PlainTextResponse("no agent responses yet", status_code=404)
    return PlainTextResponse(candidates[0].read_text(), status_code=200)
