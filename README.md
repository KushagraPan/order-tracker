# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.

## Homework 4: observability and incident response

- **Telemetry (Q2):** `app/main.py` instruments every request with OpenTelemetry
  metrics (`http.server.request.count` with `http.route` +
  `http.response.status_code`), logs, and traces; console export for
  `docker compose logs app`.
- **Pipeline (Q3):** `compose.yaml` adds OpenTelemetry Collector, Prometheus,
  Loki, Tempo, and Grafana (admin/admin at `http://localhost:3000`,
  dashboard "Order Tracker - Requests and Errors"). Config:
  `otel-collector-config.yaml`, `prometheus.yml`, `loki-config.yaml`,
  `tempo.yaml`, `grafana/`.
- **Alert (Q4+Q6):** `grafana/provisioning/alerting/` defines `OrderTracker5xx`
  (5xx rate on `/api/orders/{order_id}` over 5m, `for: 1m`, no-data → OK)
  and a webhook contact point routing to the responder.
- **Responder (Q5+Q6):** `incident-response/` receives Grafana webhooks at
  `POST /alerts` (`:8001`), saves alert + evidence (Prometheus/Loki/Tempo/app
  health) to `incident-response/incidents/<id>/`, and starts the headless
  investigator (`investigate.py`), whose answer is the last line of
  `agent_response.md`.
- **Incident fix (Q6):** express-order delivery dates used
  `replace(day=day+2)`, which raised `ValueError` at month end (HTTP 500);
  fixed with `timedelta(days=2)`, verified `express-1002` → 200 and the
  alert back to Normal.
