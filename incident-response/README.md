# Incident Responder (Homework Q5)

Receives Grafana alert webhooks at `POST /alerts` on port **8001**.

What happens per alert:
1. Alert JSON is saved to `incidents/<timestamp>-<alertname>-<i>/alert.json`
   with a `context.json` (endpoint, window, dashboard, severity).
2. The investigating assistant is started **automatically in headless mode**:
   a detached subprocess (`start_new_session=True`, stdin `/dev/null`,
   output to `agent_stdout.log`), recorded in `agent_launch.json`.
   Default command is `python investigate.py --incident <dir>`;
   override with `INCIDENT_AGENT_CMD` to call a real coding-agent CLI.
3. The assistant saves evidence (`evidence.json`: Prometheus 5xx query,
   Loki log sample, Tempo status, app health) and writes
   `agent_response.md`. The **last line** of that file is its answer.

Test drill (Q5):

```bash
curl -X POST http://localhost:8001/alerts \
  -H 'Content-Type: application/json' \
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"ResponderTest","test":"true"},"annotations":{"summary":"Test notification; no incident to fix"}}]}'
```

Then read the answer:

```bash
curl http://localhost:8001/responses/latest
# or: cat incidents/*/agent_response.md
```

Real Grafana wiring happens in Q6 (webhook contact point to `/alerts`).
This service only observes; it never edits app code.
