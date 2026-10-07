import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Status, StatusCode


DB_PATH = Path(os.getenv("ORDER_DB_PATH", "data/orders.db"))
STATUSES = {"received", "preparing", "shipped", "delivered"}

logger = logging.getLogger("order-tracker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def _setup_opentelemetry():
    resource = Resource.create({"service.name": "order-tracker"})
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    try:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)))
    except Exception as exc:
        logger.warning("OTLP trace exporter disabled: %s", exc)
    try:
        otel_trace.set_tracer_provider(provider)
    except Exception:
        pass

    readers = [PeriodicExportingMetricReader(ConsoleMetricExporter(), export_interval_millis=1000)]
    try:
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True),
                export_interval_millis=5000,
            )
        )
    except Exception as exc:
        logger.warning("OTLP metric exporter disabled: %s", exc)
    meter_provider = MeterProvider(resource=resource, metric_readers=readers)
    try:
        otel_metrics.set_meter_provider(meter_provider)
    except Exception:
        pass

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(ConsoleLogExporter()))
    try:
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=otlp_endpoint, insecure=True))
        )
    except Exception as exc:
        logger.warning("OTLP log exporter disabled: %s", exc)
    try:
        set_logger_provider(logger_provider)
    except Exception:
        pass
    try:
        otel_handler = LoggingHandler(logger_provider=logger_provider)
        root_logger = logging.getLogger()
        root_logger.addHandler(otel_handler)
        logger.addHandler(otel_handler)
    except Exception as exc:
        logger.warning("OTEL logging bridge disabled: %s", exc)


_setup_opentelemetry()

tracer = otel_trace.get_tracer("order-tracker")
meter = otel_metrics.get_meter("order-tracker")

request_counter = meter.create_counter(
    name="http.server.request.count",
    description="Total HTTP requests",
    unit="1",
)
request_duration = meter.create_histogram(
    name="http.server.request.duration",
    description="HTTP request duration in seconds",
    unit="s",
)


def _route_template(method: str, path: str) -> str:
    if path == "/":
        return "/"
    if path == "/healthz":
        return "/healthz"
    if path == "/api/orders" and method in ("GET", "POST"):
        return "/api/orders"
    if path.startswith("/api/orders/"):
        return "/api/orders/{order_id}"
    return path


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer TEXT NOT NULL,
                item TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        if db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            now = datetime.now(timezone.utc)
            previous_month_end = now.replace(day=1) - timedelta(days=1)
            for order in (
                ("standard-1001", "Avery", "Notebook", "standard", "received", now),
                ("express-1002", "Sam", "Headphones", "express", "preparing", previous_month_end),
                ("standard-1003", "Riley", "Water bottle", "standard", "shipped", now),
            ):
                db.execute(
                    "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                    (*order[:5], order[5].isoformat()),
                )


def as_dict(row):
    return dict(row) if row else None


def order_detail(row):
    order = as_dict(row)
    if order["priority"] == "express":
        placed_at = datetime.fromisoformat(order["created_at"])
        estimated_at = placed_at + timedelta(days=2)
        order["estimated_delivery"] = estimated_at.date().isoformat()
    return order


class NewOrder(BaseModel):
    customer: str = Field(min_length=1, max_length=80)
    item: str = Field(min_length=1, max_length=120)
    priority: str = "standard"


class StatusUpdate(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Order Tracker", lifespan=lifespan)


@app.middleware("http")
async def otel_middleware(request: Request, call_next):
    route = _route_template(request.method, request.url.path)
    start = time.perf_counter()
    with tracer.start_as_current_span(f"{request.method} {route}") as span:
        span.set_attribute("http.method", request.method)
        span.set_attribute("http.route", route)
        span.set_attribute("http.target", request.url.path)
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception as exc:
            status = 500
            span.set_attribute("http.response.status_code", status)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            duration = time.perf_counter() - start
            attrs = {
                "http.method": request.method,
                "http.route": route,
                "http.response.status_code": str(status),
            }
            request_counter.add(1, attrs)
            request_duration.record(duration, attrs)
            logger.info(
                "order lookup",
                extra={
                    "http.method": request.method,
                    "http.route": route,
                    "http.target": request.url.path,
                    "http.status_code": status,
                    "duration_s": round(duration, 4),
                },
            )
            print(
                f"request metric route={route} method={request.method} status={status} "
                f"duration_s={duration:.4f}",
                flush=True,
            )
            raise
        duration = time.perf_counter() - start
        span.set_attribute("http.response.status_code", status)
        if status >= 500:
            span.set_status(Status(StatusCode.ERROR, f"HTTP {status}"))
        else:
            span.set_status(Status(StatusCode.OK))
        attrs = {
            "http.method": request.method,
            "http.route": route,
            "http.response.status_code": str(status),
        }
        request_counter.add(1, attrs)
        request_duration.record(duration, attrs)
        logger.info(
            "request method=%s route=%s target=%s status=%s duration_s=%.4f",
            request.method,
            route,
            request.url.path,
            status,
            duration,
        )
        print(
            f"request metric route={route} method={request.method} status={status} "
            f"duration_s={duration:.4f}",
            flush=True,
        )
        return response


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent.parent / "static" / "index.html")


@app.get("/healthz")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/orders")
def list_orders():
    with connect() as db:
        rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    return [as_dict(row) for row in rows]


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    with tracer.start_as_current_span("order.lookup") as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("http.route", "/api/orders/{order_id}")
        with connect() as db:
            row = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            span.set_attribute("http.response.status_code", 404)
            span.set_status(Status(StatusCode.ERROR, "Order not found"))
            logger.info("order lookup order_id=%s status=404", order_id)
            raise HTTPException(404, "Order not found")
        order = order_detail(row)
        span.set_attribute("http.response.status_code", 200)
        span.set_attribute("order.priority", order.get("priority", ""))
        span.set_status(Status(StatusCode.OK))
        logger.info(
            "order lookup order_id=%s status=200 priority=%s",
            order_id,
            order.get("priority", ""),
        )
        return order


@app.post("/api/orders", status_code=201)
def create_order(order: NewOrder):
    if order.priority not in {"standard", "express"}:
        raise HTTPException(422, "Priority must be standard or express")
    order_id = str(uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, order.customer, order.item, order.priority, "received",
             datetime.now(timezone.utc).isoformat()),
        )
    return get_order(order_id)


@app.patch("/api/orders/{order_id}")
def update_status(order_id: str, update: StatusUpdate):
    if update.status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    with connect() as db:
        cursor = db.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (update.status, order_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(404, "Order not found")
    return get_order(order_id)
