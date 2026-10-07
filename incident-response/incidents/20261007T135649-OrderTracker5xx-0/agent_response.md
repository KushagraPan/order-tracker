# Agent response (incident)

- alert: OrderTracker5xx (resolved)
- endpoint under suspicion: /api/orders/{order_id}
- window: 5m
- evidence saved to evidence.json (Prometheus 5xx query, Loki logs sample, Tempo status, app health).
- No code changes made by this responder (Q5 only observes).

Evidence collected for /api/orders/{order_id} over 5m; see evidence.json.
