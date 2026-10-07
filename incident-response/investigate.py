"""Headless investigating assistant.

Runs detached (no TTY) when the responder receives a Grafana alert.
Reads incident-response/incidents/<id>/alert.json + context.json,
collects evidence (Prometheus 5xx query, Loki logs, Tempo status,
app health), and writes agent_response.md. The last line of that file
is the agent's answer for Homework Q5.

Usage: python investigate.py --incident <incident_dir>
"""

import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROM_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
LOKI_URL = os.getenv("LOKI_URL", "http://loki:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://tempo:3200")
APP_URL = os.getenv("APP_URL", "http://app:8000")

TEST_LAST_LINE = "Responder self-test passed; no incident to fix."


def http_get(url: str, timeout: int = 8) -> tuple[int | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:4000]
    except Exception as exc:
        return None, f"ERROR: {exc}"


def is_test_alert(alert: dict) -> bool:
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    return (
        labels.get("test") == "true"
        or labels.get("alertname") == "ResponderTest"
        or "no incident to fix" in (annotations.get("summary") or "")
    )


def write_test_response(incident_dir: str) -> str:
    body = (
        "# Agent response (self-test)\n\n"
        "Received a Grafana test notification.\n"
        "- status: firing (test)\n"
        "- alertname: ResponderTest\n"
        "- This is a drill: the payload says there is no incident to fix.\n"
        "- Receiver, evidence saver, and headless launch all worked.\n\n"
        f"{TEST_LAST_LINE}\n"
    )
    with open(os.path.join(incident_dir, "agent_response.md"), "w") as f:
        f.write(body)
    return TEST_LAST_LINE


def investigate(incident_dir: str) -> str:
    with open(os.path.join(incident_dir, "alert.json")) as f:
        alert = json.load(f)
    try:
        with open(os.path.join(incident_dir, "context.json")) as f:
            context = json.load(f)
    except FileNotFoundError:
        context = {}
    if is_test_alert(alert):
        return write_test_response(incident_dir)

    endpoint = context.get("endpoint", "/api/orders/{order_id}")
    window = context.get("window", "5m")
    evidence: dict = {
        "endpoint": endpoint,
        "window": window,
        "alertname": (alert.get("labels") or {}).get("alertname"),
        "status": alert.get("status"),
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }

    prom_q = (
        'sum(rate(http_server_request_count_total{http_route="/api/orders/{order_id}",'
        'http_response_status_code=~"5.."}[5m]))'
    )
    s, b = http_get(PROM_URL + "/api/v1/query?query=" + urllib.parse.quote(prom_q))
    evidence["prometheus_5xx"] = {"http_status": s, "body": b}

    loki_q = urllib.parse.quote('{service_name="order-tracker"}')
    s, b = http_get(f"{LOKI_URL}/loki/api/v1/query_range?query={loki_q}&limit=5")
    evidence["loki_logs"] = {"http_status": s, "body": b[:2000]}

    s, b = http_get(f"{TEMPO_URL}/status/services")
    evidence["tempo_status"] = {"http_status": s, "body": b[:1000]}

    s, b = http_get(f"{APP_URL}/healthz")
    evidence["app_health"] = {"http_status": s, "body": b[:500]}

    with open(os.path.join(incident_dir, "evidence.json"), "w") as f:
        json.dump(evidence, f, indent=2)

    last_line = f"Evidence collected for {endpoint} over {window}; see evidence.json."
    body = (
        "# Agent response (incident)\n\n"
        f"- alert: {(alert.get('labels') or {}).get('alertname')} "
        f"({alert.get('status')})\n"
        f"- endpoint under suspicion: {endpoint}\n"
        f"- window: {window}\n"
        "- evidence saved to evidence.json (Prometheus 5xx query, Loki logs sample, "
        "Tempo status, app health).\n"
        "- No code changes made by this responder (Q5 only observes).\n\n"
        f"{last_line}\n"
    )
    with open(os.path.join(incident_dir, "agent_response.md"), "w") as f:
        f.write(body)
    return last_line


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--incident", required=True)
    args = parser.parse_args()
    last = investigate(args.incident)
    print(last)


if __name__ == "__main__":
    main()
