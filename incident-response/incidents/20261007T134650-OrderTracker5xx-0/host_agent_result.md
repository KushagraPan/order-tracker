# Incident result: 20261007T134650-OrderTracker5xx-0

## Diagnosis

Root cause confirmed: `ValueError: day is out of range for month` raised in
`order_detail` (`app/main.py:159`) for express orders placed near month end.

Evidence chain:

- Seed data creates `express-1002` with `created_at = now.replace(day=1) - timedelta(days=1)`,
  i.e. the last day of the previous month (`app/main.py:139-142`). At incident time
  (2026-10-07) that is **2026-09-30**, confirmed by the live response body
  (`"created_at":"2026-09-30T12:44:06.284456+00:00"`).
- The express estimate used calendar-day arithmetic:
  `placed_at.replace(day=placed_at.day + 2)` -> `replace(day=32)` for a September date
  -> `ValueError: day is out of range for month`.
- The exception escapes the route handler and is caught/re-raised by `otel_middleware`,
  which logs it (`code_line_number: 207`, message `order lookup`, `http.status_code: 500`)
  and produces the HTTP 500.
- Evidence matches: Loki shows `GET /api/orders/express-1002` -> `http_status_code: 500`
  (trace `75f19177bec89f9c670157cc1dc42877`); Prometheus shows a non-zero 5xx rate
  (0.0625) on `/api/orders/{order_id}`; `/healthz` was OK (so the service was up).
- Local repro: `datetime.fromisoformat('2026-09-30T00:00:00+00:00').replace(day=30 + 2)`
  raises `ValueError: day is out of range for month`.

Standard orders are unaffected (`order_detail` only computes an estimate for
`priority == "express"`).

## Exact diff

Only `app/main.py` was edited; no telemetry or alerting code was touched.

```diff
--- a/app/main.py
+++ b/app/main.py
@@ -156,7 +156,7 @@ def order_detail(row):
     order = as_dict(row)
     if order["priority"] == "express":
         placed_at = datetime.fromisoformat(order["created_at"])
-        estimated_at = placed_at.replace(day=placed_at.day + 2)
+        estimated_at = placed_at + timedelta(days=2)
         order["estimated_delivery"] = estimated_at.date().isoformat()
     return order

```

Note: the buggy line existed only in the working tree at incident start; `HEAD`
(c7bfbd0 "HW4: observability, 5xx alerting, incident responder, express fix")
already contains the fixed line, so `git diff` for `app/main.py` is now empty
(working tree matches HEAD). No other repository files were modified by this fix.

## Test output

```
$ uv run pytest -q
3 passed, 4 warnings in 1.09s
```

(Warnings are pre-existing deprecation warnings from fastapi/testclient, starlette,
and opentelemetry SDK.)

## Failing request: before/after

Request: `GET http://localhost:8000/api/orders/express-1002`

| | Status | Body / evidence |
|---|---|---|
| Before (evidence at 2026-10-07T13:46:50Z) | **500** | Loki log: `http_target=/api/orders/express-1002`, `http_status_code=500`, `otel_middleware` exception path (line 207) |
| After (`docker compose up --build -d --wait app`) | **200** | `{"id":"express-1002","customer":"Sam","item":"Headphones","priority":"express","status":"preparing","created_at":"2026-09-30T12:44:06.284456+00:00","estimated_delivery":"2026-10-02"}` |

`estimated_delivery` is now correctly 2026-10-02 (2026-09-30 + 2 days).

Regression check after the fix: `GET /api/orders/standard-1002` -> **404**
(a 404 must not fire the OrderTracker5xx alert), and the app container reports
healthy (`docker compose up --wait` passed).

Verdict: Fixed - replaced calendar-day `replace(day=day+2)` with `timedelta(days=2)`; tests green and GET /api/orders/express-1002 now returns 200.
